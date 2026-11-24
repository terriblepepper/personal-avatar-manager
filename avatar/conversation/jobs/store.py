"""Request identity and task transitions belong to persistence, not transport."""
import hashlib
import json
import time
import uuid
from fastapi import HTTPException
from avatar.files.library.delivery import material_card, visible_delivery_reply

ACTIVE=('queued','running')
TERMINAL=('succeeded','failed','cancelled','indeterminate','awaiting_input')
LEASE_SECONDS = 45


def run_key(row):
    return (row['user_id'], 'chat', row['conversation_id'],
            json.loads(row['authorization']).get('run_id') or row['task_id'])


def current_claim(row,active=None):
    from avatar.core.execution import current_generation
    active = active or current_generation.get()
    return (not active or (active['task_id']==row['task_id'] and
            json.loads(active['authorization']).get('claim_token')==json.loads(row['authorization']).get('claim_token')))


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
        if value.get('tool_events'):
            from avatar.conversation.pauses import public_event
            value['tool_events'] = [public_event(event) for event in value['tool_events']]
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
        if not body.question_id:
            fields.pop('question_id', None)
        if not body.agent_control:
            fields.pop('agent_pause_id');fields.pop('agent_control')
        raw=canonical(fields);fingerprint=hashlib.sha256(raw.encode()).hexdigest()
        with self.database.connect(write=True) as db:
            owner=db.execute('SELECT user_id,platform FROM chat_conversations WHERE id=?',(body.conversation_id,)).fetchone()
            if not owner or owner['user_id']!=user['id']:raise HTTPException(404,'会话不存在')
            previous=db.execute('SELECT * FROM chat_generation_jobs WHERE user_id=? AND request_id=?',(user['id'],body.request_id)).fetchone()
            if previous:
                if previous['fingerprint']!=fingerprint:raise HTTPException(409,'同一请求标识不能更改内容')
                return {'accepted':True,'request_id':body.request_id,'task_id':previous['task_id']}
            if db.execute("SELECT 1 FROM chat_generation_jobs WHERE conversation_id=? AND state IN ('queued','running')",(body.conversation_id,)).fetchone():raise HTTPException(409,'上一条消息仍在生成，请等待完成')
            from avatar.conversation.pauses import checked
            held = checked(db,user['id'],body)
            task_id=uuid.uuid4().hex;timestamp=time.time()
            from avatar.conversation.jobs.pipeline import capture_authorization
            scope=capture_authorization(db,user['id'],body)
            if owner['platform']=='web' or held:
                previous = (held or {}).get('snapshot', {})
                scope['run_id'] = (held or {}).get('run_id') or previous.get('task_id') or previous.get('agent_pause_id') or task_id
            snapshot=canonical(dict(request_id=body.request_id,channel=body.channel,message=body.message,reply='',
                                    status='服务器已接收，等待生成',stage='queued',done=False,error='',recovered=True))
            db.execute('INSERT INTO chat_generation_jobs(task_id,user_id,request_id,conversation_id,fingerprint,payload,authorization,state,snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id,user['id'],body.request_id,body.conversation_id,fingerprint,raw,canonical(scope),'queued',snapshot,timestamp,timestamp))
            db.execute('INSERT OR REPLACE INTO chat_pending VALUES(?,?,?,?)',
                       (body.conversation_id,user['id'],snapshot,timestamp))
        return {'accepted':True,'request_id':body.request_id,'task_id':task_id}

    def claim(self,limit=1,*,leased=False,busy=()):
        claimed = []
        with self.database.connect(write=True) as db:
            rows=db.execute("SELECT * FROM chat_generation_jobs WHERE state='queued' AND NOT (conversation_id=ANY(?::text[])) ORDER BY created_at,rowid LIMIT ? FOR UPDATE SKIP LOCKED",(list(busy),max(0,limit))).fetchall()
            for row in rows:
                scope = json.loads(row['authorization'])
                if leased:
                    scope['claim_token'] = uuid.uuid4().hex
                row = db.execute("""UPDATE chat_generation_jobs SET state='running',authorization=?,updated_at=?
                    WHERE task_id=? AND state='queued' RETURNING *""",(canonical(scope),time.time(),row['task_id'])).fetchone()
                if row:claimed.append(dict(row))
        return claimed

    def heartbeat(self, jobs):
        revoked = []
        with self.database.connect(write=True) as db:
            for job in jobs:
                updated=db.execute("""UPDATE chat_generation_jobs SET updated_at=? WHERE task_id=? AND state='running'
                    AND authorization::jsonb->>'claim_token'=?""",(time.time(),job['task_id'],json.loads(job['authorization'])['claim_token'])).rowcount
                if not updated:
                    row=db.execute('SELECT state FROM chat_generation_jobs WHERE task_id=?',(job['task_id'],)).fetchone()
                    if not row or row['state']!='succeeded':revoked.append(job['task_id'])
        return revoked

    def release_unstarted(self, jobs):
        with self.database.connect(write=True) as db:
            for job in jobs:
                db.execute("""UPDATE chat_generation_jobs SET state='queued',updated_at=? WHERE state='running'
                    AND task_id=? AND authorization::jsonb->>'claim_token'=?""",
                    (time.time(),job['task_id'],json.loads(job['authorization'])['claim_token']))

    def record(self,user_id,request_id,snapshot):
        from avatar.core.execution import current_generation
        job=current_generation.get()
        if job and (job['user_id'],job['request_id'])!=(user_id,request_id):return False
        claim=json.loads(job['authorization']).get('claim_token') if job else None
        with self.database.connect(write=True, account_scope=user_id) as db:
            row=db.execute("""UPDATE chat_generation_jobs SET snapshot=?,updated_at=?
                WHERE user_id=? AND request_id=? AND state='running'
                AND (authorization::jsonb->>'claim_token') IS NOT DISTINCT FROM ? RETURNING conversation_id""",
                (canonical(snapshot),time.time(),user_id,request_id,claim)).fetchone()
            if not row:return False
            if job and job.get('_source_guard'):
                from avatar.conversation.jobs.pipeline import validate_authorization
                validate_authorization(db,job)
            db.execute('INSERT OR REPLACE INTO chat_pending VALUES(?,?,?,?)',(row[0],user_id,canonical({**snapshot,'request_id':request_id}),time.time()))
        if changes := getattr(self.database, 'changes', None):
            changes.generation_committed(user_id, request_id)
        return True

    def finish(self,user_id,request_id,state,result=None,error=''):
        if state not in TERMINAL:raise ValueError('Invalid generation terminal state')
        with self.database.connect(write=True) as db:
            return self.finish_in(db,user_id,request_id,state,result,error)

    @staticmethod
    def finish_in(db,user_id,request_id,state,result=None,error=''):
        row=db.execute("SELECT * FROM chat_generation_jobs WHERE user_id=? AND request_id=? AND state IN ('queued','running') FOR UPDATE",(user_id,request_id)).fetchone()
        if not row or not current_claim(row):return False
        snapshot=snapshot_value(row['snapshot']);snapshot.update(done=state=='succeeded',state=state,error=error,status='回答完成' if state=='succeeded' else error)
        if result:snapshot.update(reply=result.get('reply',snapshot.get('reply','')),result=result,usage=result.get('usage'))
        if state=='cancelled' and result:
            snapshot.update(status=result['reply'],stage='cancelled',agent_pause_id=None)
            for event in snapshot.get('tool_events',[]):
                if event.get('status')=='running':event['status']='interrupted'
        snapshot.pop('user_question', None)
        if state=='awaiting_input':
            if result.get('user_question'):
                snapshot['user_question'] = result['user_question']
            for event in snapshot.get('tool_events',[]):
                if event.get('status')=='running':event['status']='paused'
            snapshot.update(status=result['status'],agent_pause_id=result['agent_pause_id'],stream_retryable=result.get('stream_retryable',False),stage=state,reply=result.get('reply',''))
        db.execute('UPDATE chat_generation_jobs SET state=?,snapshot=?,updated_at=? WHERE task_id=?',(state,canonical(snapshot),time.time(),row['task_id']))
        if row['conversation_id']:
            if state!='awaiting_input' and json.loads(row['authorization']).get('run_id'):
                from avatar.conversation.loop.store import RunStore
                key=run_key(row)
                if state in ('failed','cancelled','indeterminate'):
                    from avatar.conversation.pauses import settle_terminal_usage
                    settle_terminal_usage(db,row,key[-1])
                retained=state in ('failed','indeterminate') and db.execute("""SELECT 1 FROM chat_agent_pauses
                    WHERE conversation_id=? AND COALESCE(document::jsonb->>'run_id',
                    document::jsonb #>> '{snapshot,task_id}',pause_id)=?""",(row['conversation_id'],key[-1])).fetchone()
                if not retained:RunStore.clear_in(db,*key)
            db.execute('INSERT OR REPLACE INTO chat_pending VALUES(?,?,?,?)',(row['conversation_id'],user_id,canonical({**snapshot,'request_id':request_id}),time.time()))
            if row['state']=='running' or state=='succeeded':
                db.execute('UPDATE chat_conversations SET lease_until=0 WHERE id=? OR parent_id=?',(row['conversation_id'],row['conversation_id']))
        return True

    def interrupted_in(self,db,row):
        """Only restart a confirmed checkpoint. A queued transition also fences the old writer."""
        from avatar.conversation.loop.runtime import resume_safe
        from avatar.conversation.loop.store import RunStore
        scope=json.loads(row['authorization'])
        state=RunStore.inspect_in(db,*run_key(row)) if scope.get('run_id') else None
        safe = state and state.get('binding') and resume_safe(state,automatic=True)
        if safe:
            scope.pop('claim_token',None)
            scope['resume_run']=True
            snapshot=snapshot_value(row['snapshot'])
            snapshot.update(reply='',status='从已保存的进度继续',stage='queued',done=False,error='')
            db.execute("""UPDATE chat_generation_jobs SET state='queued',authorization=?,snapshot=?,updated_at=?
                WHERE task_id=?""",(canonical(scope),canonical(snapshot),time.time(),row['task_id']))
            db.execute('UPDATE chat_conversations SET lease_until=0 WHERE id=? OR parent_id=?',(row['conversation_id'],row['conversation_id']))
        else:
            self.finish_in(db,row['user_id'],row['request_id'],'indeterminate',error='原请求结果未确认，请检查历史；不会自动重发')
        return bool(safe)

    def interrupted(self,job):
        with self.database.connect(write=True) as db:
            row=db.execute("SELECT * FROM chat_generation_jobs WHERE task_id=? AND state='running' FOR UPDATE",(job['task_id'],)).fetchone()
            if row and current_claim(row,job):
                return self.interrupted_in(db,row)
        return False

    def recover(self,active_ids=()):
        """Poll without a write lock until a running claim expires."""
        cutoff=time.time()-LEASE_SECONDS
        with self.database.connect() as db:
            rows=db.execute("""SELECT state,task_id FROM chat_generation_jobs WHERE state='running'
                AND (updated_at<? OR authorization::jsonb->>'claim_token' IS NULL)
                UNION ALL (SELECT state,task_id FROM chat_generation_jobs WHERE state='queued' LIMIT 1)""",(cutoff,)).fetchall()
        queued=False
        for row in rows:
            if row['state']=='queued':
                queued=True
            elif row['task_id'] not in active_ids:
                with self.database.connect(write=True) as db:
                    job=db.execute("""SELECT * FROM chat_generation_jobs WHERE task_id=? AND state='running'
                        AND (updated_at<? OR authorization::jsonb->>'claim_token' IS NULL) FOR UPDATE""",(row['task_id'],cutoff)).fetchone()
                    if job and self.interrupted_in(db,job):queued=True
        return queued
