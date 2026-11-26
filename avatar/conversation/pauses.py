"""Durable browser-agent checkpoints scoped to the authenticated parent conversation."""
import json
import time
import uuid
from contextlib import ExitStack, nullcontext

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
        question = task['snapshot'].get('user_question')
        if request.question_id and (not question or request.question_id != question['id']):
            raise HTTPException(409,'分身提问已改变，请刷新后回答')
        if question and request.agent_control=='continue' and not request.message.strip():
            raise HTTPException(422,'请填写对分身提问的回答')
        if request.agent_control=='retry' and not task['snapshot'].get('stream_retryable'):
            raise HTTPException(409,'该任务需要补充信息才能继续')
    elif task:
        raise HTTPException(409,'分身已暂停，请刷新后继续任务')
    return task


def merged_work(request, task):
    supplement = request.message.strip() if request.agent_control == 'continue' else ''
    objective = task['objective'] + ('\n\n用户补充：' + supplement if supplement else '')
    files = list(dict.fromkeys([*task.get('attachment_ids',()), *request.attachment_ids]))
    return request.model_copy(update={'message':objective,'attachment_ids':files})


_PUBLIC_ACTIONS = frozenset("""find_material send_material search_materials read_material
    find_public_material send_public_material create_html edit_html append_html_draft
    list_avatars contact_avatar read_session_file read_step_receipt require_conversation_tool
    run_python ask_user get_current_time wait_for_user end_task send_message""".split())


def public_event(event):
    if event.get('type','tool') != 'tool':
        return event
    public={key:event[key] for key in ('type','id','group','name','phase','status','started_at','finished_at')
            if key in event}
    if event.get('group') in ('model_io','tool_io'):
        # Projected at the model boundary according to the executing owner's scope; never include transport fields.
        for section, fields in (('input', ('message','message_chars','history_turns','tools','tool_count','attempt','tool','arguments')),
                                ('output', ('text','text_chars','kind','received_bytes','tool_calls','finish_reason','reason','status_code','attempt','result'))):
            data = event.get(section)
            if isinstance(data, dict):
                public[section] = {key:data[key] for key in fields if key in data}
        return public
    for section in ('input','output'):
        data=event.get(section)
        if not isinstance(data,dict):continue
        fields={k:data[k] for k in ('goal_no','iteration','planned_steps','file_count','count','accepted_bytes')
                if type(data.get(k)) is int}
        for key,allowed in (('tool',_PUBLIC_ACTIONS),('action',_PUBLIC_ACTIONS),('stage',('route','resume_route')),
                            ('operation',('check_file_receipt','select_materials')),('mode',('read','search','answer')),
                            ('file_access',('directory','content'))):
            if isinstance(data.get(key),str) and data[key] in allowed:
                fields[key]=data[key]
        for key in ('tools','tool_names'):
            if isinstance(data.get(key),list):
                fields[key]=[tool for tool in data[key] if isinstance(tool,str) and tool in _PUBLIC_ACTIONS]
        if fields:public[section]=fields
    return public


def continue_task(chat, user, request, parent, task):
    with chat.service.database.connect() as db:
        target = db.execute("""SELECT a.* FROM avatar_records a JOIN users u ON u.id=a.user_id
            WHERE a.aic=? AND a.user_id=? AND u.account_status='active'""",(task['aic'],task['owner'])).fetchone()
        session = db.execute("""SELECT * FROM chat_conversations WHERE id=? AND user_id=?
            AND (id=? OR parent_id=?)""",(task['session_id'],user['id'],parent['id'],parent['id'])).fetchone()
        if not task.get('sources'):
            raise HTTPException(409,'原任务缺少可核验的来源，请结束任务后重新提交')
        from avatar.conversation.loop.personal import validate_sources_in
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
    from avatar.conversation.tools.activity import memory_evidence
    evidence = memory_evidence(task.get('activity', []))
    if evidence:
        history = [*history, {'role':'assistant','content':evidence}]
    context=({**cfg,'_resume_history':history,'_agent_checkpoint':task['checkpoint'],'_agent_saved_files':artifacts,'_agent_persisted_file_ids':task['outputs']},key,system,history)
    return overview,context,dict(session),work


def available_outputs(chat, db, user_id, parent_id, ids):
    if not ids:return []
    files=[dict(row) for row in db.execute("""SELECT id,filename,size,sha256,animation_metadata::jsonb->>'html_mode' AS html_mode FROM chat_files WHERE user_id=? AND
        conversation_id=? AND generated_message_id IS NULL AND origin='generated' AND
        (expires_at IS NULL OR julianday(expires_at)>julianday('now')) AND id IN ("""+
        ','.join('?' for _ in ids)+')',(user_id,parent_id,*ids))]
    from avatar.files.conversation.resources import original_hashes
    available=[]
    for item in files:
        path=chat.chat_files.directory.path(user_id,parent_id,item['id']+'.blob')
        expected=item.pop('sha256')
        try:
            digest,_=original_hashes(path,item['size'])
        except FileNotFoundError:
            continue
        if expected and digest!=expected:
            raise HTTPException(409,'已保存作品的原件校验失败')
        available.append({**item,'origin':'generated','download_url':f"/api/conversations/{parent_id}/files/{item['id']}"})
    return available


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
        AND json_extract(snapshot,'$.agent_pause_id')=?""",(state,dump(changes),time.time(),user_id,parent_id,pause_id))


def finish(chat, db, user_id, parent_id, pause_id, message_id, *, ending=False):
    if not pause_id:return []
    task=pending(db,user_id,parent_id)
    if not task or task['snapshot']['agent_pause_id']!=pause_id:
        raise HTTPException(409,'原暂停状态已改变，请刷新会话')
    from avatar.conversation.loop.personal import validate_sources_in
    from avatar.conversation.loop.store import RunStore
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
        from avatar.conversation.loop.personal import run_id_for
        from avatar.conversation.loop.store import RunStore
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


def settle_run_usage(db,user_id,parent_id,run_id):
    from avatar.conversation.loop.store import RunStore
    key=(user_id,'chat',parent_id,run_id)
    state=RunStore.inspect_in(db,*key) or {}
    binding=state.get('binding',{})
    if state.get('usage') and binding.get('provider') and binding.get('model'):
        db.execute("""INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id)
            VALUES(?,?,?,?,?,?,?)""",(binding['owner'],binding['provider'],binding['model'],dump(state['usage']),
                                     now(),user_id,binding['session_id']))
        RunStore.mark_usage_recorded_for_in(db,*key)


def settle_terminal_usage(db,row,run_id):
    settle_run_usage(db,row['user_id'],row['conversation_id'],run_id)


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
