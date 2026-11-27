"""Subagent 风格双分身委派：配对长期会话、独立任务、ACPS 传输及双方独立持久记忆。"""
import asyncio
from datetime import datetime, timedelta
import json
from weakref import WeakValueDictionary
from types import SimpleNamespace
from contextlib import nullcontext, ExitStack
from fastapi import HTTPException
from avatar.core.db import now, dump
from avatar.core.execution import run_blocking
from avatar.models.effort import with_chat_effort
from avatar.conversation.schemas import ChatRequest
from avatar.conversation.loop.peer import PeerTurn, pause_wire
from avatar.conversation.collaboration import creation
from avatar.conversation.collaboration.creation import PeerRequest
from avatar.conversation.collaboration.inbox import AgentInbox, notify
from avatar.conversation.collaboration.intent import DialogueIntent
from avatar.skills.policy import require_skills_enabled
from avatar.conversation.collaboration.executor import PeerTurnExecutor
from avatar.conversation.collaboration.recovery import PeerRecovery
from avatar.conversation.collaboration.sessions import PeerSessions
from avatar.files.conversation.peer import PeerResources



# 调度与生成分离；共享入口就绪走 Partner，否则使用用户授权的同平台内部传输，通道在发起前确定。
class AgentDialogues:
    def __init__(self,database,chat,transport=None):
        self.database,self.chat,self.transport=database,chat,transport
        self.request_locks = WeakValueDictionary()
        self.calls={};self.inbox=AgentInbox(database);self.intent=DialogueIntent(self)
        self.resources=PeerResources(self)
        transport = lambda: self.transport
        self.session_store = PeerSessions(database, self.resources, transport)
        self.executor = PeerTurnExecutor(
            database, chat, self.session_store, self.resources, transport,
            self.apply_inputs, self.close_record, lambda identifier: self.recovery.flush_controls(identifier),
        )
        self.recovery = PeerRecovery(self.executor, self.calls)

    def targets(self,user,db=None):
        return [{'aic':t['aic'],'name':t['name']} for t in self.chat.assistant_router.catalog(db)
                if t['row']['user_id']!=user['id'] and t['row']['identity_status']=='active']

    async def trigger(self, user, message, parent):
        return await self.intent.navigate(user, message, parent)


    def request_lock(self, user, request):
        # 单 worker 下同键仅执行一次计费意图判断；持久回执在写事务内再次核对。
        return self.request_locks.setdefault((user['id'], request.scope, request.key), asyncio.Lock())

    def replay(self, db, user, request, legacy_task=None):
        row = db.execute('SELECT task_id,fingerprint FROM agent_request_receipts WHERE user_id=? AND scope=? AND request_id=?',
                         (user['id'], request.scope, request.key)).fetchone()
        if row:
            self.session_store.owned(db, user, row['task_id'])
            if row['fingerprint'] != request.fingerprint:
                raise HTTPException(409, '同一请求标识不能更改内容')
            return row['task_id']
        if legacy_task:
            self.session_store.owned(db, user, legacy_task)
            raise HTTPException(409, '该请求已处理，无法核对原始内容，请查看既有任务')
        return None

    def create(self,user,body,source_conversation_id=None,skills='',db=None,request=None,*,prepared=False):
        return creation.create_dialogue(self,user,body,source_conversation_id,db,request,prepared=prepared)

    # 仅确认弹窗可提交此意图；幂等恢复同一个任务，不重复发送或共享额外内容。
    async def confirm_intent(self,user,identifier,body):
        request = PeerRequest.from_body('intent', identifier, body)
        async with self.request_lock(user, request):
            return await run_blocking(creation.confirm_submission, self, user, identifier, body, request)

    def retry_draft(self,user,identifier,request_id):
        return creation.retry_draft(self,user,identifier,request_id)

    def task_draft(self,user,identifier,body):
        return creation.task_draft(self,user,identifier,body)

    def accept(self,user,identifier,task_id=None,transaction=None):
        """仅目标用户可接受指定任务；普通聊天无需授权，不批量放行其他任务。"""
        with (nullcontext(transaction) if transaction is not None else self.database.connect(write=True)) as db:
            self.session_store.member(db,user,identifier,hidden=bool(task_id))
            if task_id:row=self.session_store.owned(db,user,task_id)
            else:
                found=db.execute("SELECT * FROM agent_dialogues WHERE session_id=? AND target_user_id=? AND status='awaiting_acceptance' ORDER BY created_at,id LIMIT 1",(identifier,user['id'])).fetchone()
                if not found:raise HTTPException(409,'没有需要本人同意的任务')
                row=dict(found)
            if row['session_id']!=identifier or row['target_user_id']!=user['id']:raise HTTPException(403,'只有任务接收方可以同意')
            if row['delivery_mode']=='message':raise HTTPException(409,'普通聊天无需同意')
            if row['status']=='ended':raise HTTPException(409,'任务已结束')
            if not row['accepted']:
                if row['status']!='awaiting_acceptance':
                    raise HTTPException(409,'当前任务无需同意')
                self.apply_inputs(db,row['id'])
                db.execute("UPDATE agent_dialogues SET accepted=1,status='queued',next_speaker=0,updated_at=? WHERE id=?",(now(),row['id']))
                db.execute('UPDATE agent_peer_sessions SET accepted=1,updated_at=? WHERE id=?',(now(),identifier))
                notify(db,row['user_id'],'accepted',identifier,row['id'],'对方已确认任务','双方分身将自动开始处理本任务。','accept:'+row['id'])
                self.mark_task_request_read(db,user['id'],row['id'])
        if transaction is None:
            return self.session_store.get(user,row['id'])

    @staticmethod
    def mark_task_request_read(db,user_id,identifier):
        db.execute("UPDATE agent_notifications SET read_at=? WHERE user_id=? AND task_id=? AND kind='request' AND read_at IS NULL",
                   (now(),user_id,identifier))

    def reject(self,user,identifier):
        with self.database.connect(write=True) as db:
            row=self.session_store.owned(db,user,identifier)
            if row['target_user_id']!=user['id']:
                raise HTTPException(403,'只有任务接收方可以拒绝')
            if not row['rejected']:
                if row['delivery_mode']=='message' or row['status']!='awaiting_acceptance' or row['accepted']:
                    raise HTTPException(409,'当前任务已处理或无需拒绝')
                self.close_record(db,identifier,'接收方拒绝了任务请求','cancel',remember=False,rejected=True)
                db.execute('UPDATE agent_dialogues SET revision=revision+1 WHERE id=?',(identifier,))
                db.execute('DELETE FROM agent_pending_inputs WHERE task_id=? AND applied=0',(identifier,))
                db.execute('UPDATE agent_peer_sessions SET updated_at=? WHERE id=?',(now(),row['session_id']))
                self.mark_task_request_read(db,user['id'],identifier)
        return self.session_store.get(user,identifier)

    async def approve(self,user,identifier,body=None,initial=False):
        """补充要求与文件先持久入队；仅待确认方可同意，再一起放行给分身。"""
        action = 'accept' if initial else 'confirm'
        request = PeerRequest.from_body(action+':'+identifier, body.request_id, body) if body else None
        def read_approval():
            with self.database.connect() as db:
                row = self.session_store.owned(db, user, identifier)
                previous = request and self.replay(db, user, request)
            return row, previous
        row, previous = await run_blocking(read_approval)
        if previous:
            return await run_blocking(self.session_store.get,user, identifier)
        state=await run_blocking(self.session_store.view,row,user)
        if not state['can_accept' if initial else 'needs_confirmation']:
            if initial and row['target_user_id']==user['id'] and row['accepted']:
                if request:
                    raise HTTPException(409, '任务已接受，无法核对本次请求')
                return await run_blocking(self.session_store.get,user,identifier)
            if not initial and row['status']=='ended':raise HTTPException(409,'任务已结束')
            raise HTTPException(403,'当前无需本人确认，请等待对方')
        if request:
            async with self.request_lock(user, request):
                return await self._resume(user, identifier, body, request, approval=action)
        return await run_blocking(self.accept,user,row['session_id'],identifier) if initial else await run_blocking(self.confirm,user,identifier)

    async def send_session(self,user,identifier,body):
        request = PeerRequest.from_body('session:'+identifier, body.request_id, body)
        async with self.request_lock(user, request):
            return await self._send_session(user, identifier, body, request)

    async def _send_session(self, user, identifier, body, request):
        def read_session():
            with self.database.connect() as db:
                row=self.session_store.member(db,user,identifier)
                legacy = db.execute('SELECT id FROM agent_dialogues WHERE user_id=? AND request_id=? UNION ALL '
                                    'SELECT p.task_id FROM agent_pending_inputs p JOIN agent_dialogues d ON d.id=p.task_id '
                                    'WHERE d.session_id=? AND p.user_id=? AND p.request_id=? LIMIT 1',
                                    (user['id'], body.request_id, identifier, user['id'], body.request_id)).fetchone()
                prior = self.replay(db, user, request, legacy[0] if legacy else None)
                current=db.execute("SELECT id FROM agent_dialogues WHERE session_id=? AND delivery_mode!='message' AND status NOT IN ('ended','failed') ORDER BY created_at,id LIMIT 1",(identifier,)).fetchone()
                peer=row['right_user_id'] if row['left_user_id']==user['id'] else row['left_user_id']
                target=db.execute('SELECT aic FROM avatar_records WHERE user_id=?',(peer,)).fetchone()
            return prior, current, target
        prior, current, target = await run_blocking(read_session)
        if prior:
            return await run_blocking(self.session_store.get,user,prior)
        if getattr(body,'delivery_mode','message')!='message' and not getattr(body,'new_task',False):
            if not current:
                raise HTTPException(409,'当前没有可继续的任务，请创建新任务')
            return await self._resume(user,current['id'],body,request)
        if not body.message.strip():raise HTTPException(422,'请填写新的聊天内容或任务')
        mode=getattr(body,'delivery_mode','message')
        booking=mode!='message' and await self.intent.requires_confirmation(user,body.message)
        task_id=await run_blocking(self.create,user,SimpleNamespace(target_aic=target['aic'],objective=body.message,request_id=body.request_id,delivery_mode=mode,kind='booking' if booking else 'chat',file_ids=getattr(body,'file_ids',[])),request=request)
        return await run_blocking(self.session_store.get,user,task_id)

    async def cancel(self,user,identifier):
        def pause():
            with self.database.connect(write=True) as db:
                row=self.session_store.owned(db,user,identifier)
                if row['status'] in ('ended','paused') or row['status']=='awaiting_input' and row['agent_checkpoint']:
                    return self.session_store.views(db,[row],user)[0]
                checkpoint=json.loads(row['agent_checkpoint'] or '{}')
                speaker_id=row['user_id'] if row['next_speaker']==0 else row['target_user_id']
                if checkpoint.get('speaker_id')!=speaker_id:
                    checkpoint={'v':1,'speaker_id':speaker_id,'sandbox_attempted':False}
                activity=json.loads(row['tool_activity'])
                checkpoint['activity']=[*checkpoint.get('activity', []), *(a for a in activity if a.get('user_id')==speaker_id)]
                if row['status']=='running' and row['transport_mode']=='acps':
                    pause_wire(db,row,checkpoint)
                db.execute("UPDATE agent_dialogues SET status='paused',agent_checkpoint=?,tool_activity='[]',revision=revision+1,active_speaker='',updated_at=? WHERE id=?",
                           (dump(checkpoint),now(),identifier))
        existing = await run_blocking(pause)
        if existing is not None:
            return existing
        await self.interrupt(identifier)
        return await run_blocking(self.session_store.get,user,identifier)

    async def interrupt(self,identifier):
        call=self.calls.get(identifier)
        if call:call.cancel();await asyncio.gather(call,return_exceptions=True)

    # 补充内容先入队；运行中的模型答完后再按顺序插入，不取消/重放本轮已消耗额度的回答。
    async def resume(self,user,identifier,body):
        request = PeerRequest.from_body('task:'+identifier, body.request_id, body)
        async with self.request_lock(user, request):
            return await self._resume(user, identifier, body, request)

    async def _resume(self, user, identifier, body, request, approval=None):
        def read_supplement():
            with self.database.connect() as db:
                initial=self.session_store.owned(db,user,identifier)
                legacy = db.execute('SELECT task_id FROM agent_pending_inputs WHERE task_id=? AND user_id=? AND request_id=?',
                                    (identifier,user['id'],body.request_id)).fetchone()
                previous = self.replay(db, user, request, legacy[0] if legacy else None)
            return initial, previous
        initial, previous = await run_blocking(read_supplement)
        if not previous:
            waiting=self.check_supplement(initial,user,body,body.message)
            asking=waiting and bool(json.loads(initial['agent_checkpoint']).get('user_question'))
            steering=getattr(body,'steer',False)
            message=body.message.strip() if asking or steering else body.message
            removed=getattr(body,'remove_skills',[])
            if removed:
                require_skills_enabled()
            if initial['status']=='awaiting_identity':raise HTTPException(409,'正在自动重试通信入口，超时后可重新编辑并发送')
            if initial['status']=='ended':raise HTTPException(409,'该任务已完成，请在同一分身会话创建新任务')
            booking=not (asking or approval or steering) and bool(message) and initial['delivery_mode']!='message' and await self.intent.requires_confirmation(user,message)
            previous = await run_blocking(self._queue_input, user, identifier, body, request, approval, message, booking)
        if previous != identifier and not request.scope.startswith('session:'):
            return {'id': previous, 'session_id': initial['session_id']}
        return await run_blocking(self.session_store.get,user, previous)

    @staticmethod
    def check_supplement(row,user,body,message):
        expected=getattr(body,'expected_revision',None)
        waiting=bool(row['agent_checkpoint'] and row['status']=='awaiting_input')
        if expected is not None and (expected!=row['revision'] or not (waiting or row['status']=='paused')):
            raise HTTPException(409,'任务状态已改变，请刷新后再补充')
        question=None
        if waiting:
            checkpoint=json.loads(row['agent_checkpoint'])
            if checkpoint['speaker_id']!=user['id']:
                raise HTTPException(403,'请等待当前分身的所有人补充信息')
            question=checkpoint.get('user_question')
        question_id=getattr(body,'question_id',None)
        if question_id and (not question or question_id!=question['id']):
            raise HTTPException(409,'分身提问已改变，请刷新后回答')
        if question and not message.strip():
            raise HTTPException(422,'请填写对分身提问的回答')
        return waiting

    def _queue_input(self, user, identifier, body, request, approval, message, booking):
        with self.database.connect(write=True) as db:
            row=self.session_store.owned(db,user,identifier)
            previous = self.replay(db, user, request)
            if previous:
                return previous
            if row['status']=='ended':raise HTTPException(409,'该任务已完成，请在同一分身会话创建新任务')
            steering=getattr(body,'steer',False)
            if steering:
                if approval or not row['accepted'] or row['status'] not in ('running','queued'):
                    raise HTTPException(409,'本轮已暂停或结束，请刷新后补充')
                if not message or getattr(body,'file_ids',[]) or getattr(body,'question_id',None):
                    raise HTTPException(422,'生成中补充只接收文字')
            if approval == 'accept' and (row['target_user_id'] != user['id'] or row['accepted']):
                raise HTTPException(409, '任务已处理或无需本人接受')
            if approval == 'confirm' and not row['confirmation_required'] & (1 if row['user_id'] == user['id'] else 2):
                raise HTTPException(403, '当前无需本人确认')
            if approval and row['delivery_mode'] == 'message':
                raise HTTPException(409, '普通聊天无需确认')
            waiting=self.check_supplement(row,user,body,message)
            checkpoint=json.loads(row['agent_checkpoint']) if waiting else {}
            if question:=checkpoint.get('user_question'):
                checkpoint.pop('user_question')
                checkpoint['question_answer']={**question,'answer':message}
                updated=db.execute("""UPDATE agent_dialogues SET agent_checkpoint=?,status='queued',revision=revision+1,
                    waiting_reason='',error='',rounds_done=0,updated_at=?
                    WHERE id=? AND revision=? AND status='awaiting_input'""",(dump(checkpoint),now(),identifier,row['revision']))
                if not updated.rowcount:raise HTTPException(409,'分身提问已改变，请刷新后回答')
                self.resources.share(db,user,identifier,getattr(body,'file_ids',[]))
                request.remember(db,user,identifier)
                return identifier
            if row['delivery_mode']=='message' and message and not (waiting or steering) and row['status']!='paused':
                peer=row['target_aic'] if row['user_id']==user['id'] else row['own_aic']
                task_id=self.create(user,SimpleNamespace(target_aic=peer,objective=body.message,request_id=body.request_id,delivery_mode='message',file_ids=getattr(body,'file_ids',[])),db=db,request=request)
                # 一次消息交付中的补充另发一条，不能把尚未发送的内容写成上一条的送达结果。
                return task_id
            accepted=bool(row['accepted'])
            # 等待接受也能补充草稿，保持待接受状态；不会因补充消息绕过对方授权。
            if db.execute('SELECT 1 FROM agent_pending_inputs WHERE task_id=? AND user_id=? AND request_id=?',(identifier,user['id'],body.request_id)).fetchone():
                raise HTTPException(409,'该请求已用于其他操作')
            limits={r['user_id']:r['max_rounds'] for r in db.execute('SELECT * FROM agent_dialogue_preferences WHERE user_id IN (?,?)',(row['user_id'],row['target_user_id']))}
            db.execute('UPDATE agent_dialogues SET round_limit=? WHERE id=?',(min(limits.get(row['user_id'],10),limits.get(row['target_user_id'],10)),identifier))
            db.execute('INSERT INTO agent_pending_inputs(task_id,user_id,request_id,message) VALUES(?,?,?,?)',
                       (identifier,user['id'],body.request_id,message))
            self.resources.share(db,user,identifier,getattr(body,'file_ids',[]))
            request.remember(db, user, identifier)
            if booking:
                db.execute("UPDATE agent_dialogues SET task_kind='booking',initiator_confirmed=0,target_confirmed=0 WHERE id=?",(identifier,))
            if row['status']!='running' and accepted and not (approval or steering and row['message_prepared'] or row['status']=='awaiting_input' and row['confirmation_required']):
                self.apply_inputs(db,identifier)
                db.execute("UPDATE agent_dialogues SET status=?,rounds_done=0,error='',revision=revision+1,updated_at=? WHERE id=?",('queued' if accepted or row['delivery_mode']=='message' else 'awaiting_acceptance',now(),identifier))
            if approval == 'accept':
                self.accept(user, row['session_id'], identifier, transaction=db)
            elif approval == 'confirm':
                self.confirm(user, identifier, transaction=db)
        return identifier

    def apply_inputs(self,db,identifier):
        row=db.execute('SELECT * FROM agent_dialogues WHERE id=?',(identifier,)).fetchone()
        for item in db.execute('SELECT * FROM agent_pending_inputs WHERE task_id=? AND applied=0 ORDER BY id',(identifier,)).fetchall():
            turn=db.execute('SELECT COALESCE(MAX(turn),0)+1 FROM agent_dialogue_messages WHERE dialogue_id=?',(identifier,)).fetchone()[0]
            uid=item['user_id'];aic=row['own_aic'] if uid==row['user_id'] else row['target_aic']
            # 单独移出技能属于配置操作，不代替用户发送消息或恢复暂停任务。
            if not item['message'].strip():
                db.execute('UPDATE agent_pending_inputs SET applied=1 WHERE id=?',(item['id'],))
                continue
            db.execute("INSERT INTO agent_dialogue_messages(dialogue_id,turn,speaker_aic,speaker_name,speaker_role,content,usage,created_at) VALUES(?,?,?,'用户补充要求','user',?,'null',?)",(identifier,turn,aic,item['message'],now()))
            db.execute('UPDATE agent_dialogue_messages SET speaker_user_id=? WHERE dialogue_id=? AND turn=?',(uid,identifier,turn))
            db.execute('UPDATE agent_dialogues SET next_speaker=? WHERE id=?',(int(uid!=row['user_id']),identifier))
            if item['message'].strip() and row['task_kind']=='booking':db.execute('UPDATE agent_dialogues SET initiator_confirmed=0,target_confirmed=0 WHERE id=?',(identifier,))
            db.execute('UPDATE agent_pending_inputs SET applied=1 WHERE id=?',(item['id'],))

    def confirm(self,user,identifier,transaction=None):
        """仅放行当前待确认方的继续授权，不替本人决定预约结果。"""
        with (nullcontext(transaction) if transaction is not None else self.database.connect(write=True)) as db:
            row=self.session_store.owned(db,user,identifier)
            if row['status']=='ended':raise HTTPException(409,'任务已完成，请创建新任务')
            bit=1 if row['user_id']==user['id'] else 2
            if row['delivery_mode']=='message' or not row['confirmation_required']&bit:raise HTTPException(403,'当前无需本人确认')
            self.apply_inputs(db,identifier)
            # 确认只授权继续处理，不代表接受或拒绝预约。
            db.execute('UPDATE agent_dialogues SET confirmation_required=confirmation_required&?,confirmation_acknowledged=confirmation_acknowledged|? WHERE id=?',(~bit,bit,identifier))
            ready=not db.execute('SELECT confirmation_required FROM agent_dialogues WHERE id=?',(identifier,)).fetchone()[0]
            if ready:db.execute("UPDATE agent_dialogues SET status='queued',confirmation_required=0,waiting_reason='',rounds_done=0,updated_at=? WHERE id=?",(now(),identifier))
        if transaction is None:
            return self.session_store.get(user,identifier)

    # 删除本方历史游标和记忆，不改写对方记忆、任务账单或对方保留的原文。
    def clear_session(self,user,identifier,hide=False):
        with ExitStack() as resources, self.database.connect(write=True, account_scope=None if hide else user['id']) as db:
            self.session_store.member(db,user,identifier,hidden=True)
            if hide:
                if db.execute("SELECT 1 FROM agent_dialogues WHERE session_id=? AND delivery_mode='message' AND status NOT IN ('ended','failed')",(identifier,)).fetchone():
                    raise HTTPException(409,'会话仍有消息在处理，请结束后再删除')
                row = db.execute('SELECT resource_conversation_id FROM agent_session_members WHERE session_id=? AND user_id=?',
                                 (identifier,user['id'])).fetchone()
                self.resources.release_workspaces(db,resources,[(user['id'],row[0])])
            maximum=db.execute('SELECT COALESCE(MAX(m.id),0) FROM agent_dialogue_messages m JOIN agent_dialogues d ON d.id=m.dialogue_id WHERE d.session_id=?',(identifier,)).fetchone()[0]
            db.execute("UPDATE agent_session_members SET hidden=?,cleared_through=?,summary='',summary_through=?,cleared_at=? WHERE session_id=? AND user_id=?",(int(hide),maximum,maximum,now(),identifier,user['id']))
            if hide:self.resources.release_shared(db,resources,'session',identifier)
            sources=db.execute('SELECT source_message_id FROM agent_task_memories WHERE session_id=? AND user_id=?',(identifier,user['id'])).fetchall()
            for source in sources:
                db.execute("UPDATE chat_conversations SET summary='',summary_through=0 WHERE user_id=? AND id IN (SELECT conversation_id FROM chat_messages WHERE id=? AND user_id=?)",(user['id'],source['source_message_id'],user['id']))
                db.execute('DELETE FROM chat_messages WHERE id=? AND user_id=?',(source['source_message_id'],user['id']))
            db.execute('DELETE FROM agent_task_memories WHERE session_id=? AND user_id=?',(identifier,user['id']))
            db.execute("UPDATE agent_task_members SET summary='',summary_through=? WHERE user_id=? AND task_id IN (SELECT id FROM agent_dialogues WHERE session_id=? AND delivery_mode='message')",(maximum,user['id'],identifier))
        return {'cleared':True}

    def dismiss_all_history(self,user,session=None):
        with ExitStack() as resources, self.database.connect(write=True) as db:
            if session:self.session_store.member(db,user,session)
            condition="d.status='ended'"+(' AND d.session_id=?' if session else '')
            values=(user['id'],session) if session else (user['id'],)
            tasks=db.execute("UPDATE agent_task_members SET hidden=1,summary='',summary_through=0,expired_files='[]' WHERE user_id=? AND hidden=0 AND task_id IN (SELECT d.id FROM agent_dialogues d WHERE "+condition+') RETURNING task_id,resource_conversation_id',values).fetchall()
            workspaces=[row['resource_conversation_id'] for row in tasks]
            if tasks:
                db.execute('UPDATE agent_task_members SET resource_conversation_id=NULL WHERE user_id=? AND task_id=ANY(?::text[])',
                           (user['id'],[row['task_id'] for row in tasks]))
            if not session:
                sessions=db.execute("UPDATE agent_session_members SET hidden=1,summary='',cleared_at=?,cleared_through=(SELECT coalesce(max(m.id),0) FROM agent_dialogue_messages m JOIN agent_dialogues d ON d.id=m.dialogue_id WHERE d.session_id=agent_session_members.session_id),summary_through=0 WHERE user_id=? AND hidden=0 AND NOT EXISTS(SELECT 1 FROM agent_dialogues d WHERE d.session_id=agent_session_members.session_id AND d.status!='ended') RETURNING session_id,resource_conversation_id",(now(),user['id'])).fetchall()
                workspaces.extend(row['resource_conversation_id'] for row in sessions)
            else:sessions=[]
            self.resources.release_task_workspaces(db,resources,[(user['id'],workspace) for workspace in workspaces])
            for row in tasks:
                self.resources.release_shared(db,resources,'task',row['task_id'])
            for row in sessions:
                self.resources.release_shared(db,resources,'session',row['session_id'])
        return {'deleted':len(tasks),'sessions':len(sessions)}

    async def delete(self,user,identifier):
        """删除本方记录；未完成任务先终止且不回传记忆，完成总结不随记录删除。"""
        def hide():
            with ExitStack() as resources, self.database.connect(write=True) as db:
                row=self.session_store.owned(db,user,identifier)
                if row['status']!='ended':
                    self.close_record(db,identifier,'任务已提前删除','cancel',remember=False)
                    db.execute('UPDATE agent_dialogues SET revision=revision+1 WHERE id=?',(identifier,))
                    db.execute('DELETE FROM agent_pending_inputs WHERE task_id=? AND applied=0',(identifier,))
                member=db.execute("UPDATE agent_task_members SET hidden=1,summary='',summary_through=0,expired_files='[]' WHERE task_id=? AND user_id=? RETURNING resource_conversation_id",
                                  (identifier,user['id'])).fetchone()
                if member and row['delivery_mode']!='message':
                    db.execute('UPDATE agent_task_members SET resource_conversation_id=NULL WHERE task_id=? AND user_id=?',
                               (identifier,user['id']))
                    self.resources.release_task_workspaces(db,resources,[(user['id'],member[0])])
                    self.resources.release_shared(db,resources,'task',identifier)
                db.execute('UPDATE agent_notifications SET deleted_at=? WHERE task_id=? AND user_id=?',(now(),identifier,user['id']))
            return row
        row = await run_blocking(hide)
        await self.interrupt(identifier)
        await self.recovery.flush_controls(identifier)
        return {'deleted':True,'memory_retained':row['status']=='ended'}

    def close_record(self,db,identifier,reason,action='complete',remember=True,*,rejected=False):
        stamp=now()
        deadline=(datetime.fromisoformat(stamp)+timedelta(days=7)).isoformat(timespec='microseconds').replace('+00:00','Z')
        task=db.execute("""UPDATE agent_dialogues SET status='ended',agent_checkpoint=NULL,ended_at=?,end_reason=?,
            expires_at=CASE WHEN delivery_mode='message' THEN NULL ELSE ? END,active_speaker='',error='',
            waiting_reason='',confirmation_required=0,rejected=?,updated_at=? WHERE id=? AND status!='ended' RETURNING *""",
            (stamp,reason,deadline,rejected,stamp,identifier)).fetchone()
        if not task:return
        for owner in (task['user_id'],task['target_user_id']):
            PeerTurn.clear(db, identifier, owner)
        db.execute('UPDATE agent_dialogues SET pending_file_delivery=NULL WHERE id=?', (identifier,))
        result_title=('消息已送达' if action=='complete' else '消息已取消') if task['delivery_mode']=='message' else ('任务已完成' if action=='complete' else '任务已终止')
        if rejected:
            result_title='任务已拒绝'
        db.execute('INSERT OR IGNORE INTO agent_protocol_controls(task_id,action) VALUES(?,?)',(identifier,action))
        if task['delivery_mode']=='message':return
        self.resources.sync_task_expiry(db,identifier)
        for owner in (task['user_id'],task['target_user_id']):
            cards=self.resources.expired_cards(db,identifier,owner)
            if cards:
                db.execute('UPDATE agent_task_members SET expired_files=? WHERE task_id=? AND user_id=?',
                           (dump(cards),identifier,owner))
        for member in db.execute('SELECT * FROM agent_session_members WHERE session_id=?',(task['session_id'],)).fetchall():
            notify(db,member['user_id'],'result',task['session_id'],identifier,result_title,
                   '任务文件将在结束七天后清理，需要保留请转存到资料库。\n'+reason,'result:'+identifier)
            if not remember:continue
            own_aic=task['own_aic'] if member['user_id']==task['user_id'] else task['target_aic']
            messages=db.execute("SELECT coalesce(m.legacy_display_content,m.content) AS content,m.task_action FROM agent_dialogue_messages m WHERE dialogue_id=? AND m.id>? AND speaker_aic=? AND speaker_role='agent' ORDER BY id DESC LIMIT 1",(identifier,member['cleared_through'],own_aic)).fetchall()
            own_conclusion=bool(messages)
            if not messages:messages=db.execute('SELECT coalesce(legacy_display_content,content) AS content,task_action FROM agent_dialogue_messages WHERE dialogue_id=? AND id>? ORDER BY id DESC LIMIT 1',(identifier,member['cleared_through'])).fetchall()
            if not messages:continue
            content='任务总结（仅作历史数据）：'+task['objective']+'\n结束原因：'+reason+'\n'+('本人分身的结论：' if own_conclusion else '已接收的任务结果：')+'\n'+messages[0]['content']
            destination=db.execute("SELECT id FROM chat_conversations WHERE user_id=? AND platform='web' AND parent_id IS NULL AND channel!='peer' AND (id=? OR channel='assistant') ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,updated_at DESC LIMIT 1",(member['user_id'],member['source_conversation_id'],member['source_conversation_id'])).fetchone()
            source_message_id=None
            avatar=db.execute('SELECT aic,profile,last_modified_at FROM avatar_records WHERE user_id=?',(member['user_id'],)).fetchone()
            if destination and avatar:
                source=db.execute("INSERT INTO chat_messages(user_id,channel,role,content,created_at,profile_modified_at,conversation_id,responder_aic,responder_name) VALUES(?,'assistant','assistant',?,?,?,?,?,?)",(member['user_id'],result_title+'：'+task['objective']+'\n\n'+content[:6000],now(),avatar['last_modified_at'],destination['id'],avatar['aic'],json.loads(avatar['profile']).get('avatar_name','我的分身')))
                source_message_id=source.lastrowid
                conclusion=messages[0]['content']
                db.execute('UPDATE chat_messages SET task_event=? WHERE id=?',(dump({'kind':'end','task_id':identifier,'title':task['objective'],'mode':task['delivery_mode'],'state':'ended','reason':reason,'summary':conclusion}),source_message_id))
                db.execute('UPDATE chat_conversations SET updated_at=? WHERE id=?',(now(),destination['id']))
            db.execute('INSERT OR REPLACE INTO agent_task_memories(user_id,session_id,task_id,content,created_at,source_message_id) VALUES(?,?,?,?,?,?)',(member['user_id'],task['session_id'],identifier,content[:6000],now(),source_message_id))

    async def end(self,user,identifier,expected_revision=None):
        def close():
            with self.database.connect(write=True) as db:
                row=self.session_store.owned(db,user,identifier)
                if expected_revision is not None and (row['revision']!=expected_revision or row['status']!='awaiting_input'):
                    raise HTTPException(409,'任务状态已改变，请刷新后再结束')
                if row['status']!='ended':
                    self.close_record(db,identifier,'用户结束当前任务','cancel')
                    db.execute('UPDATE agent_dialogues SET revision=revision+1 WHERE id=?',(identifier,))
        await run_blocking(close)
        if call:=self.calls.get(identifier):
            call.cancel()
        return await run_blocking(self.session_store.get,user,identifier)


    def compact_scope(self,db,user,session_id,task_id):
        uid=user['id']
        job=None
        if task_id:
            job=self.session_store.owned(db,user,task_id)
            if job['session_id']!=session_id:raise HTTPException(404,'任务不存在')
            if job['delivery_mode']=='message':task_id,job=None,None
        member=self.session_store.member(db,user,session_id,hidden=bool(task_id))
        memory=db.execute('SELECT * FROM agent_task_members WHERE task_id=? AND user_id=? AND hidden=0',(task_id,uid)).fetchone() if task_id else member
        if not memory:raise HTTPException(404,'会话不存在')
        return member,memory,task_id,job

    def compact_usage(self,user,session_id,task_id):
        with self.database.connect() as db:
            member,memory,task_id,job=self.compact_scope(db,user,session_id,task_id)
            through=memory['summary_through'] if task_id else max(member['cleared_through'],memory['summary_through'])
            query,params=self.executor.memory_query(session_id,task_id,through,user['id'])
            size=db.execute("SELECT COALESCE(SUM((octet_length(m.speaker_name || '：' || m.content)"
                            " + CASE WHEN m.speaker_user_id=? THEN octet_length(m.tool_activity) ELSE 0 END + 2)/3 + 16),0)"+query,(user['id'],*params)).fetchone()[0]
        _,context=self.chat.context(user,ChatRequest(channel='avatar',message='/compact'))
        task_context=(job['objective']+'\n'+(job['shared_content'] or '')) if job else ''
        return self.chat.memory.usage(context[0],context[2],memory['summary'],int(size),task_context)

    async def compact(self,user,session_id,task_id,focus=''):
        uid=user['id']
        def prepare(task_id):
            with self.database.connect() as db:
                member,memory,task_id,job=self.compact_scope(db,user,session_id,task_id)
                if job and job['status']=='ended':
                    raise HTTPException(409,'任务已完成，仅可查看记录')
                through=memory['summary_through'] if task_id else max(member['cleared_through'],memory['summary_through'])
                rows=self.executor.memory_rows(db,session_id,task_id,through,uid)
                if not rows:return {'message':'没有新的消息需要压缩。'}, None
            overview,context=self.chat.context(user,ChatRequest(channel='avatar',message='/compact'))
            if context[0].get('_direct_chat'):
                return {'message':'普通聊天使用近期消息，无需调用模型压缩。','compressed':False}, None
            speaker=overview['agent']
            workspace = memory['resource_conversation_id']
            if workspace:
                override = self.chat.tools.get(uid, workspace).get('reasoning_effort')
                context = (with_chat_effort(context[0], override), *context[1:])
            history=[{'role':'assistant' if r['speaker_role']=='agent' and r['speaker_aic']==speaker['aic'] else 'user','content':r['speaker_name']+'：'+r['content']} for r in rows]
            return None, (task_id, member, memory, rows, context, speaker, history)
        reply, prepared = await run_blocking(prepare, task_id)
        if reply:
            return reply
        task_id, member, memory, rows, context, speaker, history = prepared
        result=await self.executor.billed({'user_id':uid},speaker,context[0],self.chat.memory.summarize(context,memory['summary'],history,self.chat.model_client,focus=focus),'agent_dialogue_memory')
        def save():
            with self.database.connect(write=True, account_scope=uid) as db:
                current=self.session_store.member(db,user,session_id,hidden=bool(task_id))
                if not task_id and current['cleared_through']!=member['cleared_through']:raise HTTPException(409,'会话记录已变化，请重试')
                if task_id:
                    self.session_store.owned(db,user,task_id)
                    updated=db.execute('UPDATE agent_task_members SET summary=?,summary_through=? WHERE task_id=? AND user_id=? AND hidden=0 AND summary_through=?',(result.text,rows[-1]['id'],task_id,uid,memory['summary_through']))
                else:
                    updated=db.execute('UPDATE agent_session_members SET summary=?,summary_through=? WHERE session_id=? AND user_id=? AND hidden=0 AND cleared_through=? AND summary_through=?',(result.text,rows[-1]['id'],session_id,uid,member['cleared_through'],memory['summary_through']))
                if not updated.rowcount:raise HTTPException(409,'会话记忆已变化，请刷新')
        await run_blocking(save)
        return {'message':'本方会话记忆已压缩，原始消息已保留。','compressed':True}
