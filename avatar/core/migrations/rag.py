"""资料 Chroma 索引清单与清理队列；移除旧 SQLite 向量和历史，仅保留业务权限。"""
def migrate_rag(db):
    if 'sha256' not in {row[1] for row in db.execute('PRAGMA table_info(chat_files)')}:
        db.execute("ALTER TABLE chat_files ADD COLUMN sha256 TEXT NOT NULL DEFAULT ''")
    if 'md5' not in {row[1] for row in db.execute('PRAGMA table_info(chat_files)')}:
        db.execute("ALTER TABLE chat_files ADD COLUMN md5 TEXT NOT NULL DEFAULT ''")
    db.executescript("""
    DROP TRIGGER IF EXISTS file_memory_owner;
    DROP TRIGGER IF EXISTS file_memory_immutable;
    DROP TRIGGER IF EXISTS profile_file_memory_delete;
    DROP TRIGGER IF EXISTS chat_file_memory_delete;
    DROP TABLE IF EXISTS chat_file_memories;
    CREATE TABLE IF NOT EXISTS file_rag_documents(
      source TEXT NOT NULL CHECK(source IN ('profile','chat')),file_id TEXT NOT NULL,
      user_id TEXT NOT NULL REFERENCES users(id),status TEXT NOT NULL DEFAULT 'queued',
      revision TEXT NOT NULL DEFAULT '',signature TEXT NOT NULL DEFAULT '',coverage TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT '',PRIMARY KEY(source,file_id));
    CREATE INDEX IF NOT EXISTS rag_owner_state ON file_rag_documents(user_id,status);
    CREATE TABLE IF NOT EXISTS model_artifact_cache(
      user_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL,signature TEXT NOT NULL,digest TEXT NOT NULL,
      source_hash TEXT NOT NULL DEFAULT '',value BLOB NOT NULL,updated_at TEXT NOT NULL,
      PRIMARY KEY(user_id,kind,signature,digest));
    CREATE TABLE IF NOT EXISTS file_rag_epochs(user_id TEXT PRIMARY KEY REFERENCES users(id),epoch TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS file_rag_gc(
      user_id TEXT NOT NULL REFERENCES users(id),source TEXT NOT NULL,file_id TEXT NOT NULL,
      collection TEXT NOT NULL,record_id TEXT NOT NULL,delete_after REAL NOT NULL,
      PRIMARY KEY(user_id,collection,record_id));
    CREATE INDEX IF NOT EXISTS rag_gc_due ON file_rag_gc(delete_after);
    CREATE INDEX IF NOT EXISTS chat_file_content ON chat_files(conversation_id,sha256,md5);
    """)
    columns={row[1] for row in db.execute('PRAGMA table_info(file_rag_documents)')}
    migrating='collection' not in columns
    for name,kind in [('lease_until','REAL NOT NULL DEFAULT 0'),('next_attempt_at','REAL NOT NULL DEFAULT 0')]:
        if name not in columns:db.execute('ALTER TABLE file_rag_documents ADD COLUMN '+name+' '+kind)
    db.execute('CREATE TABLE IF NOT EXISTS rag_schedule_users(user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,last_served REAL NOT NULL DEFAULT 0)')
    for name,kind in [('generation','INTEGER NOT NULL DEFAULT 0'),('cache_epoch',"TEXT NOT NULL DEFAULT ''"),('force_rebuild','INTEGER NOT NULL DEFAULT 0'),('collection',"TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:db.execute('ALTER TABLE file_rag_documents ADD COLUMN '+name+' '+kind)
    if migrating:
        db.execute('DROP TABLE IF EXISTS file_rag_history')
        db.execute('DROP TABLE IF EXISTS file_rag_chunks')
        db.execute("UPDATE file_rag_documents SET status='queued',error='',revision='',signature='',generation=generation+1")
        db.execute("DELETE FROM model_artifact_cache WHERE kind='parse' AND signature LIKE 'native-v1%'")
        db.execute("UPDATE model_settings SET model_config=json_remove(model_config,'$.retrieval_max_steps') WHERE json_valid(model_config) AND json_type(model_config,'$.retrieval_max_steps') IS NOT NULL")
        for source,table in (('profile','attachments'),('chat','chat_files')):
            db.execute("INSERT OR IGNORE INTO file_rag_documents(source,file_id,user_id) SELECT ?,id,user_id FROM "+table,(source,))
            db.execute("UPDATE "+table+" SET extracted_text='',extraction_status='rag_pending'")
    for prefix,table,source,fields,condition in (
        ('profile','attachments','profile','caption,sha256,filename,relative_path','NEW.caption!=OLD.caption OR NEW.sha256!=OLD.sha256 OR NEW.filename!=OLD.filename OR NEW.relative_path!=OLD.relative_path'),
        ('chat','chat_files','chat','description,sha256,referenced','NEW.description!=OLD.description OR NEW.sha256!=OLD.sha256 OR NEW.referenced!=OLD.referenced')):
        for suffix in ('insert','update','delete'):db.execute('DROP TRIGGER IF EXISTS rag_'+prefix+'_'+suffix)
        db.executescript(f"""
        CREATE TRIGGER rag_{prefix}_insert AFTER INSERT ON {table} BEGIN
          INSERT INTO file_rag_documents(source,file_id,user_id,cache_epoch) VALUES('{source}',NEW.id,NEW.user_id,coalesce((SELECT epoch FROM file_rag_epochs WHERE user_id=NEW.user_id),'')); END;
        CREATE TRIGGER rag_{prefix}_update AFTER UPDATE OF {fields} ON {table} WHEN {condition} BEGIN
          UPDATE file_rag_documents SET status='queued',error='',generation=generation+1 WHERE source='{source}' AND file_id=NEW.id; END;
        CREATE TRIGGER rag_{prefix}_delete AFTER DELETE ON {table} BEGIN
          DELETE FROM file_rag_documents WHERE source='{source}' AND file_id=OLD.id; END;
        CREATE TRIGGER IF NOT EXISTS {table}_parse_cache_delete AFTER DELETE ON {table} BEGIN
          DELETE FROM model_artifact_cache WHERE user_id=OLD.user_id AND kind='parse' AND source_hash=OLD.sha256
          AND NOT EXISTS(SELECT 1 FROM attachments WHERE user_id=OLD.user_id AND sha256=OLD.sha256)
          AND NOT EXISTS(SELECT 1 FROM chat_files WHERE user_id=OLD.user_id AND sha256=OLD.sha256); END;
        """)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS file_rag_nodes(
      source TEXT NOT NULL,file_id TEXT NOT NULL,user_id TEXT NOT NULL REFERENCES users(id),
      ordinal INTEGER NOT NULL,collection TEXT NOT NULL,record_id TEXT NOT NULL,
      PRIMARY KEY(source,file_id,ordinal),
      FOREIGN KEY(source,file_id) REFERENCES file_rag_documents(source,file_id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS rag_nodes_record ON file_rag_nodes(user_id,collection,record_id);
    CREATE TRIGGER IF NOT EXISTS rag_document_delete BEFORE DELETE ON file_rag_documents BEGIN
      INSERT OR REPLACE INTO file_rag_gc(user_id,source,file_id,collection,record_id,delete_after)
        SELECT user_id,source,file_id,collection,record_id,0 FROM file_rag_nodes WHERE source=OLD.source AND file_id=OLD.file_id;
      UPDATE file_rag_gc SET delete_after=0 WHERE user_id=OLD.user_id AND source=OLD.source AND file_id=OLD.file_id;
    END;
    """)
