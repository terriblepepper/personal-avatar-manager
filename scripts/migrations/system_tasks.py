"""Read and validate historical SQLite management task receipts."""
import hashlib
import json
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone

from avatar.protocols.tasks.store import TASK_COLUMNS, TASK_FIELDS, TASK_MIGRATION, initialize_system_tasks


def import_system_tasks(connection, source_path):
    """停写后在调用者提供的 psycopg 事务内导入快照；异常由调用者回滚。"""
    import sqlite3
    from psycopg.pq import TransactionStatus
    from psycopg.rows import tuple_row

    if connection.autocommit and connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ValueError('系统任务导入须使用 PostgreSQL 事务')
    path=Path(source_path).absolute()
    if path.resolve()!=path or not path.is_file():
        raise ValueError('旧系统任务账本缺失或路径不安全')
    def row_digest(row):
        encoded=json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode()
        return hashlib.sha256(encoded).digest()

    digest=hashlib.sha256()
    count=0
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source, connection.cursor(row_factory=tuple_row) as target:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        names={row[0] for row in source.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        columns=tuple(row[1] for row in source.execute('PRAGMA table_info(system_management_tasks)'))
        if names!={'system_management_tasks'} or columns!=TASK_COLUMNS or source.execute('PRAGMA user_version').fetchone()[0]!=1:
            raise ValueError('旧系统任务库版本或表结构不受支持，请先核对原库')
        initialize_system_tasks(target)
        target.execute('LOCK TABLE system_management_tasks IN SHARE ROW EXCLUSIVE MODE')
        for row in source.execute(f'SELECT {TASK_FIELDS} FROM system_management_tasks ORDER BY local_aic,peer_aic,group_id,session_key,task_id'):
            target.execute(f'INSERT INTO system_management_tasks({TASK_FIELDS}) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', row)
            copied=target.execute(f'SELECT {TASK_FIELDS} FROM system_management_tasks WHERE local_aic=%s AND peer_aic=%s AND group_id=%s AND session_key=%s AND task_id=%s',
                                  (*row[:4],row[5])).fetchone()
            source_digest=row_digest(row)
            if copied is None or row_digest(copied)!=source_digest:
                raise ValueError('系统任务回执不一致，须回滚本次导入')
            digest.update(source_digest)
            count+=1
        if target.execute('SELECT COUNT(*) FROM system_management_tasks').fetchone()[0]!=count:
            raise ValueError('系统任务行数不一致，须回滚本次导入')
        target.execute('INSERT INTO data_migrations(name,applied_at) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                       (TASK_MIGRATION,datetime.now(timezone.utc).isoformat()))
    return {'count':count,'sha256':digest.hexdigest()}
