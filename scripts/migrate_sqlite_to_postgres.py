"""SQLite migration preflight helpers; no target writes before checks pass."""


import os
import sqlite3
from pathlib import Path


def create_snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_file():
        raise FileNotFoundError(source)
    # Exclusive creation prevents accidental replacement and symlink traversal.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
            with sqlite3.connect(destination) as copy:
                original.backup(copy)
                if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('SQLite snapshot integrity check failed')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def normalize_value(value, declared_type):
    """Give BLOB columns one canonical Python type: SQLite may return str for TEXT stored in BLOB columns."""
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, str) and 'BLOB' in (declared_type or '').upper():
        return value.encode('utf-8')
    return value


def canonical_row(row, declared_types):
    return tuple(normalize_value(value, declared_types[index]) for index, value in enumerate(row))


def validate_source(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('SQLite foreign key check failed')


def dependency_order(db, tables):
    remaining = {table: {row[2] for row in db.execute('PRAGMA foreign_key_list(' + _quote(table) + ')')
                         if row[2] != table} for table in tables}
    result = []
    while remaining:
        ready = sorted(table for table, dependencies in remaining.items()
                       if not dependencies.intersection(remaining))
        if not ready:
            raise ValueError('SQLite foreign key dependency cycle requires manual migration')
        result.extend(ready)
        for table in ready:
            del remaining[table]
    return result


def _check_expressions(ddl):
    import re
    result = []
    for match in re.finditer(r"\bCHECK\s*\(", ddl, re.I):
        depth, quoted, i = 1, False, match.end()
        start = i
        while i < len(ddl) and depth:
            if ddl[i] == "'":
                if quoted and i + 1 < len(ddl) and ddl[i + 1] == "'":
                    i += 2
                    continue
                quoted = not quoted
            elif not quoted:
                if ddl[i] == '(': depth += 1
                elif ddl[i] == ')': depth -= 1
            i += 1
        if depth:
            raise ValueError('invalid SQLite schema check')
        result.append(' '.join(ddl[start:i-1].split()))
    return sorted(result)


def _schema_metadata(db):
    entries = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                         "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'rag_lexical_fts_%'").fetchall()
    tables, others = {}, {}
    for kind, name, ddl in entries:
        if kind == 'table':
            columns = sorted(tuple(row[1:]) for row in db.execute('PRAGMA table_info(' + _quote(name) + ')'))
            foreign_keys = sorted(tuple(row[2:]) for row in db.execute('PRAGMA foreign_key_list(' + _quote(name) + ')'))
            indexes = []
            for idx in db.execute('PRAGMA index_list(' + _quote(name) + ')').fetchall():
                _, index_name, unique, origin, partial = idx
                index_columns = tuple(tuple(row[2:]) for row in db.execute('PRAGMA index_xinfo(' + _quote(index_name) + ')'))
                indexes.append((unique, origin, partial, index_columns))
            tables[name] = (columns, foreign_keys, sorted(indexes, key=repr), _check_expressions(ddl))
        elif kind in ('index', 'trigger'):
            others[name] = (kind, ' '.join(ddl.split()))
    return tables, others


def validate_schema(db):
    from avatar.core.postgres_schema_manifest import EXPECTED
    tables, others = _schema_metadata(db)
    expected_tables, expected_others = EXPECTED
    present = set(tables)
    # These tables have no source rows before their feature is installed.
    for optional in ('protocol_message_versions', 'agent_request_receipts', 'agent_wire_receipts'):
        tables.setdefault(optional, expected_tables[optional])
    if 'chat_generation_jobs' not in present:
        # 引入持久任务前只有 chat_pending；缺少的对象由目标 schema 创建。
        tables['chat_generation_jobs'] = expected_tables['chat_generation_jobs']
        for name in ('chat_generation_active','chat_generation_queue','chat_generation_forget'):
            others.setdefault(name, expected_others[name])
    if 'knowledge_assets' in present:
        columns = tables['knowledge_assets'][0]
        if not any(column[0] == 'source_attachment_id' for column in columns):
            expected_source = next(column for column in expected_tables['knowledge_assets'][0] if column[0] == 'source_attachment_id')
            columns.append(expected_source)
            columns.sort()
    if (tables, others) != EXPECTED:
        different = sorted(name for name in set(tables) | set(expected_tables)
                           if tables.get(name) != expected_tables.get(name))
        different += sorted(name for name in set(others) | set(expected_others)
                            if others.get(name) != expected_others.get(name))
        raise ValueError('SQLite schema mismatch: ' + ', '.join(different))
    return present


def migrate(source, backup, target_url, *, dry_run=False):
    """Copy a verified snapshot into an empty target, committing only after verification."""
    import hashlib
    import sqlite3
    import psycopg
    from psycopg import sql
    from avatar.core.migrations.chat_generations import restore_legacy_generations
    from avatar.core.migrations.soul import restore_asset_sources
    from avatar.core.postgres import Connection, SCHEMA_LOCK, lock_schema_transaction
    from avatar.core.postgres_helpers import STATEMENTS as HELPER_STATEMENTS
    from avatar.core.postgres_schema import SCHEMA_COUNT, STATEMENTS, TABLES

    create_snapshot(source, backup)
    with sqlite3.connect(backup.resolve().as_uri() + '?mode=ro', uri=True) as src:
        validate_source(src)
        present = validate_schema(src)
        order = dependency_order(src, [table for table in TABLES if table in present])
        sequence_limits = dict(src.execute("SELECT name,seq FROM sqlite_sequence"))
        with psycopg.connect(target_url) as dst:
            with dst.transaction():
                lock_schema_transaction(dst, SCHEMA_LOCK)
                objects = dst.execute("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','S','f')""").fetchone()[0]
                functions = dst.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname=current_schema()""").fetchone()[0]
                if objects or functions:
                    raise ValueError('target schema is not empty')
                counts = {name: src.execute('SELECT count(*) FROM ' + _quote(name)).fetchone()[0] for name in order}
                if dry_run:
                    return counts
                for statement in STATEMENTS[:SCHEMA_COUNT]:
                    dst.execute(statement)
                missing_asset_sources = False
                for name in order:
                    info = src.execute('PRAGMA table_info(' + _quote(name) + ')').fetchall()
                    columns = [row[1] for row in info]
                    if name == 'knowledge_assets':
                        missing_asset_sources = 'source_attachment_id' not in columns
                    declared_types = [row[2] or '' for row in info]
                    serials = dst.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_schema=current_schema() AND table_name=%s
                          AND (is_identity='YES' OR column_default LIKE 'nextval(%%')""", (name,)).fetchall()
                    source_columns = columns.copy()
                    if ('_rowid',) in serials:
                        # 会话协议按历史 rowid 判定首轮和末轮；迁移后保持旧值及下一次分配顺序。
                        source_columns.append('rowid')
                        columns.append('_rowid')
                        declared_types.append('INTEGER')
                    select = 'SELECT ' + ','.join(_quote(c) for c in source_columns) + ' FROM ' + _quote(name)
                    insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                        sql.Identifier(name), sql.SQL(',').join(map(sql.Identifier, columns)),
                        sql.SQL(',').join(sql.Placeholder() for _ in columns))
                    with dst.cursor() as cursor:
                        rows = src.execute(select)
                        while batch := rows.fetchmany(500):
                            cursor.executemany(insert, [canonical_row(row, declared_types) for row in batch])
                    for (column,) in serials:
                        sequence = dst.execute('SELECT pg_get_serial_sequence(%s,%s)', (name, column)).fetchone()[0]
                        maximum = dst.execute(sql.SQL('SELECT max({}) FROM {}').format(sql.Identifier(column), sql.Identifier(name))).fetchone()[0]
                        high_water = max(maximum or 0, sequence_limits.get(name, 0) if column != '_rowid' else 0)
                        dst.execute('SELECT setval(%s, %s, %s)', (sequence, max(1, high_water), high_water > 0))
                    def digest(rows):
                        return sorted(hashlib.sha256(repr(canonical_row(row, declared_types)).encode()).digest() for row in rows)
                    dest_rows = dst.execute(sql.SQL('SELECT {} FROM {}').format(
                        sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(name)))
                    if digest(src.execute(select)) != digest(dest_rows):
                        raise ValueError('migrated row contents differ for table ' + name)
                for statement in [*STATEMENTS[SCHEMA_COUNT:], *HELPER_STATEMENTS]:
                    dst.execute(statement)
                # 先核验所有源列的摘要，再从已验证的记录恢复历史来源和任务。
                if missing_asset_sources:
                    restore_asset_sources(dst)
                restore_legacy_generations(Connection(dst, {}))
                return counts


def main():
    import argparse
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Migrate a verified SQLite snapshot into an empty PostgreSQL schema.')
    parser.add_argument('source', type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    target = os.environ.get('DATABASE_URL')
    if not target:
        parser.error('DATABASE_URL is required')
    try:
        counts = migrate(args.source, args.backup, target, dry_run=args.dry_run)
    except Exception as exc:
        # Driver errors can include SQL values or connection details; do not expose them.
        print('Migration failed:', type(exc).__name__)
        return 1
    for table, count in counts.items():
        print(f'{table}: {count}')
    print('Preflight passed' if args.dry_run else 'Migration committed. Source rows verified before restoring legacy references and chat jobs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
