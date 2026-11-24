"""Bounded process-local timings for administrator diagnostics."""
from collections import deque
from contextlib import contextmanager
from math import ceil
from threading import Lock
from time import perf_counter

_lock = Lock()
_series = {}
_WINDOW = 256


def record_duration(name, seconds):
    """Callers supply fixed stage names or registered route templates."""
    with _lock:
        series = _series.get(name)
        if series is None:
            series = _series[name] = [0, deque(maxlen=_WINDOW)]
        series[0] += 1
        series[1].append(seconds)


@contextmanager
def measure(name):
    started = perf_counter()
    try:
        yield
    finally:
        record_duration(name, perf_counter() - started)


def snapshot(names=None):
    with _lock:
        selected = _series if names is None else (name for name in names if name in _series)
        samples = [(name, _series[name][0], tuple(_series[name][1])) for name in selected]
    result = {}
    for name, count, window in samples:
        values = sorted(window)
        result[name] = {
            'count': count,
            'window_count': len(values),
            **{f'p{p}_ms': round(1000 * values[ceil(len(values) * p / 100) - 1], 3)
               for p in (50, 95, 99)},
        }
    return result


class RouteTimings:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        started = perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            route = scope.get('route')
            method = scope['method']
            if method in (getattr(route, 'methods', None) or ()):
                record_duration(f'{method} {route.path}', perf_counter() - started)
