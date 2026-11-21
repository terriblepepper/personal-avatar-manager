"""记录删除仅影响本人的展示，不抹去计费审计或正在执行的维护任务。"""
def migrate_record_visibility(db):
    db.execute('''CREATE TABLE IF NOT EXISTS record_dismissals (
      user_id TEXT NOT NULL REFERENCES users(id), collection TEXT NOT NULL,
      record_id TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(user_id,collection,record_id))''')
