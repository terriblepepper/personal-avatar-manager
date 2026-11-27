"""Execute and commit a single peer turn."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack
from fastapi import HTTPException
from avatar.core.db import now, dump
from avatar.core.execution import AgentNeedsInput, execution_guard, run_blocking, finish_on_cancel
from avatar.conversation.loop.peer import PeerTurn, check_peer_style, speaker as turn_speaker
from avatar.conversation.collaboration.inbox import notify
from avatar.conversation.collaboration.tools import TASK_TOOLS, task_tool_result
from avatar.conversation.tools.activity import without_false_tool_footer, ToolActivity, memory_evidence
from avatar.sandbox.python_tool import AtomicPython
from avatar.conversation.collaboration.results import confirmed_message
from avatar.conversation.collaboration.preparation import prepare_turn, build_prompt
from avatar.conversation.loop.entry_tools import bind_entry_tools, EntryToolContext, SessionFileAccess
from avatar.conversation.loop.public_materials import bind_public_material_tools
from avatar.conversation.tools.reply_artifacts import reply_with_artifacts, without_source_footer
from avatar.files.conversation.resources import original_hashes
from avatar.files.library.delivery import SavedMaterials
from avatar.models.context_budget import fit_turn


class PeerTurnExecutor:
    def __init__(self, database, chat, sessions, resources, transport, apply_inputs, close_record, flush_controls):
        self.database = database
        self.chat = chat
        self.sessions = sessions
        self.resources = resources
        self.transport = transport
        self.apply_inputs = apply_inputs
        self.close_record = close_record
        self.flush_controls = flush_controls

    async def billed(self,job,speaker,config,call,purpose):
        try:
            result=await call
        except AgentNeedsInput as paused:
            if not paused.usage:raise
            result=paused
        if purpose=='agent_dialogue' and (run := job.get('_peer_run')):
            run.charge(config,result.usage)
        else:
            def record_usage():
                with self.database.connect(write=True) as db:
                    db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,purpose,caller_id) VALUES(?,?,?,?,?,?,?)',(speaker['user_id'],config['provider'],config['model'],dump(result.usage),now(),purpose,job['user_id']))
            await run_blocking(record_usage)
        if isinstance(result,AgentNeedsInput):raise result
        return result

    @staticmethod
    def needs_peer_reply(db,identifier,speaker_aic):
        """补充后的己方发言须先交给对方回复；从原文计算，不受摘要和页面刷新影响。"""
        return bool(db.execute("""SELECT 1 FROM agent_dialogue_messages u WHERE u.dialogue_id=?
          AND u.speaker_role='user' AND u.speaker_name!='用户授权共享资料'
          AND NOT EXISTS(SELECT 1 FROM agent_dialogue_messages a WHERE a.dialogue_id=u.dialogue_id
            AND a.speaker_role='agent' AND a.speaker_aic!=? AND a.id>u.id) LIMIT 1""",(identifier,speaker_aic)).fetchone())

    @staticmethod
    def owner_can_stop(db,identifier,speaker):
        last=db.execute('SELECT speaker_user_id,speaker_aic,speaker_role,speaker_name FROM agent_dialogue_messages WHERE dialogue_id=? ORDER BY id DESC LIMIT 1',(identifier,)).fetchone()
        if not last or last['speaker_role']!='user' or last['speaker_name']!='用户补充要求':
            return False
        return last['speaker_user_id']==speaker['user_id'] if last['speaker_user_id'] else last['speaker_aic']==speaker['aic']

    # 接收方独立加载本人公开资料、本人技能与本人会话摘要；不读取对方私密档案。
    async def answer_peer(self,job,speaker_id):
        if (text := confirmed_message(job,speaker_id)) is not None:
            return SimpleNamespace(text=text,usage=None,task_action=None)
        run, answer = await run_blocking(self.begin_answer, job, speaker_id)
        if answer is not None:
            return answer
        events = []
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def enqueue(event):
            if event['type'] == 'tool':
                loop.call_soon_threadsafe(queue.put_nowait, event)

        async def persist_activity():
            while (event := await queue.get()) is not None:
                batch = {event['id']:event}
                while not queue.empty():
                    event = queue.get_nowait()
                    if event is None:
                        break
                    batch[event['id']] = event
                await run_blocking(self.record_turn_activity, job, speaker_id, batch.values())
                if event is None:
                    return

        writer = asyncio.create_task(persist_activity())

        async def drain_activity():
            queue.put_nowait(None)
            await writer

        activity = ToolActivity(events, enqueue)
        try:
            try:
                with execution_guard(run.check), activity.track(private_io=True) as progress:
                    return await self._answer_peer(job, speaker_id, progress, events)
            finally:
                await finish_on_cancel(drain_activity())
        except AgentNeedsInput as pause:
            await run_blocking(self.save_pause, job, speaker_id, run, pause)
            raise

    def begin_answer(self, job, speaker_id):
        run = PeerTurn(self, job, speaker_id)
        job['_peer_run'] = run
        with self.database.connect() as db:
            run.validate(db)
            current = self.sessions.owned(db, {'id': speaker_id}, job['id'])
            if receipt := self.verified_delivery(db, current, speaker_id):
                return run, SimpleNamespace(**receipt['answer'])
        return run, None

    def save_pause(self, job, speaker_id, run, pause):
        outputs=[f for f in pause.generated_files if not f.get('public_material_id')]
        with ExitStack() as cleanup, self.database.connect(write=True) as db:
            run.commit(db)
            task=self.sessions.owned(db,{'id':speaker_id},job['id'])
            if task['status']!='running' or task['revision']!=job['revision']:
                raise HTTPException(409,'任务状态已变化，未保存本轮输出') from None
            workspace=self.resources.workspace(db,speaker_id,job['session_id'],job['id'])
            prior=self.saved_outputs(db,task,speaker_id,workspace)
            persisted = {f['id'] for f in prior}
            outputs = [item for item in outputs if item['id'] not in persisted]
            if outputs:
                cleanup.enter_context(self.chat.chat_files.storing_outputs(db,speaker_id,workspace,None,outputs))
            held = json.loads(task['agent_checkpoint'] or '{}')
            old_activity = held.get('activity', []) if held.get('speaker_id')==speaker_id else []
            checkpoint={**pause.checkpoint,'speaker_id':speaker_id,'peer_style':run.style,'file_ids':[item['id'] for item in [*prior,*outputs]],
                        'source_revision':job.get('_output_source_revision'),
                        'activity':[*old_activity,*json.loads(task['tool_activity'])]}
            if pause.user_question:
                checkpoint['user_question']=pause.user_question
            db.execute('UPDATE agent_dialogues SET agent_checkpoint=? WHERE id=?',(dump(checkpoint),job['id']))
            pause.usage = run.settle(db)

    def verified_delivery(self, db, task, owner, *, text=None, lock=False):
        receipt = json.loads(task['pending_file_delivery'] or '{}')
        if receipt.get('revision') != task['revision'] or receipt.get('speaker') != owner:
            return None
        workspace = receipt['workspace']
        answer = receipt['answer']['text']
        if (text is not None and answer != text) or receipt['reply_sha'] != hashlib.sha256(answer.encode()).hexdigest():
            raise HTTPException(409, '分身答复与交付回执不一致')
        if receipt['authority'] != PeerTurn.source_version(self, db, task, owner, workspace, style=receipt['peer_style']):
            raise HTTPException(409, '分身资料或工具授权已改变，未交付答复')
        if proofs := receipt.get('session_file_reads'):
            self.resources.validate_file_receipts(db, task, owner, workspace, proofs, lock=lock)
        return receipt

    def output_revision(self,db,job,speaker_id,files=None,*,style=None):
        # 补充前撤销的资料权限也约束旧草稿的交付。
        sources=db.execute('SELECT a.aic,a.identity_status,a.last_modified_at,u.account_status,c.updated_at,k.updated_at AS skill_revision,s.build_id '
                           'FROM avatar_records a JOIN users u ON u.id=a.user_id LEFT JOIN attachment_collections c ON c.user_id=a.user_id '
                           'LEFT JOIN agent_skill_collections k ON k.user_id=a.user_id LEFT JOIN active_souls s ON s.user_id=a.user_id WHERE a.user_id=?',
                           (speaker_id,)).fetchone()
        files=self.resources.model_files(db,job['id'],speaker_id) if files is None else files
        identity=list(sources) if sources else []
        if style is not None:
            check_peer_style(db,speaker_id,style)
            if identity:
                identity[-1]=style['id']
        scope=[identity,sorted((f['id'],f['sha256'],f['description']) for f in files)]
        if sources and sources['skill_revision']:
            scope.append('skills-paused')
        return hashlib.sha256(dump(scope).encode()).hexdigest()

    def saved_outputs(self,db,job,speaker_id,workspace):
        checkpoint=json.loads(job['agent_checkpoint']) if job.get('agent_checkpoint') else {}
        ids=checkpoint.get('file_ids') if checkpoint.get('speaker_id')==speaker_id else []
        if not ids:return []
        if checkpoint.get('source_revision')!=self.output_revision(db,job,speaker_id,style=checkpoint.get('peer_style')):
            raise HTTPException(409,'原作品的资料授权已变化')
        storage_owner=self.chat.chat_files.session(db,speaker_id,workspace)['user_id']
        rows=db.execute("SELECT id,filename,size,sha256,animation_metadata::jsonb->>'html_mode' AS html_mode FROM chat_files WHERE user_id=? AND conversation_id=? AND origin='generated' "
                        "AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP) AND id IN ("+','.join('?' for _ in ids)+')',
                        (storage_owner,workspace,*ids))
        saved = []
        for row in rows:
            path = self.chat.chat_files.directory.path(storage_owner,workspace,row['id']+'.blob')
            try:
                actual, _ = original_hashes(path,row['size'])
            except FileNotFoundError:
                break
            if row['sha256'] and row['sha256'] != actual:
                raise HTTPException(409,'已保存的分身作品校验失败')
            saved.append({**dict(row),'download_url':'/api/conversations/'+workspace+'/files/'+row['id']})
        if {item['id'] for item in saved} != set(ids):
            raise HTTPException(409,'原分身作品已删除或过期')
        return saved

    @staticmethod
    def memory_query(session_id,task_id,through,user_id):
        scope = 'd.id=?' if task_id else "d.session_id=? AND d.delivery_mode='message'"
        query = (' FROM agent_dialogue_messages m JOIN agent_dialogues d ON d.id=m.dialogue_id WHERE '+scope+
                 ' AND m.id>? AND NOT EXISTS(SELECT 1 FROM agent_task_members tm WHERE tm.task_id=d.id AND tm.user_id=? AND tm.hidden=1)')
        return query,(task_id or session_id,through,user_id)

    def memory_rows(self,db,session_id,task_id,through,user_id):
        query,params = self.memory_query(session_id,task_id,through,user_id)
        rows=[dict(r) for r in db.execute('SELECT m.*,d.objective AS task_title'+query+' ORDER BY m.id',params)]
        for row in rows:
            row['content']=self.resources.localize(db,row['content'],row['dialogue_id'],user_id,row['id'])
            evidence=memory_evidence(item for item in json.loads(row['tool_activity']) if item.get('user_id')==user_id)
            if evidence:
                row['content']+='\n\n'+evidence
        return rows

    def record_turn_activity(self, job, speaker_id, events):
        """持久化当前发言的工具进度，过期任务不能覆盖新任务状态。"""
        with self.database.connect(write=True) as db:
            row = db.execute('SELECT tool_activity,revision,status,next_speaker,user_id,target_user_id FROM agent_dialogues WHERE id=?',(job['id'],)).fetchone()
            if not row or row['revision'] != job['revision'] or row['status'] != 'running' or turn_speaker(row)!=speaker_id:
                return
            activities = json.loads(row['tool_activity'])
            by_id = {item['id']:item for item in activities}
            for event in events:
                item = by_id.get(event['id'])
                if item is None:
                    item = {'user_id': speaker_id}
                    by_id[event['id']] = item
                    activities.append(item)
                item.update({k:v for k,v in event.items() if k != 'type'})
            db.execute('UPDATE agent_dialogues SET tool_activity=? WHERE id=?',(dump(activities),job['id']))

    async def _answer_peer(self,job,speaker_id,progress,events):
        turn = await run_blocking(prepare_turn, self, job, speaker_id)
        run = job['_peer_run']
        config, key, system = turn.config, turn.key, turn.system
        if config.get('_direct_chat'):

            system += '\n以分身身份回应最新来信或用户补充，仅返回本轮文字。'
            history = [{'role':'user','content':row['speaker_name']+'：'+row['content']}
                       for row in turn.rows[-self.chat.memory.policy.recent_messages:]
                       if row['speaker_name']!='用户授权共享资料']
            message = '当前会话请求：'+job['objective']
            history, _ = fit_turn(config,system,history,message,[])
            progress({'type':'status','stage':'result_writing','message':'正在根据已审核资料作答'})
            result=await self.billed(job,turn.speaker,config,self.chat.model_client.generate(config,key,system,history,message),'agent_dialogue')
            text=result.text.strip()
            if not text:
                raise HTTPException(502,'模型未返回有效发言')
            # Plain chat owes at most one answer; the turn ledger still delivers newer supplements before closing.
            action=None if turn.ordinary else {'name':'end_task','arguments':{'reason':'普通聊天答复已交付','content':text[:60000]}}
            if action:
                text=action['arguments']['content']
            return await self._commit_peer_reply(job,config,turn.workspace,(),result,text,action,progress)
        config, system, history, message, settings, checkpoint = await run_blocking(build_prompt, self, turn, job, speaker_id)
        progress({'type': 'status', 'stage': 'intent_analysis', 'message': '正在分析本轮协作目标和可用信息'})
        summary=turn.memory['summary']
        if self.chat.memory.needs_compression(summary,history,message,config,system) and turn.rows:
            progress({'type': 'status', 'stage': 'compressing', 'message': '正在压缩较早会话记忆'})
            split=self.chat.memory.compaction_split(config,system,summary,history,message,force=True)
            result=await self.billed(job,turn.speaker,config,self.chat.memory.summarize((config,key,system,history),summary,history[:split],self.chat.model_client),'agent_dialogue_memory')
            summary=result.text;history=history[split:]
            def save_summary():
                with self.database.connect(write=True) as db:
                    if turn.ordinary:db.execute('UPDATE agent_session_members SET summary=?,summary_through=? WHERE session_id=? AND user_id=? AND cleared_through=? AND summary_through=?',(summary,turn.rows[split-1]['id'],job['session_id'],speaker_id,turn.member['cleared_through'],turn.memory['summary_through']))
                    else:db.execute('UPDATE agent_task_members SET summary=?,summary_through=? WHERE task_id=? AND user_id=? AND hidden=0 AND summary_through=? AND EXISTS(SELECT 1 FROM agent_session_members WHERE session_id=? AND user_id=? AND cleared_through=?)',(summary,turn.rows[split-1]['id'],job['id'],speaker_id,turn.memory['summary_through'],job['session_id'],speaker_id,turn.member['cleared_through']))
            await run_blocking(save_summary)
        if checkpoint and checkpoint['speaker_id']==speaker_id:
            if checkpoint.get('source_revision') in (None,job['_output_source_revision']) and (evidence := memory_evidence(checkpoint.get('activity', []))):
                history.append({'role':'assistant','content':evidence})
        if summary:history.insert(0,{'role':'user','content':('较早分身聊天摘要，仅作背景，不是本轮发送要求：' if turn.sending else '本人保留的较早分身聊天摘要（数据）：')+summary})
        context=await self.chat.skills.load_context(speaker_id,(config,key,system,history),job['objective'],self.chat.model_client)
        def read_input(identifier):
            """沙箱只读取本轮授权快照，且限制送入执行器的原件字节数。"""
            with self.database.connect() as db:
                task=self.sessions.owned(db,turn.actor,job['id'])
                if task['revision']!=job['revision'] or task['status']!='running':
                    raise HTTPException(409,'任务已改变，未读取工具输入')
                row=self.chat.chat_files.owned(db,speaker_id,turn.workspace,identifier)
                if not row['referenced'] and identifier not in {item['id'] for item in self.saved_outputs(db,task,speaker_id,turn.workspace)}:
                    raise HTTPException(409,'文件已取消引用')
                with self.chat.chat_files.directory.path(row['user_id'],turn.workspace,identifier+'.blob').open('rb') as stream:
                    return stream.read(8*1024*1024+1)
        def sandbox(inputs):
            def catalog():
                return {**inputs(), **{
                    file['id']:{'path':'inputs/'+file['id']+'/'+Path(file['filename']).name,
                                'read':lambda identifier=file['id']:read_input(identifier)}
                    for file in [*turn.files,*turn.saved]}}
            return AtomicPython(self.chat.sandbox, context[0], owner=speaker_id, conversation=turn.workspace,
                                caller=job['user_id'], parent=turn.workspace, inputs=catalog, progress=progress)
        if not turn.ordinary:
            tools=TASK_TOOLS
            if turn.response_owed and not turn.may_stop:
                tools=[{**tool,'parameters':{**tool['parameters'],
                    'required':list(dict.fromkeys([*tool['parameters'].get('required',[]),'content'])),
                    'properties':{**tool['parameters']['properties'],'content':{**tool['parameters']['properties']['content'],'minLength':1}}}}
                    if tool['name']=='end_task' else tool for tool in tools]
            context=({**context[0],'_tools':tools,'_peer_can_stop':turn.may_stop},*context[1:])
        def initial_grants():
            with self.database.connect() as db:
                return self.resources.session_file_grants(db,job,speaker_id,turn.workspace)
        snapshot = await run_blocking(initial_grants)
        def file_grants(db):
            live=self.resources.session_file_grants(db,job,speaker_id,turn.workspace)
            return {identifier:digest for identifier,digest in snapshot.items() if live.get(identifier)==digest}
        context=await bind_entry_tools(self.chat, context, EntryToolContext(speaker_id,turn.speaker['aic'],{'id':turn.workspace},turn.files),
                                       sandbox=sandbox if settings['python'] else None, progress=progress,
                                       session_access=SessionFileAccess(turn.workspace, grants=file_grants),
                                       directory_reply=self.chat.assistant_router.catalog_reply)
        run.validate_session_files=context[0].get('_validate_session_files_in')
        if not turn.sending:
            library = SavedMaterials(self.chat.service.attachments, speaker_id)
            bind_public_material_tools(self.chat, context[0], library, speaker_id, turn.store, turn.state,
                                       workspace=turn.workspace, task_id=job['id'])
        result=await self.billed(job,turn.speaker,context[0],self.chat.rag.answer(context,message,progress=progress),'agent_dialogue')
        action=None;text=result.text
        if not turn.ordinary:
            text,_,_,_,action=task_tool_result(getattr(result,'tool_calls',()))
            if context[0].get('_sandbox_succeeded'):
                text=without_false_tool_footer(text)
        outputs=[*context[0].get('_generated_files',()), *context[0].get('_react_public_files',())]
        text = reply_with_artifacts(without_source_footer(text), outputs, events, saved=turn.saved)
        if action:action['arguments']['content']=text
        if validate_sandbox := context[0].get('_validate_sandbox'):
            await run_blocking(validate_sandbox)
        return await self._commit_peer_reply(job,context[0],turn.workspace,turn.saved,result,text,action,progress)

    async def _commit_peer_reply(self,job,config,workspace,saved,result,text,action,progress):
        answer = await run_blocking(self.store_answer, job, config, workspace, saved, text, action)
        progress({'type': 'status', 'stage': 'result_ready',
                  'message': '需要开启会话工具' if result.finish_reason=='tool_required' else '结果已生成'})
        return answer

    def store_answer(self,job,config,workspace,saved,text,action):
        run=job['_peer_run']
        speaker_id=run.owner
        outputs=[*config.get('_generated_files',()), *config.get('_react_public_files',())]
        with ExitStack() as stack, self.database.connect(write=True) as db:
            run.commit(db)
            public=[item for item in outputs if item.get('public_material_id')]
            saved_ids={item['id'] for item in saved}
            generated=[item for item in outputs if not item.get('public_material_id') and item['id'] not in saved_ids]
            if generated:
                stack.enter_context(self.chat.chat_files.storing_outputs(db,speaker_id,workspace,None,generated))
            answer = {'text':text,'usage':run.settle(db),'task_action':action}
            # 本地交付与已验证的答复共用一个事务，重启后只提取收据。
            delivery={'speaker':speaker_id,'revision':job['revision'],'reply_sha':hashlib.sha256(text.encode()).hexdigest(),
                      'authority':run.source,'peer_style':run.style,'workspace':workspace,'answer':answer,
                      'public':public,'file_ids':list(dict.fromkeys(item['id'] for item in [*saved,*outputs]))}
            if proofs:=config.get('_session_read_proofs'):
                delivery['session_file_reads']=proofs
            db.execute('UPDATE agent_dialogues SET pending_file_delivery=? WHERE id=?',(dump(delivery),job['id']))
        return SimpleNamespace(**answer)

    # 发言使用发送前选定的通道；ACPS 以真实证书复用 taskId/sessionId，内部通道保持相同会话与权限边界。
    async def turn(self,job):
        transport = self.transport()
        if transport is None:raise HTTPException(503,'ACPS 分身传输未就绪，请先完成正式证书及共享入口部署')
        speaker, peer = await run_blocking(self.prepare_delivery, job)
        result=await transport.exchange(job,peer['user_id'],speaker['user_id'])
        if await run_blocking(self.deliver, job, speaker, peer, result):
            await self.flush_controls(job['id'])

    def prepare_delivery(self, job):
        with self.database.connect() as db:
            own,target=self.sessions.participants(db,job['user_id'],job['target_aic'])
            if own['aic']!=job['own_aic']:raise HTTPException(409,'分身身份已变更，请刷新任务')
        speaker,peer=(own,target) if job['next_speaker']==0 else (target,own)
        with self.database.connect(write=True) as db:
            db.execute("UPDATE agent_dialogues SET active_speaker=?,tool_activity='[]' WHERE id=? AND revision=? AND status='running'",
                       (json.loads(speaker['profile']).get('avatar_name','分身'),job['id'],job['revision']))
        return speaker, peer

    def deliver(self, job, speaker, peer, result):
        if result.get('needs_input'):
            checkpoint={'v':1,'speaker_id':speaker['user_id'],'sandbox_attempted':result['checkpoint'].get('sandbox_attempted') is True}
            with self.database.connect(write=True) as db:
                db.execute("UPDATE agent_dialogues SET status='awaiting_input',waiting_reason=?,agent_checkpoint=COALESCE(agent_checkpoint,?),revision=revision+1,confirmation_required=0,active_speaker='',error='',updated_at=? WHERE id=? AND revision=? AND status='running'",
                           (result['needs_input'],dump(checkpoint),now(),job['id'],job['revision']))
            return
        ordinary=job['delivery_mode']=='message'
        if (confirmed := confirmed_message(job,speaker['user_id'])) is not None:
            if result['text']!=confirmed:raise HTTPException(502,'通信回执与确认正文不符，本次未发布消息')
            text,state,waiting,required=confirmed,'queued','',0
        elif ordinary:
            text,state,waiting,required=result['text'],'queued','',0
        else:
            text,state,waiting,required,_=task_tool_result([result.get('task_action')])
        done=state=='ended'
        if ordinary:done=job['next_speaker']==1;state='queued';required=0;waiting=''
        if not text and state not in ('ended','awaiting_input'):raise HTTPException(502,'接收方未返回有效发言')
        with ExitStack() as cleanup,self.database.connect(write=True) as db:
            current=db.execute('SELECT * FROM agent_dialogues WHERE id=?',(job['id'],)).fetchone()
            if not current or current['revision']!=job['revision'] or current['status']!='running':return
            pending=bool(db.execute("SELECT 1 FROM agent_pending_inputs WHERE task_id=? AND applied=0 AND (message!='' OR skill_commands!='' OR removed_skills='[]')",(job['id'],)).fetchone())
            reply_owed=self.needs_peer_reply(db,job['id'],speaker['aic'])
            response_owed=self.needs_peer_reply(db,job['id'],peer['aic'])
            task_action=result.get('task_action') or {}
            action_name='send_message' if confirmed is not None else task_action.get('name','')
            explicit_stop=not ordinary and done and action_name=='end_task' and task_action['arguments'].get('stop_requested') is True
            if explicit_stop and not self.owner_can_stop(db,job['id'],speaker):
                raise HTTPException(409,'当前发言方没有新的本人补充可授权停止任务')
            if ordinary and response_owed:
                done=True
            if done and not ordinary and not explicit_stop and not text.strip() and response_owed:
                raise HTTPException(502,'分身未返回对最新补充的回复正文，任务未结束')
            if done and (pending or reply_owed and not explicit_stop):
                if not text.strip():raise HTTPException(502,'最新补充尚未得到对方回复，分身未返回可转达的正文，任务未结束')
                done=False;state='queued';waiting='';required=0;action_name='send_message'
            turn=db.execute('SELECT COALESCE(MAX(turn),0)+1 FROM agent_dialogue_messages WHERE dialogue_id=?',(job['id'],)).fetchone()[0]
            held=json.loads(current['agent_checkpoint'] or '{}')
            prior=held.get('activity', []) if held.get('speaker_id')==speaker['user_id'] else []
            activity=dump([] if confirmed is not None else [*prior,*json.loads(current['tool_activity'])])
            mode=result.get('transport_mode','acps')
            proposal=None
            if confirmed is not None and not ordinary:
                proposal=db.execute("UPDATE agent_dialogue_messages SET state=?,transport_mode=? WHERE dialogue_id=? AND turn=1 AND state='awaiting_acceptance' RETURNING id",
                                    (state,mode,job['id'])).fetchone()
            if proposal:
                message_id=proposal['id']
            else:
                message_id=db.execute('''INSERT INTO agent_dialogue_messages(dialogue_id,turn,speaker_aic,speaker_name,
                    speaker_user_id,content,usage,created_at,state,state_detail,tool_activity,transport_mode,task_action)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (job['id'],turn,speaker['aic'],json.loads(speaker['profile']).get('avatar_name','分身'),speaker['user_id'],
                     text,dump(result.get('usage')),now(),state,waiting,activity,mode,action_name)).lastrowid
            uid = speaker['user_id']
            delivery = self.verified_delivery(db, current, uid, text=result['text'], lock=True)
            if delivery and delivery['file_ids']:
                workspace = delivery['workspace']
                paths = cleanup.enter_context(self.chat.chat_files.storing_outputs(db,uid,workspace,None,delivery['public']))
                paths.extend(self.resources.share(db,{'id':uid},job['id'],delivery['file_ids'],workspace,generated=True,
                                                  authorize_ids={item['id'] for item in delivery['public']}))
            if delivery:
                db.execute('UPDATE agent_dialogues SET pending_file_delivery=NULL WHERE id=?',(job['id'],))
            PeerTurn.clear(db, job['id'], speaker['user_id'])
            db.execute('UPDATE agent_task_files SET message_id=? WHERE task_id=? AND source_user_id=? AND message_id IS NULL',(message_id,job['id'],speaker['user_id']))
            if ordinary:
                db.execute('UPDATE agent_session_members SET hidden=0 WHERE session_id=?',(job['session_id'],))
            count=db.execute("SELECT COUNT(*) FROM agent_dialogue_messages WHERE dialogue_id=? AND speaker_role='agent'",(job['id'],)).fetchone()[0]
            rounds=job['rounds_done']+int(count%2==0)
            checkpoint=current['agent_checkpoint'] if not done and state=='queued' and held.get('speaker_id') not in (None,speaker['user_id']) else None
            if not done and not ordinary and not pending and not reply_owed and state=='queued':
                pause_reason=''
                if rounds>=job['round_limit']:
                    pause_reason='已达到会话自动轮次。可以继续，或补充信息后继续。'
                elif count>=4:
                    recent=[tuple(r) for r in db.execute('SELECT speaker_role,speaker_aic,content FROM agent_dialogue_messages WHERE dialogue_id=? ORDER BY turn DESC LIMIT 4',(job['id'],))]
                    if recent[:2]==recent[2:] and all(r[0]=='agent' for r in recent):
                        pause_reason='双方重复了同一轮发言。可以补充新信息后继续。'
                if pause_reason:
                    state='awaiting_input'
                    waiting=pause_reason
                    checkpoint=dump({'v':1,'speaker_id':peer['user_id'],'sandbox_attempted':False})
            db.execute("UPDATE agent_dialogues SET agent_checkpoint=?,revision=revision+1,next_speaker=?,rounds_done=?,status=?,waiting_reason=?,confirmation_required=?,message_prepared=0,active_speaker='',error='',retry_until=0,updated_at=? WHERE id=?",(checkpoint,1-job['next_speaker'],rounds,'queued' if done else state,waiting,required,now(),job['id']))
            if required:
                db.execute('UPDATE agent_dialogues SET initiator_confirmed=CASE WHEN ?&1 THEN 0 ELSE initiator_confirmed END,target_confirmed=CASE WHEN ?&2 THEN 0 ELSE target_confirmed END WHERE id=?',(required,required,job['id']))
            if ordinary:
                notify(db,peer['user_id'],'message',job['session_id'],job['id'],'收到分身回复' if job['next_speaker'] else '收到分身消息',text,'message:'+str(message_id))
            self.apply_inputs(db,job['id'])
            if pending and not required:
                db.execute("UPDATE agent_dialogues SET status='queued',rounds_done=0 WHERE id=?",(job['id'],))
            elif done:
                reason='双方分身已完成本轮聊天' if ordinary else waiting or '分身判断当前委派任务已完成'
                self.close_record(db,job['id'],reason,'cancel' if explicit_stop else 'complete')
            db.execute('UPDATE agent_peer_sessions SET updated_at=? WHERE id=?',(now(),job['session_id']))
        return done and not pending
