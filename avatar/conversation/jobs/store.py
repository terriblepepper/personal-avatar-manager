"""Request identity and task transitions belong to persistence, not transport."""
import hashlib
import json
import time
import uuid
from fastapi import HTTPException
from avatar.files.library.delivery import material_card, visible_delivery_reply

ACTIVE=('queued','running')
TERMINAL=('succeeded','failed','cancelled','indeterminate','awaiting_input')


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def snapshot_value(raw):
    try:
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('Invalid snapshot')
        return value
    except (ValueError,TypeError):
        return {'reply':'','done':False,'error':'任务进度损坏，原结果未确认'}


class GenerationStore:
    def __init__(self,database):self.database=database

    @staticmethod
    def view(row,db=None):
        if row is None:return None
        value=snapshot_value(row['snapshot']);value.pop('_request',None)
        value.update(task_id=row['task_id'],request_id=row['request_id'],state=row['state'],conversation_id=row['conversation_id'])
        result=value.get('result')
        files=result.get('generated_files') if isinstance(result,dict) else None
        originals=[item['id'] for item in files if item.get('origin')=='material'] if isinstance(files,list) else []
        if db is not None and originals:
            live={source['id']:material_card(source) for source in db.execute(
                'SELECT id,filename,size,content_type FROM attachments WHERE user_id=? AND id=ANY(?::text[])',
                (row['user_id'],originals))}
            result['generated_files']=[live[item['id']] if item.get('origin')=='material' else item for item in files
                                       if item.get('origin')!='material' or item['id'] in live]
        if isinstance(files,list) and row['state']=='succeeded' and 'reply' in result:
            result['reply']=visible_delivery_reply(result['reply'],result['generated_files'])
            value['reply']=result['reply']
        return value

    def get(self,user_id,request_id):
        with self.database.connect() as db:
            return self.view(db.execute('SELECT * FROM chat_generation_jobs WHERE user_id=? AND request_id=?',(user_id,request_id)).fetchone(),db)

    def latest(self,user_id,conversation_id):
        with self.database.connect() as db:
            latest=self.view(db.execute('SELECT * FROM chat_generation_jobs WHERE user_id=? AND conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(user_id,conversation_id)).fetchone(),db)
            if latest and latest['state'] in ACTIVE:return latest
            from avatar.conversation.pauses import pending
            paused=pending(db,user_id,conversation_id)
            if paused:
                if latest and latest.get('agent_pause_id')==paused['snapshot']['agent_pause_id']:return latest
                return paused['snapshot']
            return latest if latest and latest['state']!='awaiting_input' else None

    def submit(self,user,body):
        if not body.request_id or not body.conversation_id:raise HTTPException(422,'请求和会话标识不能为空')
        fields=body.model_dump()
        if not body.agent_control:
            fields.pop('agent_pause_id');fields.pop('agent_control')
        raw=canonical(fields);fingerprint=hashlib.sha256(raw.encode()).hexdigest()
        with self.database.connect(write=True) as db:
            owner=db.execute('SELECT user_id FROM chat_conversations WHERE id=?',(body.conversation_id,)).fetchone()
            if not owner or owner['user_id']!=user['id']:raise HTTPException(404,'会话不存在')
            previous=db.execute('SELECT * FROM chat_generation_jobs WHERE user_id=? AND request_id=?',(user['id'],body.request_id)).fetchone()
            if previous:
                if previous['fingerprint']!=fingerprint:raise HTTPException(409,'同一请求标识不能更改内容')
                return {'accepted':True,'request_id':body.request_id,'task_id':previous['task_id']}
            if db.execute("SELECT 1 FROM chat_generation_jobs WHERE conversation_id=? AND state IN ('queued','running')",(body.conversation_id,)).fetchone():raise HTTPException(409,'上一条消息仍在生成，请等待完成')
            from avatar.conversation.pauses import checked
            checked(db,user['id'],body)
            task_id=uuid.uuid4().hex;timestamp=time.time()
            from avatar.conversation.jobs.pipeline import capture_authorization
            scope=capture_authorization(db,user['id'],body)
            snapshot=dict(channel=body.channel,message=body.message,reply='',status='服务器已接收，等待生成',stage='queued',done=False,error='',recovered=True)
            db.execute('INSERT INTO chat_generation_jobs(task_id,user_id,request_id,conversation_id,fingerprint,payload,authorization,state,snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id,user['id'],body.request_id,body.conversation_id,fingerprint,raw,canonical(scope),'queued',canonical(snapshot),timestamp,timestamp))
        return {'accepted':True,'request_id':body.request_id,'task_id':task_id}

    def claim(self,limit=1):
        with self.database.connect(write=True) as db:
            rows=db.execute("SELECT * FROM chat_generation_jobs WHERE state='queued' ORDER BY created_at,rowid LIMIT ?",(max(0,limit),)).fetchall()
            for row in rows:db.execute("UPDATE chat_generation_jobs SET state='running',updated_at=? WHERE task_id=? AND state='queued'",(time.time(),row['task_id']))
        return [dict(row) for row in rows]

    def release_unstarted(self, tasks):
        with self.database.connect(write=True) as db:
            db.execute("UPDATE chat_generation_jobs SET state='queued',updated_at=? WHERE state='running' AND task_id=ANY(?::text[])",
                       (time.time(),tasks))

    def record(self,user_id,request_id,snapshot):
        with self.database.connect(write=True, account_scope=user_id) as db:
            row=db.execute("""UPDATE chat_generation_jobs SET snapshot=?,updated_at=?
                WHERE user_id=? AND request_id=? AND state='running' RETURNING conversation_id""",
                (canonical(snapshot),time.time(),user_id,request_id)).fetchone()
            if not row:return False
            db.execute('INSERT OR REPLACE INTO chat_pending VALUES(?,?,?,?)',(row[0],user_id,canonical({**snapshot,'request_id':request_id}),time.time()))
        return True

    def finish(self,user_id,request_id,state,result=None,error=''):
        if state not in TERMINAL:raise ValueError('Invalid generation terminal state')
        with self.database.connect(write=True) as db:
            return self.finish_in(db,user_id,request_id,state,result,error)

    @staticmethod
    def finish_in(db,user_id,request_id,state,result=None,error=''):
        row=db.execute("SELECT * FROM chat_generation_jobs WHERE user_id=? AND request_id=? AND state IN ('queued','running')",(user_id,request_id)).fetchone()
        if not row:return False
        snapshot=snapshot_value(row['snapshot']);snapshot.update(done=state=='succeeded',state=state,error=error,status='回答完成' if state=='succeeded' else error)
        if result:snapshot.update(reply=result.get('reply',snapshot.get('reply','')),result=result,usage=result.get('usage'))
        if state=='cancelled' and result:
            snapshot.update(status=result['reply'],stage='cancelled',agent_pause_id=None)
            for event in snapshot.get('tool_events',[]):
                if event.get('status')=='running':event['status']='interrupted'
        if state=='awaiting_input':
            snapshot.update(status=result['status'],agent_pause_id=result['agent_pause_id'],stream_retryable=result.get('stream_retryable',False),stage=state,reply=result.get('reply',''))
        db.execute('UPDATE chat_generation_jobs SET state=?,snapshot=?,updated_at=? WHERE task_id=?',(state,canonical(snapshot),time.time(),row['task_id']))
        if row['conversation_id']:
            db.execute('INSERT OR REPLACE INTO chat_pending VALUES(?,?,?,?)',(row['conversation_id'],user_id,canonical({**snapshot,'request_id':request_id}),time.time()))
            if state=='succeeded':
                db.execute('UPDATE chat_conversations SET lease_until=0 WHERE id=? OR parent_id=?',(row['conversation_id'],row['conversation_id']))
        return True

    def recover(self,active_ids=()):
        """Recover orphaned runs and check for queued work in one read-only poll."""
        with self.database.connect() as db:
            rows=db.execute("""SELECT state,task_id,user_id,request_id FROM chat_generation_jobs WHERE state='running'
                UNION ALL (SELECT state,task_id,user_id,request_id FROM chat_generation_jobs WHERE state='queued' LIMIT 1)""").fetchall()
        queued=False
        for row in rows:
            if row['state']=='queued':
                queued=True
                continue
            if row['task_id'] in active_ids:continue
            with self.database.connect(write=True) as db:
                job=db.execute('SELECT conversation_id FROM chat_generation_jobs WHERE user_id=? AND request_id=?',(row['user_id'],row['request_id'])).fetchone()
                if self.finish_in(db,row['user_id'],row['request_id'],'indeterminate',error='原请求结果未确认，请检查历史；不会自动重发'):
                    db.execute('UPDATE chat_conversations SET lease_until=0 WHERE id=? OR parent_id=?',(job['conversation_id'],job['conversation_id']))
        return queued
