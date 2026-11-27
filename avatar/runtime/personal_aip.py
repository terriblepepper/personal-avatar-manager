# 模块说明：个人分身独占端口 mTLS 入口，每账户独立证书、Endpoint 与动态就绪检查。
import asyncio
import json
import logging
from urllib.parse import urlsplit
from avatar.core.dispatch import begin_work, wait_work
from avatar.core.execution import run_blocking
from wit import WitAgent
from wit.acps.aip.direct import BaseDirectPartnerComponent, DirectRpcError, DIRECT_MTLS_SERVER
from avatar.protocols.transport.direct_leader import DirectLeaderComponent
from wit.acps.identity import current_local_entity
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.registration.entity import entity_identity_paths
from avatar.protocols.transport.endpoints import entity_endpoint, registered_oidc_matches
from avatar.protocols.errors import RegistrationFailure
from avatar.protocols.transport.diagnostics import RpcDiagnostics
from avatar.protocols.transport.directory import CachedEntityDirectory,PassiveTlsListener
from avatar.protocols.identity.document import LocalEntityDirectory
from avatar.protocols.tasks.personal import PersonalTaskService
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.core.db import now

log = logging.getLogger(__name__)


# 职责：接收某一个个人分身的真实 mTLS 调用。
# 输入：task_service 为共享问答服务，user_id/AIC 是服务器装配时固定的账户身份。
# 结果与约束：不允许消息修改目标或冒充 peer，不能调用管理功能，也不执行账号绑定。
class PersonalPartnerHandler(BaseDirectPartnerComponent):
    name = 'personal-avatar-partner'

    # 职责：绑定一个个人分身的目标和端点配置。
    # 输入：service、user_id、aic 由服务端提供，kwargs 为框架公开监听参数。
    # 结果与约束：所有请求永远使用同一目标账户。
    def __init__(self, service, user_id, aic, admission_directory=None, **kwargs):
        super().__init__(**kwargs)
        self.service, self.user_id, self.aic = service, user_id, aic
        self.admission_directory=admission_directory
        self.diagnostics=RpcDiagnostics(service.settings.data_dir/'users'/user_id/'identity'/'rpc-diagnostics.jsonl')

    async def _teardown(self,ctx):
        await run_blocking(self.diagnostics.close)
        await super()._teardown(ctx)

    # 职责：把通过传输认证的命令交给个人任务服务。
    # 输入：command 与 context 遵循 WIT Direct 契约。
    # 结果与约束：同时核对 TLS peer、当前本地证书身份、目标 AIC；公开错误不包含私钥或数据库正文。
    async def _handle(self, command, *, context):
        entity = current_local_entity()
        if entity is None or entity.aic != self.aic or context.local_aic != self.aic or command.get('senderId') != context.peer_aic:
            raise DirectRpcError({'code': -32003, 'message': '个人分身 RPC 必须通过真实 mTLS 连接'})
        if self.admission_directory is not None:
            try:
                if await self.admission_directory.select(entity.aic) != entity:
                    raise ValueError('本地身份已在接入后更新')
            except Exception:raise DirectRpcError({'code':-32003,'message':'本地身份证书无效或正在更新，请由管理员核对部署材料'}) from None
        if context.peer_aic==self.aic and command.get('command')=='get' and str(command.get('taskId','')).startswith('health-'):
            # 本人证书发起的只读探测：返回标准任务不存在，不进入访客会话或模型流程。
            raise DirectRpcError({'code':-32001,'message':'Health 探测任务不存在；分身 ACPS 端点正常响应'})
        try:
            await run_blocking(self.diagnostics.write, event='request',peer_aic=context.peer_aic,request=command,header_names=list(context.headers))
            result=await self.service.handle(command,context,self.user_id,self.aic)
            await run_blocking(self.diagnostics.write, event='response',peer_aic=context.peer_aic,response=result)
            return result
        except ManagementTaskError as exc:
            await run_blocking(self.diagnostics.write, event='error',peer_aic=context.peer_aic,code=exc.code,message=exc.message)
            raise DirectRpcError({'code': exc.code, 'message': exc.message}) from None


# 职责：为每个个人 Endpoint 装配原生独占端口监听器；拓扑变化先关入口再重建。
# 证书仍由账户独立 ATR 目录维护；不能通过消息或路径覆盖 TLS 选定的目标所有者。
class PersonalAipRuntime:
    def __init__(self, settings, chat):
        self.settings, self.database = settings, chat.service.database
        self.tasks = PersonalTaskService(chat, settings)
        self.instances, self.failures = {}, {}
        self.directories = {}
        self.failure_reasons = {}
        self.health_checks={}
        self.sync_lock=asyncio.Lock()
        self.ingress = None
        self._topology = None

    # 职责：核验 ACS、OIDC、端点与证书，并启动独占端口的单实体 Partner。
    # 任意字段不一致拒绝该实体，不能降级为匿名或共用其他账户的证书。
    def _materials(self, row):
        # 旧 ACS 的身份由下方可信绑定核验；切换登录 issuer 不重新分配端口。
        paths = entity_identity_paths(self.settings.data_dir, row['user_id'])
        document = json.loads(paths.acs.read_text())
        with self.database.connect() as db:
            endpoint = entity_endpoint(self.settings, row['user_id'], allocate=False, db=db)
            bound = registered_oidc_matches(self.settings, row['user_id'], document.get('entityMeta', {}), db=db)
        if (document.get('aic') != row['aic'] or document.get('entityUserId') != row['user_id']
                or document.get('endPoints') != [endpoint]
                or not bound):
            raise ValueError('正式个人 ACS 与当前账户、OIDC 或固定端点不一致')
        config = read_atr_config(paths.registration)
        if config.mode not in ('entity','integrated'):
            raise ValueError('个人端点必须使用正式实体证书')
        return endpoint, config

    async def activate(self, row):
        endpoint, config = await run_blocking(self._materials, row)
        directory = CachedEntityDirectory({row['aic']: config}, LocalEntityDirectory({row['aic']: config}), ttl=60)
        await directory.select(row['aic'])
        parsed = urlsplit(endpoint['url'])
        if any(urlsplit(item[3]).port == parsed.port for item in self.instances.values()):
            raise ValueError('重复的个人端口，拒绝覆盖其他实体绑定')
        agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'entry': 'personal-avatar'})
        agent.install(DirectLeaderComponent(directory=directory), required=True)
        listener=PassiveTlsListener(directory,local_aic=row['aic'],
            bind_address=self.settings.system_aip_bind_address,bind_port=parsed.port,
            max_connections=128,max_streams=1,request_timeout=180,max_body_bytes=65536)
        handler = await run_blocking(PersonalPartnerHandler, self.tasks, row['user_id'], row['aic'],
                                     admission_directory=directory, server_name=parsed.hostname,
                                     base_path='/aip', public_endpoint=endpoint['url'], listener=listener)
        agent.install(handler, required=True)
        try:
            await agent.start()
        except BaseException:
            await agent.stop()
            raise
        self.directories[row['user_id']] = directory
        self.instances[row['user_id']] = (row['aic'],agent,agent.context.get(DIRECT_MTLS_SERVER),endpoint['url'])

    # 职责：关闭各实体监听器；允许重复调用，部分启动失败也能清理。
    async def stop(self):
        if self.ingress:
            await self.ingress.stop()
            self.ingress = None
        await asyncio.gather(*(agent.stop() for _, agent, _, _ in self.instances.values()), return_exceptions=True)
        await self.tasks.stop()
        self.instances.clear()
        self.directories.clear()
        self._topology = None

    # 职责：协调注册、注销、账号改名后的拓扑；启动失败等待手动 Health。
    # 拓扑变化时重建；单个失败重试只补启动缺失实体，避免清空其他实体的有效缓存。
    async def sync(self):
        async with self.sync_lock:
            await self._sync()

    async def _sync(self):
        import time
        if not self.settings.entity_aip_enabled:
            await self.stop()
            return
        def active_entities():
            with self.database.connect() as db:
                return db.execute("""SELECT a.user_id,a.aic,o.username,o.subject,o.issuer,p.port,
                    (SELECT jsonb_agg(jsonb_build_array(l.issuer,l.subject,l.username)
                        ORDER BY l.issuer,l.subject,l.username)::text FROM oidc_identity_links l
                        WHERE l.user_id=a.user_id) AS linked_identities
                    FROM avatar_records a JOIN users u ON u.id=a.user_id
                    LEFT JOIN oidc_identities o ON o.user_id=u.id
                    LEFT JOIN avatar_entity_ports p ON p.user_id=u.id
                    WHERE a.aic NOT LIKE 'DEMO-%' AND a.identity_status='active'
                    AND u.account_status='active' ORDER BY a.user_id""").fetchall()
        rows = await run_blocking(active_entities)
        topology = tuple(tuple(row) for row in rows)
        if topology == self._topology and not any(deadline <= time.time() for deadline in self.failures.values()):
            return
        previous={item[0]:item for item in self._topology or ()}
        current={item[0]:item for item in topology}
        for uid in list(self.instances):
            if current.get(uid)!=previous.get(uid):
                await self.instances.pop(uid)[1].stop()
                self.directories.pop(uid,None)
                self.failures.pop(uid,None)
                self.health_checks.pop(uid,None)
        for uid in list(self.failures):
            if current.get(uid)!=previous.get(uid):
                self.failures.pop(uid,None)
                self.failure_reasons.pop(uid,None)
        self.failures = {uid: value for uid, value in self.failures.items() if any(r['user_id'] == uid for r in rows)}
        self.failure_reasons = {uid: reason for uid, reason in self.failure_reasons.items() if uid in self.failures}
        try:
            for row in rows:
                if row['user_id'] in self.instances or self.failures.get(row['user_id'], 0) > time.time():
                    continue
                try:
                    await self.activate(row)
                    self.failures.pop(row['user_id'], None)
                    self.failure_reasons.pop(row['user_id'], None)
                except Exception as exc:
                    self.failures[row['user_id']] = float('inf')
                    import httpx
                    self.failure_reasons[row['user_id']] = ('证书平台限流，请手动 Health 检查' if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==429 else '入口尚未就绪，请手动 Health 检查')
                    log.warning('个人协议未就绪：user=%s type=%s', row['user_id'], type(exc).__name__)
            self._topology = topology
        except BaseException:
            await self.stop()
            raise

    # 职责：恢复中断任务后轮询状态；应用退出始终先关闭入口。
    async def run(self):
        recovered = False
        try:
            while True:
                latch = begin_work(self.database, 'topology')
                try:
                    if not recovered:
                        await run_blocking(self.tasks.recover)
                        recovered = True
                    await self.sync()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning('个人端口监听协调异常：%s', type(exc).__name__)
                await wait_work(latch, 30., reconcile=30.)
        finally:
            await self.stop()

    def operation_protocol(self, user_id, fallback_endpoint):
        instance = self.instances.get(user_id)
        return {'endpoint': (instance[3] if instance else None) or fallback_endpoint, 'health': self.health_checks.get(user_id)}

    def online_count(self):
        return sum(bool(instance[2] and instance[2].serving) for instance in tuple(self.instances.values()))

    # 职责：报告真实 TLS 接纳状态，不把已分配 AIC 或已登记端点误报为上线。
    def status(self, user_id, *, endpoint_result=None):
        instance = self.instances.get(user_id)
        directory = self.directories.get(user_id)
        serving = bool(instance and instance[2] and instance[2].serving)
        if not self.settings.entity_aip_enabled:
            state,reason='disabled','未启用 ACPS 通信入口'
        elif serving:
            state,reason='online',''
        elif user_id in self.failures:
            state,reason='waiting',self.failure_reasons.get(user_id,'通信入口正在恢复')
        elif instance:
            limited=directory and instance[0] in directory.cooldown
            state,reason='waiting',('证书平台限流，等待重新校验' if limited else '证书状态校验或通信入口暂未就绪')
        else:
            state,reason='offline','正式身份尚未上线或已停用'
        endpoint=instance[3] if instance else None
        if not endpoint and self.settings.entity_aip_enabled:
            if endpoint_result is not None:
                endpoint, error = endpoint_result
                if error and user_id not in self.failures and not instance:
                    reason = error
            else:
                try:
                    with self.database.connect() as db:
                        configured = entity_endpoint(self.settings, user_id, allocate=False, db=db)
                    endpoint=configured['url'] if configured else None
                except RegistrationFailure as exc:
                    if user_id not in self.failures and not instance:reason=str(exc)
        return {'enabled': self.settings.entity_aip_enabled, 'serving':serving,'health':self.health_checks.get(user_id),
                'state':state,'reason':reason,'endpoint':endpoint}

    async def health(self,user_id):
        from fastapi import HTTPException
        def identity_status():
            with self.database.connect() as db:
                return db.execute('SELECT identity_status FROM avatar_records WHERE user_id=?',(user_id,)).fetchone()
        row=await asyncio.to_thread(identity_status)
        if not row or row[0]!='active':raise HTTPException(409,'正式身份尚未部署，请先完成注册和证书签发')
        try:
            async with asyncio.timeout(30):
                starting=user_id not in self.instances
                if starting:
                    self.failures[user_id]=0
                    await self.sync()
                instance=self.instances.get(user_id)
                if not instance:raise ValueError('端点未成功启动')
                directory=self.directories[user_id]
                entity=await directory.select(instance[0])
                if not instance[2].serving:raise ValueError('端点未监听')
                import uuid
                from wit.acps.aip.base import AipContext
                from wit.acps.identity import use_local_entity
                from wit.acps.aip.direct import DIRECT_LEADER,DirectRpcError
                token=uuid.uuid4().hex
                command={'type':'task-command','id':uuid.uuid4().hex,'sentAt':now(),'senderRole':'leader',
                    'senderId':instance[0],'mentions':[instance[0]],'command':'get','taskId':'health-'+token,'sessionId':'health-'+token,'dataItems':[]}
                with use_local_entity(entity):
                    try:
                        await instance[1].context.get(DIRECT_LEADER).get_task(command,context=AipContext(local_aic=instance[0],peer_aic=instance[0]),endpoint=instance[3])
                        raise ValueError('探测任务标识不应存在')
                    except DirectRpcError as error:
                        if error.error.get('code')!=-32001:raise
            result={'ok':True,'checked_at':now(),'message':'分身 URL 的 mTLS 及 ACPS 只读请求检查通过'}
        except Exception:
            result={'ok':False,'checked_at':now(),'message':'分身 URL 的 mTLS / ACPS 检查未通过；未查询注册服务器，部署记录保留'}
        self.health_checks[user_id]=result
        return result
