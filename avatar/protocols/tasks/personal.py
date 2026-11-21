# 模块说明：个人分身 Direct 任务、Keycloak 用户鉴权、会话隔离和真实模型问答。
import asyncio
import json
import logging
import time
import uuid
from fastapi import HTTPException
from avatar.core.db import dump, now
from avatar.core.schemas import ChatRequest
from avatar.core.execution import AgentNeedsInput
from avatar.protocols.identity.ontology import AVATAR_SKILL_IDS
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.protocols.tasks.town_auth import TownUserAuthorization
from avatar.protocols.tasks.town_public import TownPublicAuthorization


WORKING_TIMEOUT = 480
TIMEOUT_MESSAGE = '本轮达到分身执行时限，请缩小任务范围后再试'
PROGRESS_INTERVAL = 8
PUBLIC_STAGES = {
    'memory': '读取会话上下文', 'compressing': '整理会话上下文',
    'intent_analysis': '判断任务目标', 'thinking': '分析任务和已有信息',
    'tool_selection': '判断所需工具', 'rag_retrieving': '检索相关资料',
    'material_reading': '读取相关资料', 'tool_catalog': '查询候选资料',
    'tool_action': '执行已选择的操作', 'skill_execution': '运行已选技能',
    'result_writing': '整理最终答复',
}


class WorkingProgress:
    def __init__(self, result_id):
        self.result_id = result_id
        self.started = time.monotonic()
        self.message = '任务已接收，等待可用执行资源'
        self.revision = 0
        self.snapshot_key = None
        self.envelope = {}

    def update(self, event):
        if event.get('type') != 'status':
            return
        stage = PUBLIC_STAGES.get(event.get('stage'))
        if stage and stage != self.message:
            self.message = stage
            self.revision += 1

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
    # 结果与约束：中断任务记失败且不自动重新计费，已完成任务继续支持幂等查询。
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
            for row in db.execute("SELECT rowid,document FROM avatar_aip_tasks WHERE json_extract(document,'$.status.state')='working'").fetchall():
                result = json.loads(row['document'])
                result.pop('_agent_resume', None)
                result['status'] = {'state': 'failed', 'stateChangedAt': now(), 'dataItems': [{'type': 'text', 'text': '服务重启导致任务中断，请使用新任务重新提问'}]}
                result['id']=uuid.uuid4().hex;result['sentAt']=now()
                db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=NULL WHERE rowid=?', (dump(result), row['rowid']))

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
        if not 1 <= len(question) <= 2000:
            raise ManagementTaskError(-32602, '问题长度需为 1～2000 个字符')
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
        progress = self.progress.get(scope)
        if progress and progress.result_id==result['id'] and result['status']['state']=='working':
            return progress.view(result)
        return result

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
        if operation == 'get':
            with self.database.connect() as db:
                row = self.task_row(db, scope)
                if not row:
                    raise ManagementTaskError(-32001, '当前调用者和会话中没有该任务')
                if row['deadline'] is None or row['deadline'] > time.time():
                    result = json.loads(row['document'])
                    result.pop('_agent_resume', None)
                    return self.working_view((*scope[:3],row['session_id'],scope[4]),result)
        resume = None
        with self.database.connect(write=True) as db:
            # 空 session 的首次 start 也保存生成的会话 ID；允许调用者之后沿用该 ID 查询。
            row = self.task_row(db, scope)
            if row:
                result = json.loads(row['document'])
                state = result['status']['state']
                resume = result.pop('_agent_resume', None) if state == 'awaiting-input' else None
                next_state = ('failed' if row['deadline'] is not None and row['deadline'] <= time.time() else
                              'completed' if operation == 'complete' and state == 'awaiting-completion' else
                              'canceled' if operation == 'cancel' and state in ('working', 'awaiting-completion', 'awaiting-input') else None)
                if next_state:
                    result['status'] = {'state': next_state, 'stateChangedAt': now()}
                    if next_state == 'failed' and state == 'working':
                        result['status']['dataItems'] = [{'type': 'text', 'text': TIMEOUT_MESSAGE}]
                    result['id']=uuid.uuid4().hex;result['sentAt']=now()
                    db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=NULL WHERE rowid=?', (dump(result), row['rowid']))
                    job=self.pending.get((aic,scope_peer,context.group_id or '',row['session_id'],scope[-1]))
                    if job:job.cancel()
                if operation != 'continue':
                    return self.working_view((*scope[:3],row['session_id'],scope[4]),result)
                # Use the stored session key, including the alias generated for an empty first session.
                scope=(aic,scope_peer,context.group_id or '',row['session_id'],scope[-1])
                seen=db.execute('SELECT task_id FROM avatar_aip_task_commands WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND command_id=?',(*scope[:4],command['id'])).fetchone()
                if seen:
                    if seen['task_id']!=scope[-1]:raise ManagementTaskError(-32602,'命令 id 已用于其他任务')
                    return self.working_view(scope,result)
                if next_state=='failed':return result
                if state not in ('awaiting-input','awaiting-completion'):
                    raise ManagementTaskError(-32009,'任务正在处理，请先查询状态' if state=='working' else '任务已结束，请使用新 taskId 并沿用 sessionId 继续对话')
                session=result['sessionId']
            elif operation != 'start':
                raise ManagementTaskError(-32001, '当前调用者和会话中没有该任务')
            if db.execute('SELECT 1 FROM avatar_aip_task_commands WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND command_id=?',(*scope[:4],command['id'])).fetchone():
                raise ManagementTaskError(-32602, '命令 id 已用于其他任务')
            # 小镇负责发现及注册/登录引导；已认证请求直接进入对话，不提供 hello 握手。
            selected = dict(resume.get('skills', {})) if resume else {}
            question = self.question(command.get('dataItems'), self.approved_skills(target, selected), selected=selected)
            if resume and sum(len(text) + 8 for text in [*resume['inputs'], question]) > 16000:
                raise ManagementTaskError(-32602, '本任务补充内容已达上限，请结束后使用新任务整理要求')
            if public:
                day = now()[:10]
                from datetime import datetime, timedelta, timezone
                cutoff = (datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
                db.execute('DELETE FROM town_visitor_requests WHERE created_at<?', (cutoff,))
                if (db.execute('SELECT COUNT(*) FROM town_visitor_requests WHERE owner_id=? AND visitor_id=? AND created_at>=?',
                               (user_id, principal['visitor_id'], day)).fetchone()[0] >= self.settings.town_visitor_daily_limit
                        or db.execute('SELECT COUNT(*) FROM town_visitor_requests WHERE owner_id=? AND created_at>=?',
                                      (user_id, day)).fetchone()[0] >= self.settings.town_owner_daily_limit):
                    raise ManagementTaskError(-32010, '今日公开对话额度已用完')
                if db.execute("SELECT 1 FROM avatar_aip_tasks WHERE local_aic=? AND peer_aic=? AND json_extract(document,'$.status.state')='working' AND deadline>?",
                              (aic, scope_peer, time.time())).fetchone():
                    raise ManagementTaskError(-32010, '该访客已有请求正在处理')
                db.execute('INSERT INTO town_visitor_requests VALUES(?,?,?)', (user_id, principal['visitor_id'], now()))
            result = {'type': 'task-result', 'id': uuid.uuid4().hex, 'sentAt': now(), 'senderRole': 'partner',
                      'senderId': aic, 'mentions': [context.peer_aic], 'taskId': scope[-1],
                      'sessionId': session or uuid.uuid4().hex, 'status': {'state': 'working', 'stateChangedAt': now()}, 'products': []}
            if context.group_id:
                result['groupId'] = context.group_id
            if row:
                db.execute('UPDATE avatar_aip_tasks SET command_id=?,document=?,deadline=? WHERE rowid=?',(command['id'],dump(result),time.time()+WORKING_TIMEOUT,row['rowid']))
            else:
                db.execute('INSERT INTO avatar_aip_tasks VALUES(?,?,?,?,?,?,?,?)', (*scope, command['id'], dump(result), time.time()+WORKING_TIMEOUT))
            db.execute('INSERT INTO avatar_aip_task_commands VALUES(?,?,?,?,?,?)',(*scope,command['id']))
        # Return a status before a slow model outlives the caller's HTTP timeout.
        working=json.loads(dump(result))
        progress = WorkingProgress(result['id'])
        self.progress[scope] = progress
        task=asyncio.create_task(self.generate(scope,result,target,context,question,principal,public,resume,selected,progress))
        self.pending[scope]=task
        def done(job):
            if self.pending.get(scope) is job:
                self.pending.pop(scope,None)
                self.progress.pop(scope,None)
            if not job.cancelled() and job.exception():
                logging.getLogger(__name__).error('个人协议后台任务失败：%s',type(job.exception()).__name__)
        task.add_done_callback(done)
        try:return await asyncio.wait_for(asyncio.shield(task),timeout=1)
        except TimeoutError:return progress.view(working)

    async def stop(self):
        if getattr(self,'peer_dialogues',None):await self.peer_dialogues.stop()
        tasks=list(self.pending.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def generate(self,scope,result,target,context,question,principal,public,resume=None,selected=None,progress=None):
        user_id,aic=target['user_id'],scope[0]
        try:
            async with asyncio.timeout(WORKING_TIMEOUT), self.limit:
                # 等待全局并发槽后再次核对停用状态，不把请求正文当作身份来源。
                target = self.target(user_id, aic)
                if selected:
                    self.approved_skills(target, selected)
                options = {'principal': principal} if public else {}
                if resume:
                    options['resume'] = resume
                if progress:
                    options['progress'] = progress.update
                reply = await self.answer(target, context, result['sessionId'], question, **options)
            result['products'] = [{'id': uuid.uuid4().hex, 'dataItems': [{'type': 'text', 'text': reply}]}]
            result['status'] = {'state': 'awaiting-completion', 'stateChangedAt': now()}
        except AgentNeedsInput as exc:
            checkpoint = {'inputs': [*(resume['inputs'] if resume else []), question], 'checkpoint': exc.checkpoint,
                          'skills': selected or {}, 'clarification': exc.question}
            return self.finish(scope, result, 'awaiting-input', exc.question or str(exc), resume=checkpoint)
        except TimeoutError:
            return self.finish(scope, result, 'failed', TIMEOUT_MESSAGE)
        except asyncio.CancelledError:
            self.finish(scope, result, 'failed', '请求中断，请通过 get 核对任务后再决定是否重新提问')
            raise
        except HTTPException as exc:
            return self.finish(scope, result, 'failed', str(exc.detail))
        except Exception:
            logging.getLogger(__name__).exception('个人协议任务生成失败；未重试模型调用')
            return self.finish(scope, result, 'failed', '模型问答失败，请检查目标分身的模型配置或稍后再试')
        return self.finish(scope, result)

    # 控制命令没有 dataItems；只在已存在的配对任务中选择分身鉴权路径，随后仍核对 TLS peer。
    def is_peer_task(self,command):
        with self.database.connect() as db:
            return bool(db.execute('SELECT 1 FROM agent_dialogues WHERE id=? AND session_id=?',(command.get('taskId'),command.get('sessionId'))).fetchone())

    # 职责：保存当前任务结果，保留并发取消或超时的终态。
    # 输入：scope 为任务完整身份，result 为本次结果，可选固定失败提示。
    # 结果与约束：只更新原任务，不覆盖已取消结果；成功等待 Leader 确认最多 300 秒。
    def finish(self, scope, result, state=None, message=None, *, resume=None):
        working_id = result['id']
        if state:
            result['status'] = {'state': state, 'stateChangedAt': now(), 'dataItems': [{'type': 'text', 'text': message}]}
        result['id'] = uuid.uuid4().hex
        result['sentAt'] = now()
        with self.database.connect(write=True) as db:
            row = db.execute('SELECT document FROM avatar_aip_tasks WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND task_id=?', scope).fetchone()
            current = json.loads(row['document'])
            if current['status']['state'] != 'working' or current['id'] != working_id:
                current.pop('_agent_resume', None)
                return current
            stored = {**result, '_agent_resume': resume} if resume else result
            db.execute('UPDATE avatar_aip_tasks SET document=?,deadline=? WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_id=? AND task_id=?',
                       (dump(stored), time.time()+300 if not state else None, *scope))
        return result

    # 职责：使用已通过本人鉴权的所有者资料和模型配置生成个人回答。
    # 输入：target 为服务端固定目标，context 是认证 peer/group，session 来自协议会话。
    # 结果与约束：不创建或绑定小镇用户账号；协议会话不混入 Web 历史，计费和压缩费用归目标所有者。
    async def answer(self, target, context, session, question, *, principal=None, resume=None, progress=None):
        user = {'id': target['user_id'], 'username': target['username'], 'account_status': target['account_status']}
        request = ChatRequest(channel='avatar', message=question)
        if resume:
            question = '\n\n用户补充：'.join([*resume['inputs'], question])
            request = request.model_copy(update={'message': question})
        overview, model = self.chat.context(user, request, target, private_access=False)
        model = ({**model[0], '_avatar_agent': True, '_agent_checkpoint': resume['checkpoint'] if resume else {}}, *model[1:])
        caller = context.peer_aic if principal is None else 'visitor:' + principal['visitor_id']
        platform = 'acps-direct' if principal is None else 'town-guest' if principal.get('anonymous') else 'town-public'
        if principal is not None or (resume and resume.get('clarification')):
            config, key, system, history = model
            prefix, raw = system.split('\n', 1)
            background = json.loads(raw)
            if principal is not None:
                for name in ('available_skills', '_skills_revision', 'platform_avatars', 'platform_directory_rules'):
                    background.pop(name, None)
                prefix += ' 当前为公开访客问答，仅使用公开档案与公开附件；不得执行主人技能、私人工具、委派或管理操作。'
            if resume and resume.get('clarification'):
                background['last_clarification'] = resume['clarification']
                prefix += ' last_clarification 是上一轮你向用户提出的问题，只用于理解本轮补充。'
            model = (config, key, prefix+'\n'+json.dumps(background,ensure_ascii=False), history)
        conversation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dump(['acps-direct', target['aic'], caller, context.group_id or '', session] if principal is None else [platform, target['aic'], context.peer_aic, caller, context.group_id or '', session])))
        memory = self.chat.memory
        conversation = memory.scope(user['id'], 'acps', target['aic'], conversation_id,
                                    caller_id=caller, platform=platform, title='小镇公开访客对话' if principal else '小镇分身对话', create=True)
        memory.acquire(conversation_id)
        try:
            model = await memory.prepare(conversation, overview['agent']['last_modified_at'], model, question,
                                         self.chat.model_client, user['id'], progress=progress)
            if principal is None:
                model = await self.chat.skills.load_context(user['id'],model,question,self.chat.model_client,caller)
                model = await self.chat.skill_execution.enrich(user['id'],model,question,self.chat.model_client,caller_id=caller,conversation_id=conversation['id'],progress=progress)
            model=await self.chat.rag.enrich(user['id'],target['aic'],conversation,[],model,question,include_private=bool(overview.get('_private_context')),progress=progress)
            result = await self.chat.rag.answer(model, question, progress=progress)
            if not result.text.strip():
                raise HTTPException(502, '模型未返回有效回答')
            self.target(user['id'], target['aic'])
            overview['_target'] = {'aic': target['aic'], 'name': overview['agent']['profile']['avatar_name']}
            self.chat.save(user, request.model_copy(update={'channel': 'acps', 'conversation_id': conversation_id}),
                           overview, result.text, result.usage, caller_id=caller)
            return result.text
        except AgentNeedsInput as exc:
            if exc.usage:
                config = overview['agent']['model_config']
                with self.database.connect(write=True) as db:
                    db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id) VALUES(?,?,?,?,?,?,?)',
                               (user['id'], config['provider'], config['model'], dump(exc.usage), now(), caller, conversation_id))
            raise
        finally:
            memory.release(conversation_id)
