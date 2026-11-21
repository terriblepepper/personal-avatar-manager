"""Durable browser-agent checkpoints scoped to the authenticated parent conversation."""
import hashlib
import re
import json
import time
import uuid
from contextlib import ExitStack

from fastapi import HTTPException

from avatar.conversation.jobs.pipeline import current_generation, validate_authorization
from avatar.conversation.jobs.store import GenerationStore
from avatar.core.db import dump, now


def pending(db, user_id, parent_id):
    row = db.execute("""SELECT p.document FROM chat_agent_pauses p JOIN chat_conversations c
        ON c.id=p.conversation_id WHERE c.user_id=? AND c.id=?""", (user_id,parent_id)).fetchone()
    return json.loads(row['document']) if row else None


def checked(db, user_id, request):
    task = pending(db,user_id,request.conversation_id)
    if request.agent_control:
        if not task or task['snapshot']['agent_pause_id'] != request.agent_pause_id:
            raise HTTPException(409,'原暂停状态已改变，请刷新会话')
    elif task:
        raise HTTPException(409,'分身正在等待补充信息或结束任务，请刷新会话')
    return task


def numeric_receipts(db,user_id,parent_id,task):
    # Older pauses kept successful stdout in the job snapshot, but only action hashes in the checkpoint.
    signatures=set(task['checkpoint'].get('sandbox_actions',[]))
    if not signatures:return []
    receipts={}
    rows=db.execute("""SELECT snapshot FROM chat_generation_jobs WHERE user_id=? AND conversation_id=?
        AND state='awaiting_input' AND json_extract(snapshot,'$.agent_pause_id')=? ORDER BY created_at""",
        (user_id,parent_id,task['snapshot']['agent_pause_id']))
    for row in rows:
        for event in json.loads(row['snapshot']).get('tool_events',[]):
            args=event.get('input') or {};output=event.get('output') or {}
            if not isinstance(args,dict) or not isinstance(output,dict):continue
            if not args.get('code') or args.get('input_ids') or args.get('https_inputs') or output.get('outputs'):continue
            value=output.get('stdout','')
            if isinstance(value,str):value=value.strip()
            if output.get('status')!='success' or not isinstance(value,str) or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?',value[:2049]) or len(value)>2048:continue
            action=[['python',args['code']],args.get('args',[]),[]]
            token=hashlib.sha256(json.dumps(action,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if token in signatures:receipts[token]={'name':'Python 沙箱','status':'success','stdout':value}
    return list(receipts.values())


def continue_task(chat, user, request, parent, task):
    with chat.service.database.connect() as db:
        target = db.execute("""SELECT a.* FROM avatar_records a JOIN users u ON u.id=a.user_id
            WHERE a.aic=? AND a.user_id=? AND u.account_status='active'""",(task['aic'],task['owner'])).fetchone()
        session = db.execute("""SELECT * FROM chat_conversations WHERE id=? AND user_id=?
            AND (id=? OR parent_id=?)""",(task['session_id'],user['id'],parent['id'],parent['id'])).fetchone()
        receipts=numeric_receipts(db,user['id'],parent['id'],task)
        artifacts = available_outputs(chat,db,user['id'],parent['id'],task['outputs'])
    if not target or not session:raise HTTPException(409,'原分身或会话已变更，请结束任务后重新提问')
    message = task['objective']+'\n\n用户补充：'+request.message
    if len(message)>16000:raise HTTPException(422,'本任务累计输入已达上限，请结束后重新提问')
    work = request.model_copy(update={'message':message})
    overview, context = chat.context(user,work,target,private_access=True)
    overview['_parent_id']=parent['id']
    overview['_target']={'aic':target['aic'],'name':overview['agent']['profile'].get('avatar_name','我的分身')}
    cfg,key,system,history=context
    if task['snapshot'].get('reply'):
        prefix,raw=system.split('\n',1)
        background=json.loads(raw)
        background['last_clarification']=task['snapshot']['reply']
        system=prefix+' last_clarification 是上一轮你向用户提出的问题，只用于理解本轮补充。\n'+dump(background)
    context=({**cfg,'_agent_checkpoint':task['checkpoint'],'_agent_saved_files':artifacts,'_sandbox_receipts':receipts,
              '_resume_results_only':bool(receipts) and request.message.strip() in ('继续','继续。','continue') and '相同的沙箱操作' in task['snapshot']['status']},key,system,history)
    return overview,context,dict(session),work


def available_outputs(chat, db, user_id, parent_id, ids):
    if not ids:return []
    files=[dict(row) for row in db.execute("""SELECT id,filename,size,animation_metadata::jsonb->>'html_mode' AS html_mode FROM chat_files WHERE user_id=? AND
        conversation_id=? AND generated_message_id IS NULL AND origin='generated' AND
        (expires_at IS NULL OR julianday(expires_at)>julianday('now')) AND id IN ("""+
        ','.join('?' for _ in ids)+')',(user_id,parent_id,*ids))]
    return [{**item,'origin':'generated','download_url':f"/api/conversations/{parent_id}/files/{item['id']}"} for item in files
            if chat.chat_files.directory.new(user_id,item['id']+'.blob').is_file()]


def update_waiters(db, user_id, parent_id, pause_id, state, result):
    if not pause_id:return
    # Earlier request URLs also follow the task after another browser submits a decision.
    if state=='awaiting_input':
        changes={'status':result['status'],'reply':result['reply'],'agent_pause_id':result['agent_pause_id'],'result':result}
    else:
        changes={'state':state,'stage':state,'done':state=='succeeded','status':result['reply'] if state=='cancelled' else '回答完成',
                 'reply':result['reply'],'result':result,'usage':result.get('usage'),'agent_pause_id':None}
    db.execute("""UPDATE chat_generation_jobs SET state=?,snapshot=(snapshot::jsonb || ?::jsonb)::text,updated_at=?
        WHERE user_id=? AND conversation_id=? AND state='awaiting_input'
        AND json_extract(snapshot,'$.agent_pause_id')=?""",(state,dump(changes),time.time(),user_id,parent_id,pause_id))


def finish(chat, db, user_id, parent_id, pause_id, message_id):
    if not pause_id:return []
    task=pending(db,user_id,parent_id)
    if not task or task['snapshot']['agent_pause_id']!=pause_id:
        raise HTTPException(409,'原暂停状态已改变，请刷新会话')
    files=available_outputs(chat,db,user_id,parent_id,task['outputs'])
    for file in files:
        db.execute('UPDATE chat_files SET generated_message_id=? WHERE id=?',(message_id,file['id']))
    db.execute('DELETE FROM chat_agent_pauses WHERE conversation_id=? AND pause_id=?',(parent_id,pause_id))
    return files


def pause(chat, user, request, work, parent, session, overview, error, previous):
    pause_id=uuid.uuid4().hex
    job=current_generation.get()
    result={'state':'awaiting_input','done':False,'reply':error.question or '','status':str(error),'usage':error.usage,
            'agent_pause_id':pause_id,'conversation_id':parent['id'],'message':request.message,'stage':'awaiting_input'}
    if job:result.update(request_id=job['request_id'],task_id=job['task_id'])
    owner=overview['agent']
    with ExitStack() as resources, chat.service.database.connect(write=True) as db:
        validate_authorization(db,job)
        checked(db,user['id'],request)
        message_id=db.execute("""INSERT INTO chat_messages(user_id,channel,role,content,created_at,
            profile_modified_at,conversation_id) VALUES(?,?,?,?,?,?,?)""",
            (user['id'],session['channel'],'user',request.message,now(),'',session['id'])).lastrowid
        for file_id in overview.get('_attachment_ids',[]):
            db.execute('INSERT OR IGNORE INTO chat_file_usages(message_id,file_id) VALUES(?,?)',(message_id,file_id))
        files=list({item['id']:item for item in [*overview.get('_generated_files',[]),*getattr(error,'generated_files',[])]}.values())
        if files:resources.enter_context(chat.chat_files.storing_outputs(db,user['id'],parent['id'],None,files))
        outputs=list(dict.fromkeys([*(previous['outputs'] if previous else []),*(item['id'] for item in files)]))
        document={'aic':owner['aic'],'owner':owner['user_id'],'session_id':session['id'],
                  'checkpoint':error.checkpoint,'objective':work.message,'snapshot':result,'outputs':outputs}
        db.execute('INSERT OR REPLACE INTO chat_agent_pauses(conversation_id,pause_id,document) VALUES(?,?,?)',
                   (parent['id'],pause_id,dump(document)))
        db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?',(now(),parent['id']))
        if error.usage:
            cfg=owner['model_config']
            db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id) VALUES(?,?,?,?,?,?,?)',
                       (owner['user_id'],cfg['provider'],cfg['model'],dump(error.usage),now(),user['id'],session['id']))
        update_waiters(db,user['id'],parent['id'],request.agent_pause_id,'awaiting_input',result)
        if job:GenerationStore.finish_in(db,user['id'],job['request_id'],'awaiting_input',result)
    return result


def end_task(chat, user, parent, request):
    with chat.service.database.connect(write=True) as db:
        job=current_generation.get()
        validate_authorization(db,job)
        result=end_paused_in(chat,db,user,parent,request.agent_pause_id)
        if job:GenerationStore.finish_in(db,user['id'],job['request_id'],'succeeded',result)
    return result


def end_paused_in(chat,db,user,parent,pause_id):
    reply='已结束本次任务。'
    row=db.execute("""INSERT INTO chat_messages(user_id,channel,role,content,created_at,profile_modified_at,
        conversation_id) VALUES(?,?,?,?,?,?,?)""",(user['id'],parent['channel'],'assistant',reply,now(),'',parent['id']))
    files=finish(chat,db,user['id'],parent['id'],pause_id,row.lastrowid)
    db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?',(now(),parent['id']))
    result={'reply':reply,'mode':'agent_control','conversation_id':parent['id'],'generated_files':files}
    update_waiters(db,user['id'],parent['id'],pause_id,'cancelled',result)
    return result
