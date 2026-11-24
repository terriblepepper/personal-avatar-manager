"""Bridge committed database changes to job wake-ups and authorized UI refreshes."""
import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from contextlib import contextmanager

from psycopg import AsyncConnection, Error
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.asyncio.retry import Retry

from avatar.core.kafka import KafkaWakeups
from avatar.core.performance import record_duration

logger = logging.getLogger(__name__)
QUEUE_NAMES = ('chat', 'peer', 'controls', 'index', 'soul', 'identity', 'provision')
QUEUE_BYTES = frozenset(name.encode('ascii') for name in QUEUE_NAMES)


class Subscription(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.changed = set()

    def mark(self, topic):
        self.changed.add(topic)
        self.set()

    def take(self):
        changed = self.changed
        self.changed = set()
        self.clear()
        return changed


class Changes:
    def __init__(self, database, redis_socket, kafka_bootstrap, invalidate_reviews):
        self.dsn = database.pool.conninfo
        self.redis = Redis(unix_socket_path=redis_socket, socket_connect_timeout=.2,
                           socket_timeout=2, retry=Retry(NoBackoff(), 0)) if redis_socket else None
        self.kafka_bootstrap = kafka_bootstrap
        self.invalidate_reviews = invalidate_reviews
        self.watchers = defaultdict(set)
        self.work = {key: asyncio.Event() for key in QUEUE_NAMES}
        self.health = {'postgres': False, 'redis': False, 'kafka': False}
        self.namespace = None
        self.kafka = None
        self.loop = None

    def mark(self, key):
        for event in self.watchers.get(key, ()):
            event.mark(key[0])

    def generation_committed(self, user_id, request_id):
        if loop := self.loop:
            loop.call_soon_threadsafe(self.mark, ('generation', user_id, request_id))

    @property
    def can_push(self):
        return self.health['postgres'] and self.health['redis']

    @property
    def can_dispatch(self):
        return self.health['postgres'] and self.health['kafka']

    def set_ready(self, component, ready):
        if self.health[component] == ready:
            return
        self.health[component] = ready
        for events in self.watchers.values():
            for event in events:
                event.set()
        for event in self.work.values():
            event.set()

    def wake(self, key):
        if key in self.work:
            self.work[key].set()

    @contextmanager
    def watch(self, *keys):
        event = Subscription()
        for key in keys:
            self.watchers[key].add(event)
        try:
            yield event
        finally:
            for key in keys:
                self.watchers[key].discard(event)
                if not self.watchers[key]:
                    del self.watchers[key]

    async def _forward(self, payloads):
        updates = []
        deliveries = []
        for payload in payloads:
            try:
                topic, *keys = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if topic == 'work' and len(keys) == 1 and isinstance(keys[0], str) and keys[0] in self.work:
                if self.kafka and self.can_dispatch:
                    deliveries.append(self.kafka.publish(keys[0]))
                else:
                    self.wake(keys[0])
            elif topic in ('inbox', 'dialogue', 'generation', 'reviews') and all(keys):
                updates.append(payload)
        if updates and self.redis is not None:
            try:
                async with self.redis.pipeline(transaction=False) as pipe:
                    for payload in updates:
                        pipe.publish(self.namespace, payload)
                    await pipe.execute()
            except RedisError:
                self.set_ready('redis', False)
        if deliveries:
            await asyncio.gather(*deliveries)

    async def _listen_postgres(self):
        async with await AsyncConnection.connect(self.dsn, autocommit=True, connect_timeout=2) as conn:
            db, schema, locked = await (await conn.execute("""
                SELECT current_database(), current_schema(),
                    pg_try_advisory_lock(hashtextextended('avatar.dispatch:' || current_schema(), 0))
            """)).fetchone()
            if not locked:
                await asyncio.sleep(2)
                return
            self.namespace = 'avatar.v1.' + hashlib.sha256(
                json.dumps((conn.info.host, conn.info.port, db, schema)).encode()).hexdigest()[:32]
            self.set_ready('postgres', True)
            while True:
                loop = asyncio.get_running_loop()
                start = loop.time()
                rows = await (await conn.execute(
                    'SELECT id, payload FROM avatar_dispatch_events ORDER BY id LIMIT 128')).fetchall()
                record_duration('dispatch_poll', loop.time() - start)
                if not rows:
                    await asyncio.sleep(.2)
                    continue
                await self._forward(dict.fromkeys(payload for _, payload in rows))
                # Each business ledger is independently scanned if its wake-up is lost.
                await conn.execute('DELETE FROM avatar_dispatch_events WHERE id=ANY(%s)',
                                   ([identifier for identifier, _ in rows],))

    async def _listen_redis(self):
        while self.namespace is None:
            await asyncio.sleep(.1)
        async with self.redis.pubsub() as sub:
            await sub.subscribe(self.namespace)
            while True:
                message = await sub.get_message(timeout=1.)
                if message is None:
                    if not self.health['redis']:
                        await self.redis.ping()
                        self.set_ready('redis', True)
                    continue
                if message['type'] == 'subscribe':
                    self.set_ready('redis', True)
                    continue
                try:
                    key = tuple(json.loads(message['data']))
                    if not key or any(not isinstance(part, str) for part in key):
                        continue
                except (ValueError, TypeError):
                    continue
                if key == ('reviews',):
                    await asyncio.to_thread(self.invalidate_reviews)
                self.mark(key)

    async def _listen_kafka(self):
        while self.namespace is None:
            await asyncio.sleep(.1)
        try:
            self.kafka = KafkaWakeups(self.kafka_bootstrap, self.namespace, QUEUE_BYTES, self.wake,
                                      lambda ready: self.set_ready('kafka', ready))
        except Exception as error:
            logger.warning('Kafka client unavailable (%s)', type(error).__name__)
            return
        try:
            await self.kafka.run()
        finally:
            self.kafka = None

    async def _retry(self, name, run):
        while True:
            try:
                await run()
            except (Error, RedisError, OSError) as error:
                logger.warning('Change listener %s reconnecting (%s)', name, type(error).__name__)
            finally:
                self.set_ready(name, False)
            await asyncio.sleep(1.)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._retry('postgres', self._listen_postgres))
                if self.redis is not None:
                    group.create_task(self._retry('redis', self._listen_redis))
                if self.kafka_bootstrap:
                    group.create_task(self._retry('kafka', self._listen_kafka))
        finally:
            self.loop = None
            if self.redis is not None:
                await self.redis.aclose()


async def wait_change(event, timeout):
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except TimeoutError:
        pass


def begin_work(database, key):
    changes = getattr(database, 'changes', None)
    event = changes.work[key] if changes is not None else None
    if event is not None:
        event.clear()
    return changes, event


async def wait_work(latch, fallback, *, reconcile=5.):
    changes, event = latch
    if changes is None or not changes.kafka_bootstrap:
        await asyncio.sleep(fallback)
    else:
        await wait_change(event, reconcile if changes.can_dispatch else fallback)
