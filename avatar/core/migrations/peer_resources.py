"""分身会话复用聊天文件和工具，双方各持有独立、七天到期的资源副本。"""


def migrate_peer_resources(db):
    """增量添加资源工作区、文件授权及发言状态，不修改已有聊天正文。"""
    previous_columns={r['name'] for r in db.execute('PRAGMA table_info(agent_dialogues)')}
    for table,fields in {
        'agent_session_members':{'resource_conversation_id':'TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL'},
        'chat_messages':{'task_event':"TEXT NOT NULL DEFAULT '{}'"},
        'chat_files':{'expires_at':'TEXT','sha256':"TEXT NOT NULL DEFAULT ''"},
        'conversation_diagrams':{'expires_at':'TEXT'},
        'agent_dialogue_messages':{'task_action':"TEXT NOT NULL DEFAULT ''",'speaker_user_id':'TEXT REFERENCES users(id)','state':"TEXT NOT NULL DEFAULT ''",'state_detail':"TEXT NOT NULL DEFAULT ''",'tool_activity':"TEXT NOT NULL DEFAULT '[]'"},
        'agent_dialogues':{'waiting_reason':"TEXT NOT NULL DEFAULT ''",'tool_activity':"TEXT NOT NULL DEFAULT '[]'",'accepted':'INTEGER NOT NULL DEFAULT 0','confirmation_required':'INTEGER NOT NULL DEFAULT 0','confirmation_acknowledged':'INTEGER NOT NULL DEFAULT 0'},
    }.items():
        columns={r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
        for name,definition in fields.items():
            if name not in columns:db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
    if 'accepted' not in previous_columns:
        db.execute("UPDATE agent_dialogues SET accepted=CASE WHEN status='awaiting_acceptance' THEN 0 ELSE 1 END")
        db.execute("UPDATE agent_dialogues SET next_speaker=0 WHERE delivery_mode='message' AND status!='ended' AND NOT EXISTS(SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=agent_dialogues.id AND speaker_role='agent')")
    if 'confirmation_required' not in previous_columns:
        db.execute("UPDATE agent_dialogues SET confirmation_required=3 WHERE delivery_mode!='message' AND status='awaiting_input' AND task_kind='booking'")
        db.execute("UPDATE agent_dialogues SET confirmation_required=CASE WHEN next_speaker=1 THEN 1 ELSE 2 END WHERE delivery_mode!='message' AND status='awaiting_input' AND task_kind!='booking'")
    db.executescript('''
    CREATE TABLE IF NOT EXISTS agent_task_members (
      task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), hidden INTEGER NOT NULL DEFAULT 0,
      summary TEXT NOT NULL DEFAULT '', summary_through INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(task_id,user_id));
    INSERT OR IGNORE INTO agent_task_members(task_id,user_id) SELECT id,user_id FROM agent_dialogues;
    INSERT OR IGNORE INTO agent_task_members(task_id,user_id) SELECT id,target_user_id FROM agent_dialogues;
    CREATE TRIGGER IF NOT EXISTS agent_task_member_scope BEFORE INSERT ON agent_task_members
      WHEN NOT EXISTS(SELECT 1 FROM agent_dialogues WHERE id=NEW.task_id AND NEW.user_id IN (user_id,target_user_id))
      BEGIN SELECT RAISE(ABORT,'task member mismatch'); END;
    CREATE TABLE IF NOT EXISTS agent_task_files (
      task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      file_id TEXT NOT NULL REFERENCES chat_files(id) ON DELETE CASCADE,
      source_user_id TEXT NOT NULL REFERENCES users(id),
      PRIMARY KEY(task_id,file_id));
    CREATE INDEX IF NOT EXISTS chat_files_expiry ON chat_files(expires_at);
    CREATE TRIGGER IF NOT EXISTS peer_file_expiry AFTER INSERT ON chat_files
      WHEN EXISTS(SELECT 1 FROM chat_conversations WHERE id=NEW.conversation_id AND channel='peer')
      BEGIN UPDATE chat_files SET expires_at=datetime('now','+7 days') WHERE id=NEW.id; END;
    CREATE TRIGGER IF NOT EXISTS peer_diagram_expiry AFTER INSERT ON conversation_diagrams
      WHEN EXISTS(SELECT 1 FROM chat_conversations WHERE id=NEW.conversation_id AND channel='peer')
      BEGIN UPDATE conversation_diagrams SET expires_at=datetime('now','+7 days') WHERE id=NEW.id; END;
    ''')
    columns={r['name'] for r in db.execute('PRAGMA table_info(agent_task_members)')}
    if 'resource_conversation_id' not in columns:
        db.execute('ALTER TABLE agent_task_members ADD COLUMN resource_conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL')
        # 旧任务保留已授权文件；此后的新任务从独立空工作区开始。
        db.execute('UPDATE agent_task_members SET resource_conversation_id=(SELECT m.resource_conversation_id FROM agent_session_members m JOIN agent_dialogues d ON d.session_id=m.session_id WHERE d.id=task_id AND m.user_id=agent_task_members.user_id)')
    db.execute("UPDATE agent_dialogue_messages SET speaker_user_id=(SELECT CASE WHEN speaker_aic=d.own_aic THEN d.user_id WHEN speaker_aic=d.target_aic THEN d.target_user_id END FROM agent_dialogues d WHERE d.id=dialogue_id) WHERE speaker_user_id IS NULL")
    columns={r['name'] for r in db.execute('PRAGMA table_info(agent_task_files)')}
    if 'message_id' not in columns:db.execute('ALTER TABLE agent_task_files ADD COLUMN message_id INTEGER REFERENCES agent_dialogue_messages(id) ON DELETE SET NULL')
    db.executescript('''
    DROP TRIGGER IF EXISTS peer_file_scope;
    CREATE TRIGGER peer_file_scope BEFORE INSERT ON agent_task_files
      WHEN NOT EXISTS(SELECT 1 FROM agent_dialogues d JOIN agent_session_members m ON m.session_id=d.session_id
        JOIN agent_task_members t ON t.task_id=d.id AND t.user_id=m.user_id
        JOIN chat_files f ON f.conversation_id=CASE WHEN d.delivery_mode='message' THEN m.resource_conversation_id ELSE t.resource_conversation_id END AND f.user_id=m.user_id
        WHERE d.id=NEW.task_id AND f.id=NEW.file_id
        AND NEW.source_user_id IN (d.user_id,d.target_user_id))
      BEGIN SELECT RAISE(ABORT,'peer file scope mismatch'); END;
    ''')
