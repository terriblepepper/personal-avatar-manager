"""Durable request identities; legacy conversation snapshots remain readable."""
import hashlib
import json
import uuid


def migrate_chat_generations(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS chat_generation_jobs (
      task_id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id),
      request_id TEXT NOT NULL,
      conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL,
      fingerprint TEXT NOT NULL,
      payload TEXT NOT NULL,
      authorization TEXT NOT NULL DEFAULT '{}',
      state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','indeterminate')),
      snapshot TEXT NOT NULL,
      created_at REAL NOT NULL, updated_at REAL NOT NULL,
      UNIQUE(user_id,request_id));
    CREATE UNIQUE INDEX IF NOT EXISTS chat_generation_active
      ON chat_generation_jobs(conversation_id) WHERE state IN ('queued','running');
    CREATE INDEX IF NOT EXISTS chat_generation_queue ON chat_generation_jobs(state,created_at);
    CREATE TRIGGER IF NOT EXISTS chat_generation_forget BEFORE DELETE ON chat_conversations
    BEGIN
      UPDATE chat_generation_jobs SET payload='{}',authorization='{}',
        state='cancelled',snapshot='{"done":false,"error":"会话已删除","state":"cancelled"}'
        WHERE conversation_id=OLD.id;
    END;
    ''')
    restore_legacy_generations(db)


def restore_legacy_generations(db):
    """只有旧快照的会话补录任务；已有任务的状态、授权和租约保持原样。"""
    rows = db.execute("""SELECT p.* FROM chat_pending p JOIN chat_conversations c
        ON c.id=p.conversation_id AND c.user_id=p.user_id
        WHERE NOT EXISTS (SELECT 1 FROM chat_generation_jobs j WHERE j.conversation_id=p.conversation_id)""")
    for row in rows:
        try:
            value = json.loads(row['snapshot'])
            if not isinstance(value, dict):
                raise ValueError('Invalid legacy snapshot')
        except (ValueError, TypeError):
            value = {'done':False, 'error':'旧请求状态损坏，未执行或重发请求'}
        request_id = value.get('request_id')
        if not isinstance(request_id, str) or not request_id:
            request_id = 'legacy-' + hashlib.sha256(row['conversation_id'].encode()).hexdigest()
        if db.execute('SELECT 1 FROM chat_generation_jobs WHERE user_id=? AND request_id=?',
                      (row['user_id'],request_id)).fetchone():
            continue
        payload = value.pop('_request', None)
        if value.get('done'):
            state = 'succeeded'
        elif value.get('error'):
            state = 'failed'
        elif value.get('stage') == 'queued' and payload:
            state = 'queued'
        else:
            state = 'indeterminate'
            value['error'] = '原请求结果未确认，请检查历史；不会自动重发'
        if state != 'queued':
            db.execute("""UPDATE chat_conversations SET lease_until=0 WHERE (id=? OR parent_id=?)
                AND NOT EXISTS (SELECT 1 FROM chat_generation_jobs j
                WHERE j.conversation_id=chat_conversations.id AND j.state IN ('queued','running'))""",
                       (row['conversation_id'],row['conversation_id']))
        value.update(request_id=request_id, state=state, done=state=='succeeded')
        raw = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, separators=(',',':'))
        db.execute("""INSERT INTO chat_generation_jobs(task_id,user_id,request_id,conversation_id,fingerprint,payload,
            state,snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (uuid.uuid4().hex,row['user_id'],request_id,row['conversation_id'],hashlib.sha256(raw.encode()).hexdigest(),
                    raw,state,json.dumps(value,ensure_ascii=False),row['updated_at'],row['updated_at']))
