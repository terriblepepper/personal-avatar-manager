"""独立知识库素材、后台构建和审核后生效版本。"""
def migrate_soul(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS knowledge_assets (
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),filename TEXT NOT NULL,
      mime TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,kind TEXT NOT NULL,
      caption TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(user_id,sha256));
    CREATE TABLE IF NOT EXISTS soul_builds (
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),prompt TEXT NOT NULL,
      asset_ids TEXT NOT NULL,profile_snapshot TEXT NOT NULL,profile_revision TEXT NOT NULL,
      status TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,stage TEXT NOT NULL,
      notes TEXT NOT NULL DEFAULT '[]',document TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',
      reviewer TEXT REFERENCES users(id),reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,reviewed_at TEXT);
    CREATE TABLE IF NOT EXISTS active_souls (
      user_id TEXT PRIMARY KEY REFERENCES users(id),build_id TEXT NOT NULL REFERENCES soul_builds(id));
    CREATE INDEX IF NOT EXISTS soul_builds_queue ON soul_builds(status,created_at);
    CREATE INDEX IF NOT EXISTS soul_builds_owner ON soul_builds(user_id,created_at);
    CREATE TABLE IF NOT EXISTS soul_question_categories (
      id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,active INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS soul_questions (
      id TEXT PRIMARY KEY,category_id TEXT NOT NULL REFERENCES soul_question_categories(id),
      prompt TEXT NOT NULL,fingerprint TEXT NOT NULL UNIQUE,source TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS soul_questions_category ON soul_questions(category_id,active);
    CREATE TABLE IF NOT EXISTS style_learning_settings (
      conversation_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
      enabled INTEGER NOT NULL DEFAULT 0,last_message_id INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS style_learning_runs (
      build_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),mode TEXT NOT NULL,
      day TEXT NOT NULL,sources TEXT NOT NULL,samples TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS style_learning_daily ON style_learning_runs(user_id,day) WHERE mode='auto';
    CREATE TABLE IF NOT EXISTS style_learning_memory (
      user_id TEXT PRIMARY KEY REFERENCES users(id),document TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS embedding_settings (
      user_id TEXT PRIMARY KEY REFERENCES users(id),config TEXT NOT NULL,updated_at TEXT NOT NULL);
    ''')
    if 'source_attachment_id' not in {r[1] for r in db.execute('PRAGMA table_info(knowledge_assets)')}:
        db.execute("ALTER TABLE knowledge_assets ADD COLUMN source_attachment_id TEXT NOT NULL DEFAULT ''")
        restore_asset_sources(db)
    if 'relative_path' not in {r[1] for r in db.execute('PRAGMA table_info(knowledge_assets)')}:
        db.execute("ALTER TABLE knowledge_assets ADD COLUMN relative_path TEXT NOT NULL DEFAULT ''")
    db.executescript('''CREATE TABLE IF NOT EXISTS knowledge_folders(user_id TEXT NOT NULL REFERENCES users(id),path TEXT NOT NULL,caption TEXT NOT NULL DEFAULT '',PRIMARY KEY(user_id,path));
      CREATE TABLE IF NOT EXISTS knowledge_collections(user_id TEXT PRIMARY KEY REFERENCES users(id),updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS knowledge_asset_updates(user_id TEXT PRIMARY KEY REFERENCES users(id),revision TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);''')
    columns={row[1] for row in db.execute('PRAGMA table_info(soul_builds)')}
    for name,definition in {
        'interview_rounds':'INTEGER NOT NULL DEFAULT 0',
        'interview':"TEXT NOT NULL DEFAULT '[]'",
        'observations':"TEXT NOT NULL DEFAULT '[]'",
        'phase':"TEXT NOT NULL DEFAULT 'analysis'",
        'asset_captions':"TEXT NOT NULL DEFAULT '{}'",
        'question_plan':"TEXT NOT NULL DEFAULT '[]'",
        'interview_observations':"TEXT NOT NULL DEFAULT '[]'",
        'analyzed_answers':'INTEGER NOT NULL DEFAULT 0',
        'bank_answers':"TEXT NOT NULL DEFAULT '{}'",
        'followup_rounds':'INTEGER NOT NULL DEFAULT 0',
        'parent_build':"TEXT NOT NULL DEFAULT ''",
        'origin':"TEXT NOT NULL DEFAULT 'build'",
        'memory_context':"TEXT NOT NULL DEFAULT '{}'",
        'memory_fingerprint':"TEXT NOT NULL DEFAULT ''",
        'memory_tree':"TEXT NOT NULL DEFAULT '{}'",
        'chat_samples':"TEXT NOT NULL DEFAULT '[]'",
        'rebuild':'INTEGER NOT NULL DEFAULT 0',
    }.items():
        if name not in columns:
            db.execute(f'ALTER TABLE soul_builds ADD COLUMN {name} {definition}')
            if name=='followup_rounds':
                db.execute("UPDATE soul_builds SET followup_rounds=interview_rounds WHERE question_plan!='[]' AND interview_rounds IN (3,5) AND status IN ('queued','running','waiting_user')")
    for table in ('knowledge_assets','soul_builds'):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_owner_immutable BEFORE UPDATE OF user_id ON {table}
                       WHEN NEW.user_id IS NOT OLD.user_id BEGIN SELECT RAISE(ABORT,'owner is immutable'); END""")
    for action in ('INSERT','UPDATE'):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS active_soul_owner_{action.lower()} BEFORE {action} ON active_souls
          WHEN NOT EXISTS(SELECT 1 FROM soul_builds b WHERE b.id=NEW.build_id AND b.user_id=NEW.user_id)
          BEGIN SELECT RAISE(ABORT,'soul owner mismatch'); END""")


def restore_asset_sources(db):
    """来源列首次引入时，按所有者和 SHA 匹配历史附件。"""
    db.execute("""UPDATE knowledge_assets SET source_attachment_id=coalesce(
        (SELECT a.id FROM attachments a WHERE a.user_id=knowledge_assets.user_id
         AND a.sha256=knowledge_assets.sha256 ORDER BY a.created_at,a.id LIMIT 1),'')""")
