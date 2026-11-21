# 模块说明：用户技能及分身协商的增量存储，原账户、资料和会话保持原样。


# 职责：建立按账户隔离的技能、集合修订和双分身对话表。
# 输入：db 为数据库初始化连接，已有用户和分身基础表已存在。
# 结果与约束：增量保留原文；移除旧轮数配置并暂停旧任务，外键及不可变归属限制跨用户搬移。
def migrate_agent_collaboration(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS agent_skills (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
      description TEXT NOT NULL, instructions TEXT NOT NULL, manifest TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL, UNIQUE(user_id,name));
    CREATE TABLE IF NOT EXISTS agent_skill_collections (
      user_id TEXT PRIMARY KEY REFERENCES users(id), updated_at TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS skill_owner_immutable BEFORE UPDATE OF user_id ON agent_skills
      WHEN NEW.user_id IS NOT OLD.user_id BEGIN SELECT RAISE(ABORT,'owner is immutable'); END;
    CREATE TABLE IF NOT EXISTS agent_dialogues (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), own_aic TEXT NOT NULL,
      target_user_id TEXT NOT NULL REFERENCES users(id), target_aic TEXT NOT NULL, objective TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued', active_speaker TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '', initiator_confirmed INTEGER NOT NULL DEFAULT 0,
      target_confirmed INTEGER NOT NULL DEFAULT 0, request_id TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id,request_id));
    CREATE INDEX IF NOT EXISTS agent_dialogues_participants ON agent_dialogues(user_id,target_user_id,created_at);
    CREATE INDEX IF NOT EXISTS agent_dialogues_incoming ON agent_dialogues(target_user_id,created_at);
    CREATE INDEX IF NOT EXISTS agent_dialogues_queue ON agent_dialogues(status,created_at,id);
    CREATE TRIGGER IF NOT EXISTS dialogue_participants_immutable
      BEFORE UPDATE OF user_id,own_aic,target_user_id,target_aic ON agent_dialogues
      WHEN NEW.user_id IS NOT OLD.user_id OR NEW.own_aic IS NOT OLD.own_aic
        OR NEW.target_user_id IS NOT OLD.target_user_id OR NEW.target_aic IS NOT OLD.target_aic
      BEGIN SELECT RAISE(ABORT,'participants are immutable'); END;
    CREATE TABLE IF NOT EXISTS agent_dialogue_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, dialogue_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      turn INTEGER NOT NULL, speaker_aic TEXT NOT NULL, speaker_name TEXT NOT NULL, content TEXT NOT NULL,
      usage TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(dialogue_id,turn));
    ''')

    # 移除旧总轮数设置，保留已有协商和原文。
    columns={row['name'] for row in db.execute('PRAGMA table_info(agent_dialogues)')}
    if 'max_turns' in columns:
        # 原任务按有限轮数委托；升级时暂停，避免擅自转为持续计费。
        db.execute("UPDATE agent_dialogues SET status='paused',active_speaker='',error='协商已升级为无固定轮数，请确认后继续' WHERE status IN ('queued','running')")
        db.execute('ALTER TABLE agent_dialogues DROP COLUMN max_turns')
    for name,declaration in (('revision','INTEGER NOT NULL DEFAULT 0'),('summary',"TEXT NOT NULL DEFAULT ''"),
                             ('summary_through','INTEGER NOT NULL DEFAULT 0'),('next_speaker','INTEGER NOT NULL DEFAULT 0'),
                             ('initiator_deleted','INTEGER NOT NULL DEFAULT 0'),('target_deleted','INTEGER NOT NULL DEFAULT 0'),('initiator_done','INTEGER NOT NULL DEFAULT 0'),
                             ('target_done','INTEGER NOT NULL DEFAULT 0'),('ended_at','TEXT'),('expires_at','TEXT'),
                             ('end_reason',"TEXT NOT NULL DEFAULT ''"),('initiator_skills',"TEXT NOT NULL DEFAULT ''"),('target_skills',"TEXT NOT NULL DEFAULT ''"),
                             ('source_conversation_id','TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL'),
                             ('retry_until','REAL NOT NULL DEFAULT 0')):
        if name not in columns:db.execute(f'ALTER TABLE agent_dialogues ADD COLUMN {name} {declaration}')
    if 'speaker_role' not in {row['name'] for row in db.execute('PRAGMA table_info(agent_dialogue_messages)')}:
        db.execute("ALTER TABLE agent_dialogue_messages ADD COLUMN speaker_role TEXT NOT NULL DEFAULT 'agent'")
    if 'next_speaker' not in columns:
        db.execute("UPDATE agent_dialogues SET next_speaker=(SELECT COUNT(*)%2 FROM agent_dialogue_messages WHERE dialogue_id=agent_dialogues.id AND speaker_role='agent')")
    db.execute("UPDATE agent_dialogues SET status='paused' WHERE status='cancelled'")
    db.execute("""CREATE TABLE IF NOT EXISTS agent_dialogue_controls (
      dialogue_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
      PRIMARY KEY(dialogue_id,user_id,request_id))""")
    db.execute('CREATE INDEX IF NOT EXISTS agent_dialogues_expiry ON agent_dialogues(expires_at)')
    # 历史双方已确认的记录恢复推进，不再把确认作为终态。
    db.execute("UPDATE agent_dialogues SET status='queued',revision=revision+1 WHERE status='confirmed'")
