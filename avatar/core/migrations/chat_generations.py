"""Durable request identities; legacy conversation snapshots remain readable."""
import hashlib
import json
import uuid


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
