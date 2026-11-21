"""将临时协商增量升级为分身配对会话、会话内任务和双方独立记忆，不删除旧原文。"""
import uuid


# 配对使用稳定账户 ID，正式 AIC 更换不创建重复会话；旧任务只补关联并暂停，不擅自计费。
def migrate_peer_sessions(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS agent_peer_sessions (
      id TEXT PRIMARY KEY, left_user_id TEXT NOT NULL REFERENCES users(id),
      right_user_id TEXT NOT NULL REFERENCES users(id), creator_id TEXT NOT NULL REFERENCES users(id),
      accepted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(left_user_id,right_user_id), CHECK(left_user_id<right_user_id));
    CREATE TABLE IF NOT EXISTS agent_session_members (
      session_id TEXT NOT NULL REFERENCES agent_peer_sessions(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), hidden INTEGER NOT NULL DEFAULT 0,
      cleared_through INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
      summary_through INTEGER NOT NULL DEFAULT 0, source_conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL,
      PRIMARY KEY(session_id,user_id));
    CREATE TABLE IF NOT EXISTS agent_dialogue_preferences (
      user_id TEXT PRIMARY KEY REFERENCES users(id), max_rounds INTEGER NOT NULL DEFAULT 10 CHECK(max_rounds BETWEEN 1 AND 1000));
    CREATE TABLE IF NOT EXISTS agent_request_receipts (
      user_id TEXT NOT NULL REFERENCES users(id), scope TEXT NOT NULL, request_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      PRIMARY KEY(user_id,scope,request_id));
    CREATE TABLE IF NOT EXISTS agent_pending_inputs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), request_id TEXT NOT NULL, message TEXT NOT NULL,
      applied INTEGER NOT NULL DEFAULT 0, UNIQUE(task_id,user_id,request_id));
    CREATE TABLE IF NOT EXISTS agent_task_memories (
      user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES agent_peer_sessions(id) ON DELETE CASCADE,
      task_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(user_id,task_id));
    CREATE TABLE IF NOT EXISTS agent_notifications (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
      session_id TEXT, task_id TEXT, title TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
      read_at TEXT, deleted_at TEXT, dedupe_key TEXT NOT NULL, UNIQUE(user_id,dedupe_key));
    CREATE INDEX IF NOT EXISTS agent_notifications_inbox ON agent_notifications(user_id,deleted_at,read_at,id);
    CREATE TABLE IF NOT EXISTS agent_activity_cursors (
      user_id TEXT PRIMARY KEY REFERENCES users(id), last_event INTEGER NOT NULL DEFAULT 0,
      admin INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS agent_intents (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
      document TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', task_id TEXT);
    CREATE TABLE IF NOT EXISTS agent_protocol_controls (
      task_id TEXT PRIMARY KEY REFERENCES agent_dialogues(id) ON DELETE CASCADE, action TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued', next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS agent_wire_receipts (
      local_aic TEXT NOT NULL, peer_aic TEXT NOT NULL, command_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL, document TEXT, PRIMARY KEY(local_aic,peer_aic,command_id));
    CREATE TABLE IF NOT EXISTS agent_wire_results (
      command_id TEXT NOT NULL, local_aic TEXT NOT NULL, peer_aic TEXT NOT NULL,
      session_id TEXT NOT NULL, task_id TEXT NOT NULL, state TEXT NOT NULL, document TEXT NOT NULL,
      PRIMARY KEY(local_aic,peer_aic,command_id));
    ''')
    columns={r['name'] for r in db.execute('PRAGMA table_info(agent_dialogues)')}
    for name,definition in [('session_id','TEXT REFERENCES agent_peer_sessions(id)'),('round_limit','INTEGER NOT NULL DEFAULT 10'),
                            ('rounds_done','INTEGER NOT NULL DEFAULT 0'),('task_kind',"TEXT NOT NULL DEFAULT 'chat'"),('shared_content',"TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:db.execute(f'ALTER TABLE agent_dialogues ADD COLUMN {name} {definition}')
    for table, additions in {
        'agent_notifications': [('category', "TEXT NOT NULL DEFAULT 'chat'"), ('target_page', "TEXT NOT NULL DEFAULT ''"), ('admin_only', 'INTEGER NOT NULL DEFAULT 0')],
        'agent_pending_inputs': [('skill_commands', "TEXT NOT NULL DEFAULT ''"), ('removed_skills', "TEXT NOT NULL DEFAULT '[]'")],
        'agent_dialogues': [('initiator_excluded_skills', "TEXT NOT NULL DEFAULT '[]'"), ('target_excluded_skills', "TEXT NOT NULL DEFAULT '[]'"), ('delivery_mode', "TEXT NOT NULL DEFAULT 'negotiation'")],
        'agent_intents': [('message_id', 'INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL')],
        'agent_dialogue_messages': [('transport_mode', "TEXT NOT NULL DEFAULT ''")],
        'agent_session_members': [('cleared_at', "TEXT NOT NULL DEFAULT ''")],
    }.items():
        existing={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        for name, definition in additions:
            if name not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    if 'transport_mode' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_dialogues)')}:
        db.execute("ALTER TABLE agent_dialogues ADD COLUMN transport_mode TEXT NOT NULL DEFAULT ''")
    db.execute("UPDATE agent_session_members SET cleared_at=datetime('now') WHERE cleared_at='' AND cleared_through>0")
    db.execute("UPDATE agent_notifications SET category='request' WHERE kind='request' AND category='chat'")
    # 移除旧版本误投递的他人登录提醒；本人消息和原始审计均保留，不回退动态游标。
    db.execute("""DELETE FROM agent_notifications AS n WHERE n.kind='activity' AND EXISTS (
      SELECT 1 FROM audit_events e WHERE n.dedupe_key='event:'||e.id
      AND e.action='统一认证登录' AND e.user_id IS NOT n.user_id)""")
    for row in db.execute('SELECT * FROM agent_dialogues WHERE session_id IS NULL ORDER BY created_at,id').fetchall():
        left,right=sorted((row['user_id'],row['target_user_id']))
        pair=db.execute('SELECT id FROM agent_peer_sessions WHERE left_user_id=? AND right_user_id=?',(left,right)).fetchone()
        identifier=pair['id'] if pair else str(uuid.uuid4())
        if not pair:db.execute('INSERT INTO agent_peer_sessions VALUES(?,?,?,?,0,?,?)',(identifier,left,right,row['user_id'],row['created_at'],row['updated_at']))
        for user in (left,right):db.execute('INSERT OR IGNORE INTO agent_session_members(session_id,user_id) VALUES(?,?)',(identifier,user))
        db.execute("UPDATE agent_dialogues SET session_id=?,status=CASE WHEN status IN ('ended','failed') THEN status ELSE 'paused' END,expires_at=NULL WHERE id=?",(identifier,row['id']))
    if 'source_message_id' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_task_memories)')}:
        db.execute('ALTER TABLE agent_task_memories ADD COLUMN source_message_id INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL')
    db.execute('CREATE INDEX IF NOT EXISTS agent_dialogues_session ON agent_dialogues(session_id,created_at,id)')
    db.executescript('''CREATE TRIGGER IF NOT EXISTS agent_session_member_insert BEFORE INSERT ON agent_session_members
      WHEN NOT EXISTS(SELECT 1 FROM agent_peer_sessions WHERE id=NEW.session_id AND NEW.user_id IN (left_user_id,right_user_id))
      BEGIN SELECT RAISE(ABORT,'session member mismatch'); END;
    CREATE TRIGGER IF NOT EXISTS agent_session_member_immutable BEFORE UPDATE OF session_id,user_id ON agent_session_members
      BEGIN SELECT RAISE(ABORT,'session member immutable'); END;''')

    # AIC 正式升级只允许同步到该稳定账户当前的身份；双方账户归属仍不可更改。
    db.executescript('''
    DROP TRIGGER IF EXISTS dialogue_participants_immutable;
    CREATE TRIGGER dialogue_participants_immutable BEFORE UPDATE OF user_id,own_aic,target_user_id,target_aic ON agent_dialogues
      WHEN NEW.user_id IS NOT OLD.user_id OR NEW.target_user_id IS NOT OLD.target_user_id
        OR (NEW.own_aic IS NOT OLD.own_aic AND NOT EXISTS(SELECT 1 FROM avatar_records WHERE user_id=NEW.user_id AND aic=NEW.own_aic))
        OR (NEW.target_aic IS NOT OLD.target_aic AND NOT EXISTS(SELECT 1 FROM avatar_records WHERE user_id=NEW.target_user_id AND aic=NEW.target_aic))
      BEGIN SELECT RAISE(ABORT,'participants are immutable'); END;
    ''')
