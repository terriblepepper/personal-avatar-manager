"""Bounded, aggregate-only observations of the platform HTTP surface."""
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from math import ceil
from time import perf_counter, time

from starlette.requests import ClientDisconnect


LIMITS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 15000, 30000, 60000, 120000)
METHODS = frozenset(('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'))
HISTORY_MINUTES = 7 * 24 * 60
BUFFER_MINUTES = 120
COUNTERS = ('requests', 'client_errors', 'server_errors', 'interrupted', 'received_bytes', 'sent_bytes')


@dataclass
class Histogram:
    counts: list = field(default_factory=lambda: [0] * (len(LIMITS_MS) + 1))
    peak: float = 0

    def record(self, seconds):
        ms = seconds * 1000
        self.counts[bisect_left(LIMITS_MS, ms)] += 1
        self.peak = max(self.peak, ms)

    def merge(self, other):
        self.counts = [a + b for a, b in zip(self.counts, other.counts)]
        self.peak = max(self.peak, other.peak)

    def percentile(self, fraction):
        count = sum(self.counts)
        if not count:
            return None
        target = ceil(count * fraction)
        for bound, size in zip(LIMITS_MS, self.counts):
            target -= size
            if target <= 0:
                return min(bound, round(self.peak, 2))
        return round(self.peak, 2)


@dataclass
class Minute:
    counts: Counter = field(default_factory=Counter)
    routes: Counter = field(default_factory=Counter)
    route_latency: dict = field(default_factory=dict)
    latency: Histogram = field(default_factory=Histogram)
    first_byte: Histogram = field(default_factory=Histogram)

    def totals(self):
        return {key: self.counts[key] for key in COUNTERS}


class HttpMetrics:
    def __init__(self):
        self.started_at = time()
        self.minutes = {}
        self.in_flight = 0
        self.open_streams = 0

    def at(self, minute=None):
        minute = int(time() // 60) if minute is None else minute
        if minute not in self.minutes:
            self.minutes[minute] = Minute()
            for key in tuple(self.minutes):
                if key < minute - BUFFER_MINUTES:
                    del self.minutes[key]
        return self.minutes[minute]

    def recent(self):
        current = int(time() // 60)
        windows = [sample for key, sample in self.minutes.items() if current - 4 <= key <= current]
        counts, routes = Counter(), Counter()
        latency, first_byte = Histogram(), Histogram()
        for sample in windows:
            counts.update(sample.counts)
            routes.update(sample.routes)
            latency.merge(sample.latency)
            first_byte.merge(sample.first_byte)
        rankings = []
        for route, count in routes.most_common(12):
            distribution = Histogram()
            for sample in windows:
                if route in sample.route_latency:
                    distribution.merge(sample.route_latency[route])
            rankings.append({'route': route, 'count': count, 'p95_ms': distribution.percentile(.95)})
        seconds = round(max(1., time() - max(self.started_at, (current - 4) * 60)), 1)
        return {**{key: counts[key] for key in COUNTERS},
                'span_seconds': seconds, 'in_flight': self.in_flight,
                'open_streams': self.open_streams,
                'p50_ms': latency.percentile(.50), 'p95_ms': latency.percentile(.95),
                'p99_ms': latency.percentile(.99), 'first_byte_p95_ms': first_byte.percentile(.95),
                'routes': rankings}


def route_key(scope):
    route = getattr(scope.get('route'), 'path', None)
    method = scope['method'] if scope['method'] in METHODS else 'OTHER'
    if route is None:
        path = scope.get('path', '')
        route = next((prefix for prefix in ('/assets/', '/static/') if path.startswith(prefix)), '未匹配路由')
    return f'{method} {route}'


class ObserveHTTP:
    def __init__(self, app, metrics):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or path in ('/ops', '/api/ops') or path.startswith(('/ops/', '/api/ops/')):
            return await self.app(scope, receive, send)
        started = perf_counter()
        self.metrics.in_flight += 1
        status, streaming, completed, exception = 0, False, False, False
        first_body = False
        response_headers = ()

        async def observe_receive():
            message = await receive()
            if message['type'] == 'http.request':
                size = len(message.get('body', b''))
                if size:
                    self.metrics.at().counts['received_bytes'] += size
            return message

        async def observe_send(message):
            nonlocal status, streaming, completed, first_body, response_headers
            await send(message)
            if message['type'] == 'http.response.start':
                status = message['status']
                response_headers = message.get('headers', ())
                streaming = any(key.lower() == b'content-type' and b'text/event-stream' in value.lower()
                                for key, value in response_headers)
                if streaming:
                    self.metrics.open_streams += 1
            if message['type'] in ('http.response.body', 'http.response.pathsend'):
                if not first_body:
                    first_body = True
                    self.metrics.at().first_byte.record(perf_counter() - started)
                size = len(message.get('body', b''))
                if message['type'] == 'http.response.pathsend':
                    length = next((value for key, value in response_headers if key.lower() == b'content-length'), b'0')
                    if length.isdigit():
                        size = int(length)
                if size:
                    self.metrics.at().counts['sent_bytes'] += size
                completed = not message.get('more_body', False)

        try:
            await self.app(scope, observe_receive, observe_send)
        except ClientDisconnect:
            raise
        except Exception:
            exception = True
            raise
        finally:
            self.metrics.in_flight -= 1
            sample = self.metrics.at()
            sample.counts['requests'] += 1
            key = route_key(scope)
            sample.routes[key] += 1
            if streaming:
                self.metrics.open_streams -= 1
            elif completed:
                elapsed = perf_counter() - started
                sample.latency.record(elapsed)
                distribution = sample.route_latency.get(key)
                if distribution is None:
                    distribution = sample.route_latency[key] = Histogram()
                distribution.record(elapsed)
            if 400 <= status < 500:
                sample.counts['client_errors'] += 1
            if status >= 500 or (exception and not status):
                sample.counts['server_errors'] += 1
            if not completed and (status or not exception):
                sample.counts['interrupted'] += 1
