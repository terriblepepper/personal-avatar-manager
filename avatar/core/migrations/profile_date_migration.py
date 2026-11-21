# 模块说明：以新增日期表接管个人资料业务；旧表完整保留，只在一次性复制时读取，迁移不删除表、字段或历史内容。
from datetime import datetime, timezone


# 职责：将历史日期规范化为含时区的修改时间。
# 输入：value 为旧库日期；允许旧日期仅含年月日。
# 结果与约束：返回 UTC ISO 8601；缺失或损坏日期采用迁移时刻，新表使用规范值而旧记录保持原样。
def normalize_date(value):
    try:
        parsed = datetime.fromisoformat(value or '')
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc).isoformat(timespec='microseconds') if parsed.tzinfo is None else parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')


# 职责：复制一条历史业务记录到日期表。
# 输入：db 为初始化事务，table 为模块内固定表名，values 为已转换字段字典。
# 结果与约束：仅写入新表已有字段，历史数字字段不会进入新表；冲突抛错使整次复制回滚，不覆盖已有新记录。
def copy_record(db, table, values):
    allowed = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
    fields = [key for key in values if key in allowed]
    db.execute(f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", [values[key] for key in fields])


# 职责：一次性将旧资料、审核、分身、任务、会话及审计复制到日期业务表。
# 输入：db 为已建好新表的初始化事务；旧表只读，数字字段仅在迁移中用于匹配历史目标。
# 结果与约束：保留所有原表、原字段和原内容，日期恢复自编辑事件；访客消息映射目标分身日期，未知旧记忆保留原文但不参与当前回答，完成标记防止重复复制。
def migrate_personal_dates(db):
    db.execute('CREATE TABLE IF NOT EXISTS data_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    if db.execute("SELECT 1 FROM data_migrations WHERE name='personal_profile_dates'").fetchone():
        return
    tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    old = {table: [dict(row) for row in db.execute(f'SELECT * FROM {table}')]
           if table in tables else [] for table in ('profiles', 'submissions', 'agents', 'jobs', 'conversations', 'messages', 'events')}
    edits = {}
    for item in old['events']:
        if item['action'] in ('保存资料草稿', '上传资料附件', '移除资料附件'):
            edits.setdefault(item['user_id'], []).append((item['id'], item['created_at']))
    mapping = {}
    for item in old['profiles']:
        events = edits.get(item['user_id'], [])
        edited = max(events)[1] if events else item['updated_at']
        copy_record(db, 'profile_records', {**item, 'last_modified_at': normalize_date(edited)})
    submissions = []
    for item in old['submissions']:
        events = [event for event in edits.get(item['user_id'], []) if event[1] <= item['submitted_at']]
        modified = normalize_date(max(events)[1] if events else item['submitted_at'])
        mapping[(item['user_id'], item['version'])] = modified
        record = {**item, 'last_modified_at': modified}
        copy_record(db, 'profile_submissions', record)
        submissions.append(record)
    agents = {}
    for item in old['agents']:
        modified = mapping.get((item['user_id'], item['version']), normalize_date(item['updated_at']))
        mapping.setdefault((item['user_id'], item['version']), modified)
        copy_record(db, 'avatar_records', {**item, 'last_modified_at': modified})
        agents[item['aic']] = item['user_id']
    for item in old['jobs']:
        copy_record(db, 'provision_jobs', item)
    conversations = {item['id']: item for item in old['conversations']}
    for item in old['conversations']:
        owner = agents.get(item['target_aic'], item['user_id'] if item['channel'] == 'avatar' else None)
        modified = mapping.get((owner, item.get('summary_version', 0)), '')
        copy_record(db, 'chat_conversations', {**item, 'summary_profile_modified_at': modified})
    for item in old['messages']:
        conversation = conversations.get(item.get('conversation_id'), {})
        target = item.get('responder_aic') or conversation.get('target_aic')
        owner = agents.get(target, item['user_id'] if item['channel'] == 'avatar' else None)
        modified = mapping.get((owner, item.get('profile_version', 0)), '')
        copy_record(db, 'chat_messages', {**item, 'profile_modified_at': modified})
    for item in old['events']:
        detail = item.get('detail', '')
        if item['action'] in ('提交创建审核', '提交更新审核') and detail.startswith('资料版本 '):
            snapshots = [row for row in submissions if row['user_id'] == item['user_id'] and row['submitted_at'] <= item['created_at']]
            modified = max(snapshots, key=lambda row: row['submitted_at'])['last_modified_at'] if snapshots else normalize_date(item['created_at'])
            detail = '资料修改于 ' + modified
        copy_record(db, 'audit_events', {**item, 'detail': detail})
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('日期表复制后外键检查失败，初始化事务将回滚')
    db.execute("INSERT INTO data_migrations(name,applied_at) VALUES('personal_profile_dates',?)", (normalize_date(None),))
