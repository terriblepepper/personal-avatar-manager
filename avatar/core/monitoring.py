"""Shared dashboard samples and seven days of platform-level HTTP aggregates."""
import asyncio
from collections import Counter, deque
import logging
from datetime import datetime, timezone
from time import time

import psutil
from psycopg import Error
from psycopg.types.json import Jsonb
from redis.exceptions import RedisError

from avatar.core.execution import run_blocking
from avatar.core.http_metrics import COUNTERS, HISTORY_MINUTES, HttpMetrics
from avatar.core.performance import snapshot as timings_snapshot


WINDOWS = {'1h': (60, 1), '24h': (1440, 10), '7d': (10080, 60)}
QUEUES = {
    'chat': ('chat_generation_jobs', 'state'),
    'peer': ('agent_dialogues', 'status'),
    'controls': ('agent_protocol_controls', 'status'),
    'index': ('file_rag_documents', 'status'),
    'soul': ('soul_builds', 'status'),
    'provision': ('provision_jobs', 'status'),
    'identity': ('avatar_identity_jobs', 'status'),
}
QUEUE_COUNTS = " UNION ALL ".join(
    f"SELECT '{key}', count(*) FILTER (WHERE {column}='queued'), "
    f"count(*) FILTER (WHERE {column}='running') FROM {table} WHERE {column} IN ('queued','running')"
    for key, (table, column) in QUEUES.items())
TIMINGS = ('event_loop_lag', 'db_pool_acquire', 'db_write_lock', 'dispatch_poll', 'document_image_process_start')


def iso_stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class PlatformMonitor:
    def __init__(self, database, changes, redis):
        self.database = database
        self.changes = changes
        self.redis = redis
        self.http = HttpMetrics()
        self.process = psutil.Process()
        self.history = deque(maxlen=HISTORY_MINUTES)
        self.series_cache = {}
        self.loaded = False
        self.pending_until = int(self.http.started_at // 60) - 1
        self.services = None

    def _rollup(self, rows, minute, restore):
        with self.database.pool.connection(timeout=.75) as connection:
            connection.execute("SET LOCAL statement_timeout='1s'")
            history = connection.execute(
                'SELECT minute, metrics FROM ops_metric_minutes WHERE minute >= %s ORDER BY minute',
                (minute - HISTORY_MINUTES,)).fetchall() if restore else ()
            if rows:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                        INSERT INTO ops_metric_minutes(minute,metrics) VALUES (%s,%s)
                        ON CONFLICT(minute) DO UPDATE SET metrics=EXCLUDED.metrics
                    """, [(key, Jsonb(metrics)) for key, metrics in rows])
                connection.execute('DELETE FROM ops_metric_minutes WHERE minute < %s',
                                   (minute - HISTORY_MINUTES,))
        return history

    def _postgres(self):
        pool_stats = self.database.pool.get_stats()
        pool = {key: pool_stats.get(key, 0) for key in
                ('pool_size', 'pool_available', 'requests_waiting', 'requests_queued', 'requests_errors')}
        try:
            with self.database.pool.connection(timeout=.75) as connection:
                connection.execute("SET LOCAL statement_timeout='1s'")
                backends, commits, rollbacks, deadlocks = connection.execute("""
                    SELECT numbackends,xact_commit,xact_rollback,deadlocks
                    FROM pg_stat_database WHERE datname=current_database()
                """).fetchone()
                waiting = connection.execute("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname=current_database() AND wait_event_type='Lock'
                """).fetchone()[0]
                pending = connection.execute("SELECT count(*) FROM avatar_dispatch_events").fetchone()[0]
                queues = {key: {'queued': queued, 'running': running}
                          for key, queued, running in connection.execute(QUEUE_COUNTS)}
            return ({'state': 'ready', 'pool': pool, 'connections': backends, 'commits': commits,
                     'rollbacks': rollbacks, 'deadlocks': deadlocks, 'lock_waits': waiting}, queues, pending)
        except (Error, OSError):
            return ({'state': 'unavailable', 'pool': pool}, None, None)

    def _redis(self):
        if self.redis is None:
            return {'state': 'disabled'}
        try:
            with self.redis.pipeline(transaction=False) as pipe:
                pipe.info('stats')
                pipe.info('memory')
                stats, memory = pipe.execute()
            return {'state': 'ready', 'hits': stats['keyspace_hits'],
                    'misses': stats['keyspace_misses'], 'evicted': stats['evicted_keys'],
                    'memory_bytes': memory['used_memory'], 'limit_bytes': memory['maxmemory']}
        except (RedisError, OSError, KeyError):
            return {'state': 'unavailable'}

    def _resources(self, health):
        postgres, queues, pending = self._postgres()
        try:
            process = {'cpu_percent': self.process.cpu_percent(), 'memory_bytes': self.process.memory_info().rss,
                       'threads': self.process.num_threads()}
        except psutil.Error:
            process = None
        result = {
            'sampled_at': iso_stamp(time()), 'process': process,
            'postgres': postgres, 'redis': self._redis(),
            'kafka': {'state': 'ready' if health['kafka'] else
                      ('unavailable' if self.changes.kafka_bootstrap else 'disabled'),
                      'relay_pending': pending},
            'queues': queues,
        }
        return result

    async def persist_completed(self):
        minute = int(time() // 60)
        self.http.at(minute)
        rows = [(key, sample.totals()) for key, sample in sorted(self.http.minutes.items())
                if self.pending_until < key < minute]
        if rows or not self.loaded:
            try:
                previous = await run_blocking(self._rollup, rows, minute, not self.loaded)
            except (Error, OSError):
                pass
            else:
                if previous:
                    # The upsert and the in-memory history must select the same minute.
                    replacing = {key for key, _ in rows}
                    self.history.extend(row for row in previous if row[0] not in replacing)
                self.history.extend(rows)
                if rows:
                    self.pending_until = rows[-1][0]
                self.loaded = True
    async def refresh(self):
        await self.persist_completed()
        self.services = await run_blocking(self._resources, dict(self.changes.health))

    async def run(self):
        try:
            while True:
                try:
                    await self.refresh()
                except Exception as error:
                    logging.getLogger(__name__).warning('Platform sample unavailable (%s)', type(error).__name__)
                await asyncio.sleep(15)
        finally:
            await self.persist_completed()

    @staticmethod
    def _group_series(history, current, minutes, step):
        lower = current - minutes
        groups = {}
        for key, metrics in history:
            if lower <= key < current:
                group = groups.setdefault(key // step * step, Counter())
                group.update({metric: metrics[metric] for metric in COUNTERS})
                group['sample_minutes'] += 1
        return [{'time': iso_stamp(key * 60), 'sample_minutes': row['sample_minutes'],
                 **{metric: row[metric] for metric in COUNTERS}}
                for key, row in sorted(groups.items())], step

    def read_minutes(self, start, count):
        lower = max(start, int(time() // 60) - HISTORY_MINUTES)
        return [{'time': iso_stamp(minute * 60), 'sample_minutes': 1, **metrics}
                for minute, metrics in self.history if lower <= minute < start + count]

    async def read(self, selection):
        current = int(time() // 60)
        stamp = (current, self.pending_until, self.loaded)
        cached = self.series_cache.get(selection)
        if cached is None or cached[0] != stamp:
            history = tuple(self.history)
            minutes, step = WINDOWS[selection]
            cached = stamp, await asyncio.to_thread(self._group_series, history, current, minutes, step)
            self.series_cache[selection] = cached
        history, bucket = cached[1]
        timers = timings_snapshot(TIMINGS)
        http = self.http.recent()
        return {'now': iso_stamp(time()), 'started_at': iso_stamp(self.http.started_at),
                'http': http, 'series': history, 'bucket_minutes': bucket,
                'history_ready': self.loaded, 'services': self.services,
                'timings': timers}
