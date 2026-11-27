"""Durable browser-agent checkpoints scoped to the authenticated parent conversation."""
import json
import time
import uuid
from contextlib import ExitStack, nullcontext

from fastapi import HTTPException

from avatar.conversation.jobs.pipeline import current_generation, validate_authorization
from avatar.conversation.jobs.store import GenerationStore
from avatar.conversation.pause_state import available_outputs, checked, pending, settle_run_usage
from avatar.core.db import dump, now
from avatar.conversation.loop.personal import validate_sources_in, run_id_for
from avatar.conversation.loop.store import RunStore
from avatar.conversation.tools.activity import memory_evidence


def merged_work(request, task):
    supplement = request.message.strip() if request.agent_control == 'continue' else ''
    objective = task['objective'] + ('\n\n用户补充：' + supplement if supplement else '')
    files = list(dict.fromkeys([*task.get('attachment_ids',()), *request.attachment_ids]))
    return request.model_copy(update={'message':objective,'attachment_ids':files})


def continue_task(chat, user, request, parent, task):
    with chat.service.database.connect() as db:
        target = db.execute("""SELECT a.* FROM avatar_records a JOIN users u ON u.id=a.user_id
            WHERE a.aic=? AND a.user_id=? AND u.account_status='active'""",(task['aic'],task['owner'])).fetchone()
        session = db.execute("""SELECT * FROM chat_conversations WHERE id=? AND user_id=?
            AND (id=? OR parent_id=?)""",(task['session_id'],user['id'],parent['id'],parent['id'])).fetchone()
        if not task.get('sources'):
            raise HTTPException(409,'原任务缺少可核验的来源，请结束任务后重新提交')
        validate_sources_in(db,user['id'],parent['id'],task,task['sources'])
        artifacts = available_outputs(chat,db,user['id'],parent['id'],task['outputs'])
    if not target or not session:raise HTTPException(409,'原分身或会话已变更，无法继续此任务')
    if {item['id'] for item in artifacts} != set(task['outputs']):
        raise HTTPException(409,'原任务的已保存作品已删除或过期，无法按旧回执继续')
    work = merged_work(request,task)
    overview, context = chat.context(user,work,target,private_access=True,run_parent=parent['id'])
    overview['_parent_id']=parent['id']
    overview['_prior_tool_events']=task.get('activity', [])
    overview['_target']={'aic':target['aic'],'name':overview['agent']['profile'].get('avatar_name','我的分身')}
    cfg,key,system,history=context
    if request.agent_control=='continue' and (question:=task['snapshot'].get('user_question')):
        cfg={**cfg,'_answered_question':{**question,'answer':request.message.strip()}}
    if task['snapshot'].get('reply'):
        prefix,raw=system.split('\n',1)
        background=json.loads(raw)
        background['last_clarification']=task['snapshot']['reply']
        system=prefix+' last_clarification 是上一轮你向用户提出的问题，只用于理解本轮补充。\n'+dump(background)
    evidence = memory_evidence(task.get('activity', []))
    if evidence:
        history = [*history, {'role':'assistant','content':evidence}]
    context=({**cfg,'_resume_history':history,'_agent_checkpoint':task['checkpoint'],'_agent_saved_files':artifacts,'_agent_persisted_file_ids':task['outputs']},key,system,history)
    return overview,context,dict(session),work


def update_waiters(db, user_id, parent_id, pause_id, state, result):
    if not pause_id:return
    # Earlier request URLs also follow the task after another browser submits a decision.
    if state=='awaiting_input':
        changes={'status':result['status'],'reply':result['reply'],'agent_pause_id':result['agent_pause_id'],'stream_retryable':result.get('stream_retryable',False),'result':result}
    else:
        changes={'state':state,'stage':state,'done':state=='succeeded','status':result['reply'] if state=='cancelled' else '回答完成',
                 'reply':result['reply'],'result':result,'usage':result.get('usage'),'agent_pause_id':None}
    if result.get('user_question'):
        changes['user_question'] = result['user_question']
    db.execute("""UPDATE chat_generation_jobs SET state=?,snapshot=((snapshot::jsonb - 'user_question') || ?::jsonb)::text,updated_at=?
        WHERE user_id=? AND conversation_id=? AND state='awaiting_input'
        AND (snapshot::jsonb->>'agent_pause_id')=?""",(state,dump(changes),time.time(),user_id,parent_id,pause_id))


def finish(chat, db, user_id, parent_id, pause_id, message_id, *, ending=False):
    if not pause_id:return []
    task=pending(db,user_id,parent_id)
    if not task or task['snapshot']['agent_pause_id']!=pause_id:
        raise HTTPException(409,'原暂停状态已改变，请刷新会话')
    try:
        if task.get('sources'):
            validate_sources_in(db,user_id,parent_id,task,task['sources'])
        files=available_outputs(chat,db,user_id,parent_id,task['outputs']) if task.get('sources') else []
    except HTTPException:
        if not ending:raise
        files=[]
    if task.get('run_id'):
        if ending:settle_run_usage(db,user_id,parent_id,task['run_id'])
        RunStore.clear_in(db,user_id,'chat',parent_id,task['run_id'])
    for file in files:
        db.execute('UPDATE chat_files SET generated_message_id=? WHERE id=?',(message_id,file['id']))
    db.execute('DELETE FROM chat_agent_pauses WHERE conversation_id=? AND pause_id=?',(parent_id,pause_id))
    return files


def pause(chat, user, request, work, parent, session, overview, error, previous, *, db=None, job=None):
    pause_id=uuid.uuid4().hex
    job=job or current_generation.get()
    transaction = db
    result={'state':'awaiting_input','done':False,'reply':error.question or '','status':str(error),'usage':error.usage,
            'agent_pause_id':pause_id,'conversation_id':parent['id'],'message':request.message,'stage':'awaiting_input'}
    if error.user_question:
        result['user_question'] = error.user_question
    result['stream_retryable'] = bool(getattr(error,'stream_retryable',False))
    if job:result.update(request_id=job['request_id'],task_id=job['task_id'])
    owner=overview['agent']
    with ExitStack() as resources, (nullcontext(transaction) if transaction is not None else chat.service.database.connect(write=True)) as db:
        if transaction is None:
            validate_authorization(db,job,lock=True)
        checked(db,user['id'],request)
        if request.message.strip() and request.agent_control != 'retry':
            message_id=db.execute("""INSERT INTO chat_messages(user_id,channel,role,content,created_at,
                profile_modified_at,conversation_id) VALUES(?,?,?,?,?,?,?)""",
                (user['id'],session['channel'],'user',request.message,now(),owner.get('last_modified_at',''),session['id'])).lastrowid
            for file_id in overview.get('_attachment_ids',[]):
                db.execute('INSERT OR IGNORE INTO chat_file_usages(message_id,file_id) VALUES(?,?)',(message_id,file_id))
        run_id=run_id_for(job,previous,request)
        store=overview.get('_run_store')
        if store:
            if not job:overview['_source_guard'](db,full=True)
            run_id=store.run_id
            store.check_in(db)
        state=RunStore.inspect_in(db,user['id'],'chat',parent['id'],run_id) or {}
        result['usage']=error.usage or state.get('usage')
        # A durable file is recoverable only after its private receipt commits. A stop never publishes a draft.
        files=[] if state.get('binding') else list({item['id']:item
            for item in [*overview.get('_generated_files',[]),*getattr(error,'generated_files',[])]
            if item['id'] not in (previous or {}).get('outputs',())}.values())
        if files:resources.enter_context(chat.chat_files.storing_outputs(db,user['id'],parent['id'],None,files))
        trusted = previous if previous and previous.get('sources') else {}
        outputs=list(dict.fromkeys([*trusted.get('outputs',()),*(item['id'] for item in files)]))
        document={'run_id':run_id,'sources':state.get('sources') or overview.get('_run_sources') or (previous or {}).get('sources'),
                  'aic':owner['aic'],'owner':owner['user_id'],'session_id':session['id'],
                  'retry_dispatch':getattr(error,'retry_dispatch',False),
                  'checkpoint':error.checkpoint,'objective':work.message,
                  'goal':previous.get('goal',previous['objective']) if previous else work.message,
                  'attachment_ids':work.attachment_ids,'snapshot':result,'outputs':outputs,
                  'activity': [*trusted.get('activity', ()), *overview.get('_tool_events', ())]}
        db.execute('INSERT OR REPLACE INTO chat_agent_pauses(conversation_id,pause_id,document) VALUES(?,?,?)',
                   (parent['id'],pause_id,dump(document)))
        db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?',(now(),parent['id']))
        if result['usage']:
            cfg=owner.get('model_config') or state.get('binding',{})
            db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id) VALUES(?,?,?,?,?,?,?)',
                       (owner['user_id'],cfg['provider'],cfg['model'],dump(result['usage']),now(),user['id'],session['id']))
            if state:
                RunStore.mark_usage_recorded_for_in(db,user['id'],'chat',parent['id'],run_id)
        update_waiters(db,user['id'],parent['id'],request.agent_pause_id,'awaiting_input',result)
        if job:GenerationStore.finish_in(db,user['id'],job['request_id'],'awaiting_input',result)
    return result


def end_task(chat, user, parent, request):
    with chat.service.database.connect(write=True) as db:
        job=current_generation.get()
        validate_authorization(db,job,lock=True)
        result=end_paused_in(chat,db,user,parent,request.agent_pause_id)
        if job:GenerationStore.finish_in(db,user['id'],job['request_id'],'succeeded',result)
    return result


def end_paused_in(chat,db,user,parent,pause_id):
    reply='已结束本次任务。'
    row=db.execute("""INSERT INTO chat_messages(user_id,channel,role,content,created_at,profile_modified_at,
        conversation_id) VALUES(?,?,?,?,?,?,?)""",(user['id'],parent['channel'],'assistant',reply,now(),'',parent['id']))
    files=finish(chat,db,user['id'],parent['id'],pause_id,row.lastrowid,ending=True)
    db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?',(now(),parent['id']))
    result={'reply':reply,'mode':'agent_control','conversation_id':parent['id'],'generated_files':files}
    update_waiters(db,user['id'],parent['id'],pause_id,'cancelled',result)
    return result
