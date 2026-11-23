"""资料和 Soul 的待审角标快照。审核操作始终使用业务数据库。"""
import hashlib
import json
from threading import Lock

from redis import Redis, RedisError
from redis.backoff import NoBackoff
from redis.retry import Retry


def pending_count(db):
    return db.execute(
        "SELECT (SELECT count(*) FROM profile_submissions WHERE status='pending')"
        "+(SELECT count(*) FROM soul_builds WHERE status='pending')"
    ).fetchone()[0]


def _cache_key(database):
    with database.connect() as db:
        info = db.raw.info
        projection = db.execute(
            "SELECT current_database(), current_user, current_setting('search_path'), current_schemas(true)::text"
        ).fetchone()
        namespace = ('postgres', info.host, info.port, *projection)
    digest = hashlib.sha256(json.dumps(namespace).encode()).hexdigest()
    return f'avatar:v1:{digest}:reviews:pending'


class PendingReviews:
    ttl = 5

    def __init__(self, database, socket_path):
        self.fill_lock = Lock()
        self.database = database
        self.key = _cache_key(database) if socket_path else None
        self.client = Redis(unix_socket_path=socket_path, socket_timeout=0.1,
                            retry=Retry(NoBackoff(), 0), max_connections=16) if socket_path else None

    def _read(self):
        with self.database.connect() as db:
            return pending_count(db)

    def get(self):
        if self.client is None:
            return self._read()
        value = None
        try:
            raw = self.client.get(self.key)
            if raw is not None and raw.isdigit():
                return int(raw)
            with self.fill_lock, self.client.pipeline() as pipe:
                pipe.watch(self.key)
                raw = pipe.get(self.key)
                if raw is not None and raw.isdigit():
                    return int(raw)
                value = self._read()
                pipe.multi()
                pipe.set(self.key, value, ex=self.ttl)
                pipe.execute()
        except RedisError:
            # A failed fill or concurrent invalidation cannot undo the SQL read.
            if value is None:
                return self._read()
        return value

    def invalidate(self):
        if self.client is not None:
            try:
                # Writing a marker also invalidates a WATCH on an initially absent key.
                self.client.set(self.key, b'-', ex=self.ttl)
            except RedisError:
                pass

    def close(self):
        if self.client is not None:
            self.client.close()
