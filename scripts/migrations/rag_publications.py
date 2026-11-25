"""Immutable published manifests and a derived FTS index; originals remain authoritative."""
import hashlib
import json


def migrate_rag_publications(db):
    columns={row[1] for row in db.execute('PRAGMA table_info(file_rag_documents)')}
    for name,definition in [('active_publication',"TEXT NOT NULL DEFAULT ''"),('legacy_backfill','INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:db.execute('ALTER TABLE file_rag_documents ADD COLUMN '+name+' '+definition)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS rag_publications(
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),source TEXT NOT NULL,file_id TEXT NOT NULL,
      revision TEXT NOT NULL,signature TEXT NOT NULL,collection TEXT NOT NULL,coverage TEXT NOT NULL,
      record_ids TEXT NOT NULL,manifest TEXT NOT NULL,lexical_ready INTEGER NOT NULL DEFAULT 0,
      legacy INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
      FOREIGN KEY(source,file_id) REFERENCES file_rag_documents(source,file_id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS rag_publication_owner ON rag_publications(user_id,collection);
    CREATE TABLE IF NOT EXISTS rag_lexical_records(
      user_id TEXT NOT NULL,collection TEXT NOT NULL,record_id TEXT NOT NULL,
      file_id TEXT NOT NULL,source TEXT NOT NULL,text TEXT NOT NULL,metadata TEXT NOT NULL,tokens TEXT NOT NULL,
      UNIQUE(user_id,collection,record_id));
    CREATE VIRTUAL TABLE IF NOT EXISTS rag_lexical_fts USING fts5(tokens,content='rag_lexical_records',content_rowid='rowid');
    CREATE TRIGGER IF NOT EXISTS rag_lexical_insert AFTER INSERT ON rag_lexical_records BEGIN
      INSERT INTO rag_lexical_fts(rowid,tokens) VALUES(NEW.rowid,NEW.tokens); END;
    CREATE TRIGGER IF NOT EXISTS rag_lexical_delete AFTER DELETE ON rag_lexical_records BEGIN
      INSERT INTO rag_lexical_fts(rag_lexical_fts,rowid,tokens) VALUES('delete',OLD.rowid,OLD.tokens); END;
    CREATE TRIGGER IF NOT EXISTS rag_lexical_update AFTER UPDATE OF tokens ON rag_lexical_records BEGIN
      INSERT INTO rag_lexical_fts(rag_lexical_fts,rowid,tokens) VALUES('delete',OLD.rowid,OLD.tokens);
      INSERT INTO rag_lexical_fts(rowid,tokens) VALUES(NEW.rowid,NEW.tokens); END;
    """)
    # Adopt old published records without discarding vectors or charging a model call.
    for row in db.execute("SELECT * FROM file_rag_documents WHERE active_publication='' AND collection!=''").fetchall():
        ids=[node[0] for node in db.execute('SELECT record_id FROM file_rag_nodes WHERE source=? AND file_id=? ORDER BY ordinal',(row['source'],row['file_id']))]
        if not ids:continue
        identity='legacy-'+hashlib.sha256(json.dumps([row['user_id'],row['source'],row['file_id'],row['collection'],ids]).encode()).hexdigest()
        manifest=dict(expected_chunks=len(ids),processed_chunks=len(ids),expected_pages=None,processed_pages=None,
                      expected_images=None,processed_images=None,complete=None,legacy=True)
        db.execute('INSERT OR IGNORE INTO rag_publications VALUES(?,?,?,?,?,?,?,?,?,?,0,1,?)',
            (identity,row['user_id'],row['source'],row['file_id'],row['revision'],row['signature'],row['collection'],row['coverage'],json.dumps(ids),json.dumps(manifest),row['updated_at']))
        db.execute('UPDATE file_rag_documents SET active_publication=?,legacy_backfill=1 WHERE source=? AND file_id=?',(identity,row['source'],row['file_id']))
