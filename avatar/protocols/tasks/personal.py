# 模块说明：个人分身 Direct 任务、Keycloak 用户鉴权、会话隔离和真实模型问答。
import asyncio
import json
import logging
import time
import uuid
from weakref import WeakValueDictionary
from contextlib import ExitStack
from datetime import datetime, timezone
from fastapi import HTTPException
from avatar.accounts.auth import prune_auth_limits
from avatar.core.text_budget import fits_text_budget
from avatar.core.db import dump, now
from avatar.core.execution import AgentNeedsInput, execution_guard, finish_on_cancel, run_blocking
from avatar.conversation.loop.runtime import prepare_checkpoint, resume_safe
from avatar.conversation.tools.reply_artifacts import without_source_footer
from avatar.conversation.loop.store import RunConflict, RunStore
from avatar.protocols.identity.ontology import AVATAR_SKILL_IDS
from avatar.protocols.tasks import personal_turns as turns
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.protocols.tasks.town_auth import TownUserAuthorization
from avatar.protocols.tasks.town_public import TownPublicAuthorization
from avatar.protocols.tasks.town_files import TownFiles
from avatar.protocols.tasks.visitor_quota import charge_visitor, visitor_busy
from avatar.protocols.tasks.guest_maintenance import expire_guests, expired_guest
from avatar.protocols.tasks.town_answer import answer_town
from avatar.protocols.tasks.town_progress import PROGRESS_INTERVAL, WORKING_TIMEOUT, WorkingProgress


log = logging.getLogger('uvicorn.error.' + __name__)


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
        self.admission_locks = WeakValueDictionary()
        self.accepting = set()
        self.running = set()
        self.pending = {}
        self.progress = {}
        self.authorization = TownUserAuthorization(self.database, settings)
        self.public_authorization = TownPublicAuthorization(self.database, settings)
        self.settings = settings
        self.files = TownFiles(chat, settings)

    def download(self, rowid, token, identifier):
        return self.files.download(rowid, token, identifier)

    def expire_guests(self, batch_size=100):
        expire_guests(self.database, self.chat.memory, batch_size)

    async def maintenance(self):
        while True:
            for action, args in ((prune_auth_limits, (self.database,)), (self.expire_guests, ())):
                try:
                    await run_blocking(action, *args)
                except Exception:
                    log.exception('Background retention failed for %s', action.__name__)
            await asyncio.sleep(60)

    # 职责：恢复上次进程未完成的模型任务。
    # 输入：应用启动时调用一次。
    # 结果与约束：只回收过期执行权，等原调用者重新鉴权后续跑。
    def recover(self):
        if getattr(self,'peer_dialogues',None):self.peer_dialogues.recover()
        with self.database.connect(write=True) as db:
            for row in db.execute("""SELECT task_number,peer_aic,document,deadline FROM avatar_aip_tasks
                WHERE (document::jsonb->'status'->>'state')='working' AND (deadline IS NULL OR deadline<=?)""",
                (time.time(),)).fetchall():
                if not expired_guest(row):
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
    def task_row(db, scope, *, replace_expired=False):
        aic, peer, group, session, task = scope
        rows = db.execute("""SELECT * FROM avatar_aip_tasks WHERE local_aic=? AND peer_aic=? AND group_id=? AND task_id=?
                             AND (session_id=? OR (session_id='' AND (document::jsonb->>'sessionId')=?))""",
                          (aic, peer, group, task, session, session)).fetchall()
        if len(rows) > 1:
            raise ManagementTaskError(-32602, '请提供原任务会话标识')
        if rows and expired_guest(rows[0]):
            if replace_expired:
                db.execute('DELETE FROM avatar_aip_tasks WHERE task_number=?', (rows[0]['task_number'],))
            return None
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
        result = await self._handle(command, context, user_id, aic)
        for product in result.get('products') or ():
            for index, item in enumerate(product.get('dataItems', ())):
                if item.get('type') == 'text' and '引用来源' in item['text']:
                    product['dataItems'][index] = {**item, 'text': without_source_footer(item['text'])}
        if command['command'] in ('start','get','continue') and any(
                (item.get('metadata') or {}).get('inlineHtml') for product in (result.get('products') or ())
                for item in product.get('dataItems',())):
            return await run_blocking(self.files.with_html_body,result)
        return result

    async def _handle(self, command, context, user_id, aic):
        target = await run_blocking(self.target, user_id, aic)
        headers={k.lower():v for k,v in getattr(context,'headers',{}).items()}
        public = headers.get('x-avatar-access')=='public' or not headers.get('authorization')
        if headers.get('x-avatar-access')!='public' and (command.get('dataItems') == [{'type':'data','data':{'peerDialogueTask':command.get('taskId')}}] or command.get('command') in ('get','complete','cancel') and await run_blocking(self.is_peer_task, command)):
            handler=getattr(self,'peer_dialogues',None)
            if handler is None:raise ManagementTaskError(-32009,'分身委派入口未启用')
            return await handler.handle(command,context,user_id)
        principal = await self.public_authorization.authorize(context) if public else None
        if not public:
            await self.authorization.authorize(context, user_id)
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
        accepted = await finish_on_cancel(self._accept(
            command, context, user_id, aic, target, principal, public, scope, operation, session))
        if isinstance(accepted, dict):
            return accepted
        task, progress, result, scope = accepted
        try:return await asyncio.wait_for(asyncio.shield(task),timeout=1)
        except TimeoutError:return progress.view(result)
        except asyncio.CancelledError:
            if not task.cancelled() or asyncio.current_task().cancelling():
                raise
            return await run_blocking(self._public_result, scope)

    def _public_result(self, scope):
        with self.database.connect() as db:
            row = self.task_row(db,scope)
            if not row:
                raise ManagementTaskError(-32001,'原任务已删除')
            return turns.public_result(json.loads(row['document']))

    async def _accept(self, command, context, user_id, aic, target, principal, public, scope, operation, session):
        accepting = asyncio.current_task()
        self.accepting.add(accepting)
        # The receipt may use an assigned session ID. Both spellings share this lock.
        key = (*scope[:3], scope[-1])
        lock = self.admission_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                action, scope, payload = await run_blocking(
                    self._admit, command, context, user_id, aic, target, principal, public, scope, operation, session)
                return self._after_commit(action, scope, payload, context, principal)
        finally:
            self.accepting.discard(accepting)

    def _after_commit(self, action, scope, payload, context, principal):
        if action == 'view':
            return self.working_view(scope, payload)
        if action == 'cancel':
            writer, result = payload
            progress = self.progress.get(scope)
            if progress and progress.result_id == writer and scope in self.pending:
                self.pending[scope].cancel()
            return result
        # Return a status before a slow model outlives the caller's HTTP timeout.
        rowid, result, run, restarting = payload
        if restarting and scope in self.pending:
            self.pending[scope].cancel()
        progress = WorkingProgress(result,run)
        self.progress[scope] = progress
        task=asyncio.create_task(self.generate(scope,rowid,result,context,principal,run,progress))
        self.pending[scope]=task
        self.running.add(task)
        def done(job):
            self.running.discard(job)
            if self.pending.get(scope) is job:
                self.pending.pop(scope,None)
                self.progress.pop(scope,None)
            if not job.cancelled() and job.exception():
                log.error('Personal AIP worker taskId=%r sessionId=%r run_id=%s error=%s',
                          *progress.ids, type(job.exception()).__name__)
        task.add_done_callback(done)
        return task, progress, result, scope

    def _admit(self, command, context, user_id, aic, target, principal, public, scope, operation, session):
        scope_peer = scope[1]
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
                    return 'view', (*scope[:3], row['session_id'], scope[4]), turns.public_result(document)
        with ExitStack() as cleanup, self.database.connect(write=True, account_scope=user_id) as db:
            row = self.task_row(db, scope, replace_expired=(operation == 'start'))
            run = {}
            deliveries = []
            restarting = False
            if row:
                scope = (*scope[:3], row['session_id'], scope[4])
                document = json.loads(row['document'])
                expired = row['deadline'] is not None and row['deadline'] <= time.time()
                state = document['status']['state']
                deliveries = document.get('_file_deliveries',[])
                if deliveries and (expired or operation=='cancel'
                                   or operation=='complete' and state=='awaiting-completion'):
                    cleanup.callback(self.files.discard_previous,row['task_number'],deliveries)
                # The lease holder can have newer model progress than the last persisted checkpoint.
                if state == 'working' and (expired or row['deadline'] is None) and operation != 'cancel':
                    turns.interrupt_in(db, row, document)
                    state = document['status']['state']
                run = document.get('_agent_resume', {})
                if (operation == 'cancel' and state in ('working','awaiting-completion','awaiting-input')
                        or operation == 'complete' and state == 'awaiting-completion'
                        or expired and state == 'awaiting-completion'):
                    final = 'canceled' if operation == 'cancel' else 'failed' if expired else 'completed'
                    writer = document.get('id')
                    result = turns.close_in(db,row,document,final,reason='confirmation_timeout' if final=='failed' else None)
                    return 'cancel', scope, (writer, result)
                if state == 'awaiting-input' and run.get('user_question'):
                    return 'view', scope, turns.close_in(db,row,document,'failed',turns.QUESTION_UNAVAILABLE,reason='interactive_question_disabled')
                restarting = operation == 'get' and state == 'awaiting-input' and run.get('restart')
                if restarting:
                    if turns.work_expired(document):
                        return 'view', scope, turns.expire_in(db,row,document)
                    if public and visitor_busy(db,aic,scope_peer):
                        return 'view', scope, turns.public_result(document)
                if not restarting:
                    if operation != 'continue':
                        return 'view', scope, turns.public_result(document)
                    seen=db.execute('SELECT task_id FROM avatar_aip_task_commands WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND command_id=?',(*scope[:4],command['id'])).fetchone()
                    if seen:
                        if seen['task_id']!=scope[-1]:raise ManagementTaskError(-32602,'命令 id 已用于其他任务')
                        return 'view', scope, turns.public_result(document)
                    if state not in ('awaiting-input','awaiting-completion'):
                        raise ManagementTaskError(-32009,'任务正在处理，请先查询状态' if state=='working' else '任务已结束，请使用新 taskId 并沿用 sessionId 继续对话')
                session=document['sessionId']
            elif operation != 'start':
                raise ManagementTaskError(-32001, '当前调用者和会话中没有该任务')
            revision = turns.source_revision(db, user_id, aic)
            if run.get('sources') and (run['sources'] != revision or run['owner'] != user_id):
                return 'view', scope, turns.close_in(db,row,document,'failed','原任务的资料或授权已改变，请重新发起任务')
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
                    charge_visitor(db, user_id, aic, scope_peer, principal['visitor_id'])
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
            if deliveries:
                run['previous_deliveries'] = deliveries
            stored = dump({**result, '_agent_resume':run, **({'_file_deliveries':deliveries} if deliveries else {})})
            deadline = accepted_at+turns.LEASE_SECONDS
            if row:
                rowid = row['task_number']
                db.execute('UPDATE avatar_aip_tasks SET command_id=?,document=?,deadline=? WHERE task_number=?',
                           (row['command_id'] if restarting else command['id'],stored,deadline,rowid))
            else:
                db.execute('INSERT INTO avatar_aip_tasks(local_aic,peer_aic,group_id,session_id,task_id,command_id,document,deadline) VALUES(?,?,?,?,?,?,?,?)', (*scope, command['id'], stored, deadline))
                rowid = self.task_row(db, scope)['task_number']
            if not restarting:
                db.execute('INSERT INTO avatar_aip_task_commands VALUES(?,?,?,?,?,?)',(*scope,command['id']))
        return 'start', scope, (rowid, result, run, restarting)

    async def stop(self):
        if getattr(self,'peer_dialogues',None):await self.peer_dialogues.stop()
        if self.accepting:
            await asyncio.gather(*tuple(self.accepting), return_exceptions=True)
        tasks = tuple(self.running)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def generate(self,scope,rowid,result,context,principal,run,progress):
        writer, owner = result['id'], run['owner']
        def renew(conversation_id=None):
            turns.renew(self.database,rowid,writer,run,conversation_id,work_until=progress.deadline())
        def validate(db):
            document = turns.current_in(db,rowid,writer,run)
            progress.deadline()
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

        def commit(db, reply, pause=None, files=None):
            nonlocal committed
            store.check_in(db)
            state = 'awaiting-completion'
            if pause:
                state, reply = pause_result(pause)
                if pause.usage:
                    store.mark_usage_recorded_in(db)
            record, items = self.files.publish(db,rowid,owner,files)
            committed = turns.finish_in(db,rowid,result,run,state,reply,delivery=record,files=items)
        async def work():
            await self.limit.acquire()
            try:
                state = await run_blocking(store.acquire)
                if not resume_safe(state, initial=not run.get('claimed'), automatic=run['automatic'], allow_question=False):
                    raise HTTPException(409,'上次操作尚无确定回执，请核对已有结果后重新发起任务')
                await run_blocking(prepare_checkpoint, store, state, automatic=run['automatic'], allow_question=False)
                active = await run_blocking(self.target, owner, scope[0])
                self.approved_skills(active,run['skills'])
                with execution_guard(guard):
                    return await self.answer(active,context,result['sessionId'],principal=principal,
                                             resume=run,store=store,progress=progress.update,commit=commit,
                                             stream_state=progress.stream_state,claim=renew)
            finally:
                self.limit.release()
        task = asyncio.create_task(work())
        try:
            while not task.done():
                remaining = progress.deadline()-time.time()
                completed, _ = await asyncio.wait((task,),timeout=min(remaining,turns.RENEW_INTERVAL))
                if not completed:
                    await run_blocking(renew)
            task.result()
            if committed is None:
                raise RunConflict('缺少本次回答的提交回执')
            return committed
        except AgentNeedsInput as exc:
            state, message = pause_result(exc)
            return await run_blocking(turns.finish, self.database,rowid,result,run,state,message)
        except asyncio.CancelledError:
            # Fence the writer before draining an interrupted provider or a blocking tool.
            def interrupt():
                with self.database.connect(write=True, account_scope=owner) as db:
                    row = self.task_row(db,scope)
                    if row:
                        doc = json.loads(row['document'])
                        if doc['id']==writer and doc['status']['state']=='working' and doc.get('_agent_resume',{}).get('owner')==owner:
                            turns.interrupt_in(db,row,doc)
            await run_blocking(interrupt)
            raise
        except turns.WorkDeadlineExceeded:
            return await run_blocking(turns.finish, self.database,rowid,result,run,'failed',turns.TIMEOUT_MESSAGE,reason='working_timeout')
        except (RunConflict, TimeoutError, HTTPException, ManagementTaskError) as exc:
            message = (str(exc.detail) if isinstance(exc,HTTPException) else
                       '此次模型或工具调用超时，请重新发起任务' if isinstance(exc,TimeoutError) else str(exc))
            return await run_blocking(turns.finish, self.database,rowid,result,run,'failed',message,reason=type(exc).__name__)
        except Exception as exc:
            log.error('Personal AIP generation taskId=%r sessionId=%r run_id=%s error=%s',
                      *progress.ids, type(exc).__name__)
            return await run_blocking(turns.finish, self.database,rowid,result,run,'failed','模型问答失败，请检查目标分身的模型配置或稍后再试')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            if committed is None and (previous := run.get('previous_deliveries')):
                await run_blocking(self.files.discard_previous,rowid,previous)

    # 控制命令没有 dataItems；只在已存在的配对任务中选择分身鉴权路径，随后仍核对 TLS peer。
    def is_peer_task(self,command):
        with self.database.connect() as db:
            return bool(db.execute('SELECT 1 FROM agent_dialogues WHERE id=? AND session_id=?',(command.get('taskId'),command.get('sessionId'))).fetchone())

    async def answer(self, target, context, session, **options):
        return await answer_town(self.chat, self.files, target, context, session, **options)
