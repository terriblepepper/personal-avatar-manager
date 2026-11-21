"""PostgreSQL adapter for the application's existing DB-API usage."""
import re
import sys
from functools import lru_cache, partial
from itertools import chain
from time import perf_counter
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from avatar.core.performance import measure, record_duration


WRITE_LOCK = 1096172626
SCHEMA_LOCK = 1096172627
ACCOUNT_WRITE_LOCK = 1096172628


def lock_schema_transaction(raw, domain, *, account_scope=None):
    # 旧版进程仍取全库排他锁；共享同一旧锁后，再按当前 schema 互斥。
    raw.execute('SELECT pg_advisory_xact_lock_shared(%s::bigint)', (domain,))
    if account_scope is None:
        raw.execute('SELECT pg_advisory_xact_lock(%s::integer, hashtext(current_schema()))', (domain,))
        return
    # Materialize the schema barrier before taking the account lock in the same round trip.
    raw.execute("""WITH barrier AS MATERIALIZED (
                     SELECT pg_advisory_xact_lock_shared(%s::integer, hashtext(current_schema()))
                   )
                   SELECT pg_advisory_xact_lock(%s::integer, hashtext(current_schema() || ':' || %s::text))
                   FROM barrier""", (domain, ACCOUNT_WRITE_LOCK, account_scope))


class Row:
    __slots__ = ("_names", "_indexes", "_values")

    def __init__(self, names, indexes, values):
        self._indexes = indexes
        self._names = names
        self._values = values

    def keys(self):
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self._indexes[key]
            except KeyError:
                raise ValueError(f"{key!r} is not in list") from None
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _tokens(sql):
    # Keep placeholders and identifiers inside quoted strings and comments untouched.
    return re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*[\s\S]*?\*/)", sql)


def _translate(sql):
    parts = _tokens(sql)
    for i in range(0, len(parts), 2):
        part = parts[i].replace('%', '%%').replace('?', '%s')
        part = re.sub(r'\bLIKE\b', 'ILIKE', part, flags=re.I)
        part = re.sub(r'\bLIMIT\s+-1\b', 'LIMIT ALL', part, flags=re.I)
        part = re.sub(r'\bMAX\s*\(\s*last_message_id\s*,', 'GREATEST(last_message_id,', part, flags=re.I)
        part = re.sub(r'\bMIN\s*\(\s*round_limit\s*,', 'LEAST(round_limit,', part, flags=re.I)
        part = re.sub(r'\bauthorization\b', '"authorization"', part, flags=re.I)
        part = re.sub(r'\browid\b', '_rowid', part, flags=re.I)
        part = re.sub(r'\bIS\s+NOT\s+(?=\')', 'IS DISTINCT FROM ', part, flags=re.I)
        parts[i] = part
    # Psycopg's param parser also sees percent signs in literal strings.
    for i in range(1, len(parts), 2):
        parts[i] = parts[i].replace('%', '%%')
    return ''.join(parts)


class Cursor:
    def __init__(self, cursor, lastrowid=None, expose_rowid=False):
        self.cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount
        columns = [column.name for column in cursor.description or ()]
        self.names = []
        positions = []
        for index, name in enumerate(columns):
            # The hidden identity is never part of SQLite's SELECT *.
            if name == '_rowid':
                if not expose_rowid:
                    continue
                name, expose_rowid = 'rowid', False
            self.names.append(name)
            positions.append(index)
        self.indexes = {}
        for index, name in enumerate(self.names):
            self.indexes.setdefault(name, index)
        self.positions = positions if len(positions) != len(columns) else None

    def _row(self, values):
        if values is None:
            return None
        if self.positions is not None:
            values = tuple(values[index] for index in self.positions)
        return Row(self.names, self.indexes, values)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return (self._row(row) for row in self.cursor)


def _compile_sql(metadata, sql):
    original = sql
    if re.search(r'\bsqlite_master\b', sql, re.I):
        sql = re.sub(r'\bsqlite_master\b',
            "(SELECT table_name AS name, 'table' AS type FROM information_schema.tables WHERE table_schema=current_schema()) AS sqlite_master",
            sql, flags=re.I)
    match = re.match(r'\s*INSERT\s+(?:(OR\s+(IGNORE|REPLACE))\s+)?INTO\s+(\w+)\s*(\([^)]*\))?', sql, re.I)
    identity = None
    if match:
        mode, table = match.group(2), match.group(3).lower()
        columns, pk, identity = metadata.get(table, ([], [], None))
        if match.group(4):
            inserted = [x.strip().strip('"') for x in match.group(4)[1:-1].split(',')]
        else:
            inserted = [c for c in columns if c != '_rowid']
            if '_rowid' in columns:
                sql = sql[:match.end(3)] + ' (' + ','.join('"'+c+'"' for c in inserted) + ')' + sql[match.end(3):]
        if mode:
            sql = re.sub(r'\bINSERT\s+OR\s+(?:IGNORE|REPLACE)\b', 'INSERT', sql, count=1, flags=re.I)
            if mode.upper() == 'IGNORE':
                sql += ' ON CONFLICT DO NOTHING'
            elif pk:
                updates = [c for c in inserted if c not in pk]
                sql += ' ON CONFLICT (' + ','.join('"'+c+'"' for c in pk) + ') DO UPDATE SET ' + ','.join('"'+c+'"=EXCLUDED."'+c+'"' for c in updates) if updates else ' ON CONFLICT DO NOTHING'
            else:
                raise ValueError('No primary key for INSERT OR REPLACE: ' + table)
        if identity and re.search(r'\bRETURNING\b', sql, re.I) is None:
            sql += ' RETURNING "' + identity + '"'
    # Preserve SQLite rowid alias while hiding SELECT * identity columns.
    sql = re.sub(r'\bSELECT\s+rowid\s*,', 'SELECT rowid AS rowid,', sql, flags=re.I)
    sql = re.sub(r"\bIS\s+NOT\s+(?=')", 'IS DISTINCT FROM ', sql, flags=re.I)
    sql = re.sub(r'\?\s+IS\s+NULL', '?::text IS NULL', sql, flags=re.I)
    translated = _translate(sql)
    expose_rowid = bool(re.search(r"\bSELECT\s+(?:\w+\.)?rowid\b", original, re.I))
    bulk_write = bool(match or re.match(r'\s*(UPDATE|DELETE)\b', original, re.I))
    bulk_write = bulk_write and not identity and not re.search(r'\bRETURNING\b', sql, re.I)
    return translated, identity, expose_rowid, bulk_write


def _params(values):
    return tuple(int(value) if isinstance(value, bool) else value for value in values)


class Connection:
    def __init__(self, raw, metadata, compile_static=None):
        self.raw = raw
        self.metadata = metadata
        self.compile_static = compile_static

    def _plan(self, sql, constants):
        # Source literals may be cached; runtime-built SQL and its literals stay request-local.
        static = len(sql) <= 8192 and sql in constants
        if static and self.compile_static is not None:
            return self.compile_static(sql), True
        return _compile_sql(self.metadata, sql), static

    def _execute(self, plan, params, static):
        translated, identity, expose_rowid, _ = plan
        cur = self.raw.execute(translated, _params(params), prepare=None if static else False)
        lastrowid = None
        if identity:
            returned = cur.fetchone()
            lastrowid = returned[0] if returned else None
        return Cursor(cur, lastrowid, expose_rowid)

    def execute(self, sql, params=()):
        plan, static = self._plan(sql, sys._getframe(1).f_code.co_consts)
        return self._execute(plan, params, static)

    def executemany(self, sql, params):
        rows = iter(params)
        sentinel = object()
        first = next(rows, sentinel)
        if first is sentinel:
            return None
        plan, static = self._plan(sql, sys._getframe(1).f_code.co_consts)
        if not plan[3] or not static or not hasattr(self.raw, "cursor"):
            for values in chain((first,), rows):
                result = self._execute(plan, values, static)
            return result
        last = first
        def before_last():
            nonlocal last
            for row in rows:
                previous, last = last, row
                yield _params(previous)

        # The caller sees the final statement's cursor and rowcount, not the batch total.
        with self.raw.cursor() as cursor:
            cursor.executemany(plan[0], before_last())
        return self._execute(plan, last, static)


class PostgresDatabase:
    def __init__(self, url):
        self.pool = ConnectionPool(url, min_size=1, max_size=10, open=True)
        self.metadata = {}
        self.compile_static = lru_cache(maxsize=512)(partial(_compile_sql, self.metadata))

    @staticmethod
    def _has_root(raw):
        users = raw.execute("SELECT to_regclass(format('%I.users',current_schema()))").fetchone()[0]
        return users is not None and raw.execute('SELECT 1 FROM users WHERE is_root=1 LIMIT 1').fetchone() is not None

    def initialize(self, *, existing_data=False):
        from avatar.core.migrations.chat_generations import restore_legacy_generations
        from avatar.core.postgres_schema import install_schema
        with self.pool.connection() as raw:
            lock_schema_transaction(raw, SCHEMA_LOCK)
            lock_schema_transaction(raw, WRITE_LOCK)
            if existing_data and not self._has_root(raw):
                raise ValueError('数据目录已有账户或旧库，须先完成离线迁移并核对 DATABASE_URL')
            install_schema(raw)
            self._load_metadata(raw)
            restore_legacy_generations(Connection(raw, self.metadata))

    def _load_metadata(self, raw):
        metadata = {}
        rows = raw.execute("""SELECT c.table_name,c.column_name,(c.is_identity = 'YES' OR c.column_default LIKE 'nextval(%'),
            EXISTS (SELECT 1 FROM information_schema.table_constraints t
              JOIN information_schema.key_column_usage k USING (constraint_catalog,constraint_schema,constraint_name)
              WHERE t.constraint_type='PRIMARY KEY' AND t.table_schema=c.table_schema
                AND t.table_name=c.table_name AND k.column_name=c.column_name)
            FROM information_schema.columns c WHERE c.table_schema=current_schema()
            ORDER BY c.table_name,c.ordinal_position""").fetchall()
        for table, column, identity, primary in rows:
            cols, pk, generated = metadata.setdefault(table, ([], [], None))
            cols.append(column)
            if primary:
                pk.append(column)
            if identity and column != '_rowid':
                metadata[table] = (cols, pk, column)
        self.metadata = metadata
        self.compile_static = lru_cache(maxsize=512)(partial(_compile_sql, metadata))

    @contextmanager
    def connect(self, write=False, *, account_scope=None):
        started = perf_counter()
        with self.pool.connection() as raw:
            record_duration('db_pool_acquire', perf_counter() - started)
            if write:
                with measure('db_write_lock'):
                    lock_schema_transaction(raw, WRITE_LOCK, account_scope=account_scope)
            yield Connection(raw, self.metadata, self.compile_static)

    def close(self):
        self.pool.close()
