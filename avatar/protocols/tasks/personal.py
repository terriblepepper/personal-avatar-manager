# 模块说明：个人分身 Direct 任务、Keycloak 用户鉴权、会话隔离和真实模型问答。
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from fastapi import HTTPException
from avatar.core.text_budget import fits_text_budget
from avatar.core.db import dump, now
from avatar.core.schemas import ChatRequest
from avatar.core.execution import AgentNeedsInput, execution_guard
from avatar.conversation.loop.runtime import resume_safe
from avatar.conversation.loop.store import RunConflict, RunStore
from avatar.protocols.identity.ontology import AVATAR_SKILL_IDS
from avatar.protocols.tasks import personal_turns as turns
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.protocols.tasks.town_auth import TownUserAuthorization
from avatar.protocols.tasks.town_public import TownPublicAuthorization


WORKING_TIMEOUT = 480
PROGRESS_INTERVAL = 8
log = logging.getLogger('uvicorn.error.' + __name__)
PUBLIC_STAGES = {
    'memory': '读取会话上下文', 'compressing': '整理会话上下文',
    'intent_analysis': '判断任务目标', 'thinking': '分析任务和已有信息',
    'tool_selection': '判断所需工具', 'rag_retrieving': '检索相关资料',
    'material_reading': '读取相关资料', 'tool_catalog': '查询候选资料',
    'tool_action': '执行已选择的操作', 'skill_execution': '运行已选技能',
    'result_writing': '整理最终答复', 'model_output': '正在接收模型输出',
}


class WorkingProgress:
    def __init__(self, result, run):
        self.result_id = result['id']
        self.ids = result['taskId'], result['sessionId'], run['run_id']
        self.run = run
        self.started = time.monotonic()
        self.message = '任务已接收，等待可用执行资源'
        self.revision = 0
        self.snapshot_key = None
        self.envelope = {}

    def update(self, event):
        if event.get('group') == 'model_io':
            status = event['status']
            if status not in ('completed','error','interrupted'):
                return
            attempt = event['input']['attempt']
            output = event['output']
            log.info('Personal AIP model taskId=%r sessionId=%r run_id=%s purpose=%s attempt=%d status=%s interrupted=%s elapsed_ms=%d',
                     *self.ids, event['purpose'], attempt, status, output.get('stream_interrupted', False),
                     round(1000*(time.monotonic()-self.started)))
            stage = (f"模型响应流中断，正在进行第 {output['retry']}/{output['retry_limit']} 次重试"
                     if output.get('retry') else None)
        elif event.get('type') == 'status':
            stage = PUBLIC_STAGES.get(event.get('stage'))
        else:
            return
        if stage and stage != self.message:
            self.message = stage
            self.revision += 1

    def model_output(self, idle_limit):
        now_at = time.time()
        until = self.run['work_until']
        if now_at < until:
            self.run['work_until'] = max(until, now_at + idle_limit)
            if self.message != PUBLIC_STAGES['model_output']:
                self.update({'type':'status','stage':'model_output'})

    def view(self, result):
        # Progress is a new message, while the ledger retains the working ID used by finish().
        elapsed = max(0, int(time.monotonic()-self.started))
        key = (self.revision, elapsed//PROGRESS_INTERVAL)
        if self.snapshot_key != key:
            self.snapshot_key = key
            self.envelope = {'id':uuid.uuid4().hex, 'sentAt':now(),
                             'status':{**result['status'], 'dataItems':[
                                 {'type':'text','text':f'{self.message}（已等待 {elapsed} 秒）'}]}}
        return {**result, **self.envelope}


# 职责：在固定个人 AIC 作用域处理 ACPs 完整问答和任务确认。
# 输入：共享 AvatarChat、可信传输上下文和监听器固定的 user_id/AIC。
# 结果与约束：所有知识和 Key 只由固定所有者读取；会话按 peer/group/session/目标隔离，重复任务不重复调用模型。
class PersonalTaskService:
    # 职责：保存领域服务与跨分身模型并发上限。
    # 输入：chat 为现有问答服务，settings 提供固定 OIDC 配置。
    # 结果与约束：复用原记忆压缩及 BYOK，最多同时生成 8 条协议回答；用户必须通过独立 OIDC 鉴权。
    def __init__(self, chat, settings):
        self.chat, self.database = chat, chat.service.database
        self.limit = asyncio.Semaphore(8)
        self.pending = {}
        self.progress = {}
        self.authorization = TownUserAuthorization(self.database, settings)
        self.public_authorization = TownPublicAuthorization(self.database, settings)
        self.settings = settings

    def expire_guests(self):
        from datetime import datetime,timedelta,timezone
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        expired = "instr(peer_aic,'#guest:')>0 AND json_extract(document,'$.sentAt')<? AND (deadline IS NULL OR deadline<?)"
        limits = (cutoff, time.time())
        with self.database.connect() as db:
            has_expired = db.execute("SELECT 1 FROM avatar_aip_tasks WHERE " + expired + " LIMIT 1", limits).fetchone()
            stale=db.execute("SELECT id FROM chat_conversations WHERE platform='town-guest' AND updated_at<? AND lease_until<? LIMIT 100",(cutoff,time.time())).fetchall()
        for row in stale:
            try:self.chat.memory.remove(row['id'])
            except HTTPException as exc:
                if exc.status_code!=409:raise
        if has_expired:
            with self.database.connect(write=True) as db:
                db.execute("DELETE FROM avatar_aip_tasks WHERE " + expired, limits)

    # 职责：恢复上次进程未完成的模型任务。
    # 输入：应用启动时调用一次。
    # 结果与约束：只回收过期执行权，等原调用者重新鉴权后续跑。
    def recover(self):
        self.expire_guests()
        if getattr(self,'peer_dialogues',None):self.peer_dialogues.recover()
        with self.database.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS protocol_message_versions(kind TEXT PRIMARY KEY,version INTEGER NOT NULL)')
            upgrade=not db.execute("SELECT 1 FROM protocol_message_versions WHERE kind='personal-task' AND version>=1").fetchone()
            if upgrade:
                for old in db.execute('SELECT rowid,document FROM avatar_aip_tasks').fetchall():
                    document=json.loads(old['document']);document['id']=uuid.uuid4().hex;document['sentAt']=now()
                    db.execute('UPDATE avatar_aip_tasks SET document=? WHERE rowid=?',(dump(document),old['rowid']))
                db.execute("INSERT OR REPLACE INTO protocol_message_versions VALUES('personal-task',1)")
            for row in db.execute("""SELECT rowid,document FROM avatar_aip_tasks
                WHERE json_extract(document,'$.status.state')='working' AND (deadline IS NULL OR deadline<=?)""",
                (time.time(),)).fetchall():
                turns.interrupt_in(db, row, json.loads(row['document']))

    # 职责：校验固定端点仍对应已激活的正式个人分身。
    # 输入：user_id/AIC 由已接纳监听器固定，不能从提问或 metadata 覆盖。
    # 结果与约束：同时核对数据库所有者、AIC、本地身份状态及账户状态；注销排队后立即拒绝新请求。
    def target(self, user_id, aic):
        with self.database.connect() as db:
            row = db.execute("SELECT a.*,u.username,u.account_status FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.user_id=? AND a.aic=? AND a.identity_status='active' AND u.account_status='active'", (user_id, aic)).fetchone()
            if not row or aic.startswith('DEMO-'):
                raise ManagementTaskError(-32003, '个人分身身份当前不可用')
            return dict(row)

    # 职责：提取协议中的纯文本问题或固定个人技能调用。
    # 输入：dataItems 为框架校验后的列表。
    # 结果与约束：不接受自定义工具、目标身份、文件路径或用户 ID；技能只允许继承的两项。
    def question(self, items, skills=None, *, selected=None):
        allowed=AVATAR_SKILL_IDS if skills is None else {item["id"] for item in skills}
        parts = []
        for item in items or []:
            if item.get('type') == 'text' and isinstance(item.get('text'), str):
                parts.append(item['text'])
            elif item.get('type') == 'data':
                data = item.get('data', {})
                if isinstance(data, dict) and set(data) == {'skillId', 'query'}:
                    data = {'skill': data['skillId'], 'arguments': {'question': data['query']}}
                if not isinstance(data, dict) or set(data) != {'skill', 'arguments'} or data.get('skill') not in allowed:
                    raise ManagementTaskError(-32602, '所选技能不在该分身已审核的 ACS 中')
                arguments = data.get('arguments', {})
                if not isinstance(arguments, dict) or set(arguments) != {'question'} or not isinstance(arguments['question'], str):
                    raise ManagementTaskError(-32602, '技能参数只能包含 question 文本')
                skill=next((item for item in (skills or []) if item['id']==data['skill']),None)
                if skill:
                    if selected is not None:
                        selected[skill['id']] = [skill['name'], skill['description']]
                    parts.append('本轮使用已审核能力：'+skill['name']+'。能力说明：'+skill['description'])
                parts.append(arguments['question'])
            else:
                raise ManagementTaskError(-32602, '只接受文本提问或固定技能参数')
        question = '\n'.join(parts).strip()
        if not question or not fits_text_budget(question):
            raise ManagementTaskError(-32602, '请填写问题，最多 64 KiB（UTF-8）')
        return question

    @staticmethod
    def approved_skills(target, selected):
        skills = json.loads(target['acs_document']).get('skills', [])
        current = {item['id']: [item['name'], item['description']] for item in skills if item['id'] in selected}
        if current != selected:
            raise ManagementTaskError(-32009, '原任务选用的分身技能已变更，请结束任务后重新选择')
        return skills

    @staticmethod
    def task_row(db, scope):
        aic, peer, group, session, task = scope
        rows = db.execute("""SELECT rowid,* FROM avatar_aip_tasks WHERE local_aic=? AND peer_aic=? AND group_id=? AND task_id=?
                             AND (session_id=? OR (session_id='' AND json_extract(document,'$.sessionId')=?))""",
                          (aic, peer, group, task, session, session)).fetchall()
        if len(rows) > 1:
            raise ManagementTaskError(-32602, '请提供原任务会话标识')
        return rows[0] if rows else None

    def working_view(self, scope, result):
        if result['status']['state']!='working':
            return result
        progress = self.progress.get(scope)
        if progress and progress.result_id==result['id']:
            return progress.view(result)
        # Another worker can own the lease. Keep the leader connected while its status is checked.
        tick = int(time.time()) // PROGRESS_INTERVAL * PROGRESS_INTERVAL
        stamp = datetime.fromtimestamp(tick,timezone.utc).isoformat()
        return {**result, 'id':uuid.uuid5(uuid.NAMESPACE_OID,f"{result['id']}:{tick}").hex,
                'sentAt':max(result['sentAt'],stamp),'status':{**result['status'],'dataItems':[
                    {'type':'text','text':'任务已受理，正在等待执行进度更新'}]}}

    # 职责：原子认领任务并处理幂等查询、完成及取消。
    # 输入：command、context 已由真实传输认证，aic/user_id 为固定端点身份。
    # 结果与约束：长模型调用在事务外；重复命令不重复计费；continue 在原 taskId/session 下生成下一轮，不暴露其他 peer 或 session 的任务。
    async def handle(self, command, context, user_id, aic):
        target = self.target(user_id, aic)
        headers={k.lower():v for k,v in getattr(context,'headers',{}).items()}
        public = headers.get('x-avatar-access')=='public' or not headers.get('authorization')
        if headers.get('x-avatar-access')!='public' and (command.get('dataItems') == [{'type':'data','data':{'peerDialogueTask':command.get('taskId')}}] or command.get('command') in ('get','complete','cancel') and self.is_peer_task(command)):
            handler=getattr(self,'peer_dialogues',None)
            if handler is None:raise ManagementTaskError(-32009,'分身委派入口未启用')
            return await handler.handle(command,context,user_id)
        principal = await self.public_authorization.authorize(context) if public else None
        if not public:
            await self.authorization.authorize(context, user_id)
        if principal and principal.get('anonymous'):self.expire_guests()
        scope_peer = context.peer_aic + ('#guest:' if principal.get('anonymous') else '#public:') + principal['visitor_id'] if public else context.peer_aic
        operation = command.get('command')
        session = command.get('sessionId') or ''
        scope = (aic, scope_peer, context.group_id or '', session, command.get('taskId'))
        if operation not in ('start', 'continue', 'get', 'complete', 'cancel') or not scope[-1]:
            raise ManagementTaskError(-32602, '需要带 taskId 的 start/continue/get/complete/cancel')
        if operation in ('get','cancel'):
            audit = log.info if operation == 'cancel' else log.debug
            audit('Personal AIP control operation=%s taskId=%r sessionId=%r commandId=%r entry=%s',
                  operation, scope[-1], session, command['id'], 'town-http' if context.peer_aic == 'town-http' else 'direct')
        if operation == 'get':
            with self.database.connect() as db:
                row = self.task_row(db, scope)
                if not row:
                    raise ManagementTaskError(-32001, '当前调用者和会话中没有该任务')
                document = json.loads(row['document'])
                saved = document.get('_agent_resume', {})
                working = document['status']['state']=='working'
                deadline = row['deadline']
                lease_valid = deadline > time.time() if deadline is not None else not working
                if lease_valid and not (saved.get('restart') or saved.get('user_question')):
                    return self.working_view((*scope[:3],row['session_id'],scope[4]),turns.public_result(document))
        with self.database.connect(write=True) as db:
            row = self.task_row(db, scope)
            run = {}
            restarting = False
            if row:
                scope = (*scope[:3], row['session_id'], scope[4])
                document = json.loads(row['document'])
                expired = row['deadline'] is not None and row['deadline'] <= time.time()
                state = document['status']['state']
                # The lease holder can have newer model progress than the last persisted checkpoint.
                if state == 'working' and (expired or row['deadline'] is None) and operation != 'cancel':
                    turns.interrupt_in(db, row, document)
                    state = document['status']['state']
                run = document.get('_agent_resume', {})
                if (operation == 'cancel' and state in ('working','awaiting-completion','awaiting-input')
                        or operation == 'complete' and state == 'awaiting-completion'
                        or expired and state == 'awaiting-completion'):
                    final = 'canceled' if operation == 'cancel' else 'failed' if expired else 'completed'
                    result = turns.close_in(db,row,document,final,reason='confirmation_timeout' if final=='failed' else None)
                    job = self.pending.get(scope)
                    if job: job.cancel()
                    return result
                if state == 'awaiting-input' and run.get('user_question'):
                    return turns.close_in(db,row,document,'failed',turns.QUESTION_UNAVAILABLE,reason='interactive_question_disabled')
                restarting = operation == 'get' and state == 'awaiting-input' and run.get('restart')
                if restarting:
                    if turns.work_expired(document):
                        return turns.expire_in(db,row,document)
                    if public and self.visitor_busy(db,aic,scope_peer):
                        return turns.public_result(document)
                if not restarting:
                    if operation != 'continue':
                        return self.working_view(scope,turns.public_result(document))
                    seen=db.execute('SELECT task_id FROM avatar_aip_task_commands WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND command_id=?',(*scope[:4],command['id'])).fetchone()
                    if seen:
                        if seen['task_id']!=scope[-1]:raise ManagementTaskError(-32602,'命令 id 已用于其他任务')
                        return self.working_view(scope,turns.public_result(document))
                    if state not in ('awaiting-input','awaiting-completion'):
                        raise ManagementTaskError(-32009,'任务正在处理，请先查询状态' if state=='working' else '任务已结束，请使用新 taskId 并沿用 sessionId 继续对话')
                session=document['sessionId']
            elif operation != 'start':
                raise ManagementTaskError(-32001, '当前调用者和会话中没有该任务')
            revision = turns.source_revision(db, user_id, aic)
            if run.get('sources') and (run['sources'] != revision or run['owner'] != user_id):
                return turns.close_in(db,row,document,'failed','原任务的资料或授权已改变，请重新发起任务')
            if not restarting:
                if db.execute('SELECT 1 FROM avatar_aip_task_commands WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND command_id=?',(*scope[:4],command['id'])).fetchone():
                    raise ManagementTaskError(-32602,'命令 id 已用于其他任务')
                selected = dict(run.get('skills', {}))
                items=command.get('dataItems') or []
                answers=[item for item in items if item.get('type')=='data' and isinstance(item.get('data'),dict)
                         and set(item['data'])=={'question_id'}]
                if answers:
                    raise ManagementTaskError(-32009,'小镇会话不支持中途提问，请补充信息并重新发起任务')
                question = self.question(items, self.approved_skills(target, selected), selected=selected)
                inputs = [*run.get('inputs', ()), question]
                if sum(len(text) + 8 for text in inputs) > 16000:
                    raise ManagementTaskError(-32602,'本任务补充内容已达上限，请结束后使用新任务整理要求')
                if public:
                    self.charge_visitor(db, user_id, aic, scope_peer, principal['visitor_id'])
                run.update(run_id=run.get('run_id') or uuid.uuid4().hex, owner=user_id, sources=revision, skills=selected,
                           inputs=inputs, input_id=command['id'], supplement=question if len(inputs)>1 else '')
            accepted_at = time.time()
            if not restarting or 'work_until' not in run:
                run['work_until'] = accepted_at + WORKING_TIMEOUT
            run['automatic'] = bool(restarting)
            run.pop('restart', None)
            result = {'type': 'task-result', 'id': uuid.uuid4().hex, 'sentAt': now(), 'senderRole': 'partner',
                      'senderId': aic, 'mentions': [context.peer_aic], 'taskId': scope[-1],
                      'sessionId': session or uuid.uuid4().hex, 'status': {'state': 'working', 'stateChangedAt': now()}, 'products': []}
            if context.group_id:
                result['groupId'] = context.group_id
            stored = dump({**result, '_agent_resume':run})
            deadline = accepted_at+turns.LEASE_SECONDS
            if row:
                rowid = row['rowid']
                db.execute('UPDATE avatar_aip_tasks SET command_id=?,document=?,deadline=? WHERE rowid=?',
                           (row['command_id'] if restarting else command['id'],stored,deadline,rowid))
            else:
                db.execute('INSERT INTO avatar_aip_tasks VALUES(?,?,?,?,?,?,?,?)', (*scope, command['id'], stored, deadline))
                rowid = self.task_row(db, scope)['rowid']
            if not restarting:
                db.execute('INSERT INTO avatar_aip_task_commands VALUES(?,?,?,?,?,?)',(*scope,command['id']))
        # Return a status before a slow model outlives the caller's HTTP timeout.
        if restarting and scope in self.pending:
            self.pending[scope].cancel()
        progress = WorkingProgress(result,run)
        self.progress[scope] = progress
        task=asyncio.create_task(self.generate(scope,rowid,result,context,principal,run,progress))
        self.pending[scope]=task
        def done(job):
            if self.pending.get(scope) is job:
                self.pending.pop(scope,None)
                self.progress.pop(scope,None)
            if not job.cancelled() and job.exception():
                log.error('Personal AIP worker taskId=%r sessionId=%r run_id=%s error=%s',
                          *progress.ids, type(job.exception()).__name__)
        task.add_done_callback(done)
        try:return await asyncio.wait_for(asyncio.shield(task),timeout=1)
        except TimeoutError:return progress.view(result)
        except asyncio.CancelledError:
            if not task.cancelled() or asyncio.current_task().cancelling():
                raise
            with self.database.connect() as db:
                row = self.task_row(db,scope)
                if not row:
                    raise ManagementTaskError(-32001,'原任务已删除')
                return turns.public_result(json.loads(row['document']))

    def charge_visitor(self, db, owner, aic, scope_peer, visitor):
        from datetime import datetime, timedelta, timezone
        day = now()[:10]
        cutoff = (datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        db.execute('DELETE FROM town_visitor_requests WHERE created_at<?', (cutoff,))
        if (db.execute('SELECT COUNT(*) FROM town_visitor_requests WHERE owner_id=? AND visitor_id=? AND created_at>=?',
                       (owner, visitor, day)).fetchone()[0] >= self.settings.town_visitor_daily_limit
                or db.execute('SELECT COUNT(*) FROM town_visitor_requests WHERE owner_id=? AND created_at>=?',
                              (owner, day)).fetchone()[0] >= self.settings.town_owner_daily_limit):
            raise ManagementTaskError(-32010, '今日公开对话额度已用完')
        if self.visitor_busy(db, aic, scope_peer):
            raise ManagementTaskError(-32010, '该访客已有请求正在处理')
        db.execute('INSERT INTO town_visitor_requests VALUES(?,?,?)', (owner, visitor, now()))

    @staticmethod
    def visitor_busy(db, aic, peer):
        return db.execute("""SELECT 1 FROM avatar_aip_tasks WHERE local_aic=? AND peer_aic=?
            AND json_extract(document,'$.status.state')='working' AND deadline>? LIMIT 1""",
            (aic,peer,time.time())).fetchone() is not None

    async def stop(self):
        if getattr(self,'peer_dialogues',None):await self.peer_dialogues.stop()
        tasks=list(self.pending.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def generate(self,scope,rowid,result,context,principal,run,progress):
        writer, owner = result['id'], run['owner']
        def validate(db):
            document = turns.current_in(db,rowid,writer,run)
            lease = document['_agent_resume'].get('lease')
            if lease and not db.execute('SELECT 1 FROM chat_conversations WHERE id=? AND lease_until=?',lease).fetchone():
                raise RunConflict('原任务的会话执行权已变更')
            if turns.source_revision(db,owner,scope[0])!=run['sources']:
                raise HTTPException(409,'原任务资料或授权已变更，请重新发起任务')
        def guard():
            with self.database.connect() as db:
                validate(db)
        store = RunStore(self.database, owner, 'protocol', str(rowid), run['run_id'], writer, validate)
        committed = None
        def pause_result(pause):
            if pause.user_question:
                return 'failed', turns.QUESTION_UNAVAILABLE
            run['checkpoint'] = pause.checkpoint
            return 'awaiting-input', str(pause)

        def commit(db, reply, pause=None):
            nonlocal committed
            store.check_in(db)
            state = 'awaiting-completion'
            if pause:
                state, reply = pause_result(pause)
                if pause.usage:
                    store.mark_usage_recorded_in(db)
            committed = turns.finish_in(db,rowid,result,run,state,reply)
        async def work():
            await self.limit.acquire()
            try:
                state = store.acquire()
                if not resume_safe(state, initial=not run.get('claimed'), automatic=run['automatic']):
                    raise HTTPException(409,'上次操作尚无确定回执，请核对已有结果后重新发起任务')
                if not state.get('phase'):
                    store.merge({'phase':'planning'})
                active = self.target(owner, scope[0])
                self.approved_skills(active,run['skills'])
                with execution_guard(guard):
                    return await self.answer(active,context,result['sessionId'],principal=principal,
                                             resume=run,store=store,progress=progress.update,commit=commit,
                                             model_output=progress.model_output,claim=lambda cid:turns.renew(self.database,rowid,writer,run,cid))
            finally:
                self.limit.release()
        task = asyncio.create_task(work())
        try:
            while not task.done():
                remaining = run['work_until']-time.time()
                if remaining <= 0:
                    raise turns.WorkDeadlineExceeded
                completed, _ = await asyncio.wait((task,),timeout=min(remaining,turns.RENEW_INTERVAL))
                if not completed:
                    turns.renew(self.database,rowid,writer,run)
            task.result()
            if committed is None:
                raise RunConflict('缺少本次回答的提交回执')
            return committed
        except AgentNeedsInput as exc:
            state, message = pause_result(exc)
            return turns.finish(self.database,rowid,result,run,state,message)
        except asyncio.CancelledError:
            # Fence the writer before draining an interrupted provider or a blocking tool.
            with self.database.connect(write=True) as db:
                row = self.task_row(db,scope)
                if row:
                    doc = json.loads(row['document'])
                    if doc['id']==writer and doc['status']['state']=='working':
                        turns.interrupt_in(db,row,doc)
            raise
        except turns.WorkDeadlineExceeded:
            return turns.finish(self.database,rowid,result,run,'failed',turns.TIMEOUT_MESSAGE,reason='working_timeout')
        except (RunConflict, TimeoutError, HTTPException, ManagementTaskError) as exc:
            message = (str(exc.detail) if isinstance(exc,HTTPException) else
                       '此次模型或工具调用超时，请重新发起任务' if isinstance(exc,TimeoutError) else str(exc))
            return turns.finish(self.database,rowid,result,run,'failed',message,reason=type(exc).__name__)
        except Exception as exc:
            log.error('Personal AIP generation taskId=%r sessionId=%r run_id=%s error=%s',
                      *progress.ids, type(exc).__name__)
            return turns.finish(self.database,rowid,result,run,'failed','模型问答失败，请检查目标分身的模型配置或稍后再试')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    # 控制命令没有 dataItems；只在已存在的配对任务中选择分身鉴权路径，随后仍核对 TLS peer。
    def is_peer_task(self,command):
        with self.database.connect() as db:
            return bool(db.execute('SELECT 1 FROM agent_dialogues WHERE id=? AND session_id=?',(command.get('taskId'),command.get('sessionId'))).fetchone())

    # 职责：使用已通过本人鉴权的所有者资料和模型配置生成个人回答。
    # 输入：target 为服务端固定目标，context 是认证 peer/group，session 来自协议会话。
    # 结果与约束：不创建或绑定小镇用户账号；协议会话不混入 Web 历史，计费和压缩费用归目标所有者。
    async def answer(self, target, context, session, *, principal=None, resume, store, progress, commit, model_output, claim):
        user = {'id': target['user_id'], 'username': target['username'], 'account_status': target['account_status']}
        question = '\n\n用户补充：'.join(resume['inputs'])
        request = ChatRequest(channel='avatar', message=question)
        overview, model = self.chat.context(user, request, target, private_access=False)
        config, key, system, history = model
        config = {**config, '_aip_stream_progress':model_output}
        if not config.get('_direct_chat'):
            config = {**config, '_avatar_agent':True, '_allow_user_questions':False,
                      '_agent_checkpoint':resume.get('checkpoint',{}),
                      '_run_store':store, '_run_input_id':resume['input_id'], '_run_supplement':resume['supplement'],
                      '_auto_recover':resume['automatic']}
            prefix, raw = system.split('\n', 1)
            background = json.loads(raw)
            for name in ('available_skills', '_skills_revision'):
                background.pop(name, None)
            prefix += ' 当前为小镇问答。资料只通过混合检索获取，只交付文字答复。无法中途提问；缺少信息时，在最终答复中说明缺口。'
            if resume.get('clarification'):
                background['last_clarification'] = resume['clarification']
                prefix += ' last_clarification 是上一轮你向用户提出的问题，只用于理解本轮补充。'
            system = prefix+'\n'+json.dumps(background,ensure_ascii=False)
        model = (config, key, system, history)
        caller = context.peer_aic if principal is None else 'visitor:' + principal['visitor_id']
        platform = 'acps-direct' if principal is None else 'town-guest' if principal.get('anonymous') else 'town-public'
        conversation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dump(['acps-direct', target['aic'], caller, context.group_id or '', session] if principal is None else [platform, target['aic'], context.peer_aic, caller, context.group_id or '', session])))
        memory = self.chat.memory
        conversation = memory.scope(user['id'], 'acps', target['aic'], conversation_id,
                                    caller_id=caller, platform=platform, title='小镇公开访客对话' if principal else '小镇分身对话', create=True)
        claim(conversation_id)
        try:
            from avatar.conversation.loop.entry_tools import bind_entry_tools
            from avatar.models.trace import model_io_scope
            with model_io_scope(progress,private=False):
                if config.get('_direct_chat'):
                    progress({'type':'status','stage':'result_writing'})
                    model = await self.chat.prepare_direct(model, conversation, question)
                    result = await self.chat.model_client.generate(*model, question)
                else:
                    model = await memory.prepare(conversation, overview['agent']['last_modified_at'], model, question,
                                                 self.chat.model_client, user['id'], progress=progress)
                    model = await bind_entry_tools(self.chat,model,user['id'],target['aic'],conversation,[],
                        progress=progress,search_only=True)
                    result = await self.chat.rag.answer(model, question, progress=progress)
            if not result.text.strip():
                raise HTTPException(502, '模型未返回有效回答')
            validate_sandbox = model[0].get('_validate_sandbox')
            if validate_sandbox:
                validate_sandbox()
            overview['_target'] = {'aic': target['aic'], 'name': overview['agent']['profile']['avatar_name']}
            self.chat.save(user, request.model_copy(update={'channel': 'acps', 'conversation_id': conversation_id}),
                           overview, result.text, result.usage, caller_id=caller, commit_in=lambda db:commit(db,result.text))
            return result.text
        except AgentNeedsInput as exc:
            with self.database.connect(write=True) as db:
                store.check_in(db)
                if exc.usage:
                    config = overview['agent']['model_config']
                    db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id) VALUES(?,?,?,?,?,?,?)',
                               (user['id'], config['provider'], config['model'], dump(exc.usage), now(), caller, conversation_id))
                commit(db,exc.question or str(exc),pause=exc)
