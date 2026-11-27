# 模块说明：管理系统普通服务 ATR 自动推进器；一个 ACS、一个身份，证书就绪后直接交接 mTLS 监听。
import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from avatar.protocols.transport.certificates import registration_component
from wit import WitAgent
from wit.acps.atr import TRUSTED_REGISTRATION, RegistryServerCredentials
from avatar.core.db import now
from avatar.core.execution import blocking_context, run_blocking
from avatar.protocols.identity import document as identity_document
from avatar.protocols.identity.document import wit_candidate_config, sync_component_acs
from avatar.protocols.identity.system import system_identity_paths, initialize_system_identity
from avatar.protocols.registration.system import RegistrationConfigurationError, validate_system_registration, validate_ca_directory, publish_system_acs
from avatar.runtime.system_aip import system_registration_config

logger = logging.getLogger(__name__)

MESSAGES = {
    'disabled': '自动注册未启用，请补齐配置后开启。',
    'registry_credentials_missing': '请在 .env 配置 WIT_REGISTRY_USERNAME 和 WIT_REGISTRY_PASSWORD。',
    'trusted_ca_missing': '请提供经平台确认的可信 CA 文件，路径见 registration.json。',
    'trusted_ca_invalid': '可信 CA 文件不是可用的 PEM 信任链。',
    'ca_https_required': 'CA 根地址必须使用与 ACME directory 广告一致的 HTTPS 地址。',
    'ca_origin_mismatch': 'CA 根地址与 directory 广告的 origin 或部署前缀不一致。',
    'ca_directory_invalid': 'CA directory 响应不完整或格式无效。',
    'invalid_candidate': 'ACS 候选无效；检查候选格式或正式 ACS 与原 ATR 状态是否一致，不改写原候选指纹。',
    'ontology_mode_required': '个人分身本体需要 ontology 模式，不能使用普通管理服务身份。',
    'system_mode_required': '管理系统需要 integrated 普通服务模式；已有本体状态须按平台规范处理，不能覆盖重注册。',
    'ontology_endpoint_forbidden': '个人分身本体不直接提供服务端点。',
    'candidate_endpoint_mismatch': '候选身份标识或管理服务端点与配置不一致。',
    'certificate_san_mismatch': 'ACS 证书 SAN 未覆盖协议公开地址的主机名或 IP。',
    'invalid_poll_configuration': '注册轮询或请求超时参数不在允许范围内。',
    'configuration_unavailable': '无法读取系统注册配置，请核对专用目录和文件。',
    'remote_error': '注册请求未完成，将于 1 小时后重试，也可由管理员手动刷新；若提交结果不确定，请保留状态并使用 reconcile 对账。',
    'blocked': '流程受阻，请查看根管理员的审核原因和 wit 状态，修复后重启；不要删除状态重注册。',
    'rejected': 'Registry 已拒绝申请，请按平台审核要求处理；不会自动创建新申请。',
    'pending_review': '已提交，等待 Registry 人工审核；平台不会自动调用 staff API 审批。',
    'approved': '审核已通过，继续推进证书签发。',
    'active': '正式身份和证书就绪；协议是否接入以 AIP serving 状态为准。',
    'initial': '准备提交或恢复已有注册。',
    'aip_not_ready': '身份已就绪，但协议尚未接通；将重试，请核对 AIC 约束、监听端口及 CA 状态。',
    'avatar_ontology_mismatch': '个人分身本体的用途或技能不正确，禁止使用管理系统 ACS 作为分身模板。',
}


# 职责：在 Web 生命周期中管理系统注册后台任务及注册状态投影。
# 输入：settings 提供敏感凭据和显式开关，aip 为独立系统协议运行时。
# 结果与约束：默认关闭；每次只调用公开 registration.up()，注册容器退出释放 ATR 文件锁后才加载 AIP，拒绝自动审核和重复身份回退。
class SystemRegistrationRuntime:
    # 职责：初始化后台任务持有者及只含元数据的状态。
    # 输入：settings 是不可变启动配置，aip 是 SystemAipRuntime。
    # 结果与约束：构造不读写磁盘或发起网络；状态不保存 ACS 候选、Token、证书或私钥。
    def __init__(self, settings, aip):
        self.settings, self.aip = settings, aip
        self.task = None
        self.wake = asyncio.Event()
        self.phase = 'initial'
        self.registration_id = self.aic = self.acs_fingerprint = None
        self.reason = ''
        self.code = 'disabled' if not self.enabled else 'initial'
        self.updated_at = None
        self.stage = 'registration'

    # 职责：读取当前注册任务的显式启用开关。
    # 输入：不可变 settings；个人分身本体子类覆盖为独立开关。
    # 结果与约束：管理服务与个人本体互不隐式启用，构造或读取不会提交注册。
    @property
    def enabled(self):
        return self.settings.system_atr_enabled

    # 职责：发布管理系统唯一身份已确认的正式 ACS。
    # 输入：框架返回的完整正式文档与真实 AIC。
    # 结果与约束：写回唯一 acs.json；个人分身本体覆盖该方法使用自己的目录和模板标识。
    def publish(self, document, aic):
        publish_system_acs(self.settings.data_dir, document, aic)

    # 职责：构造自动注册配置并检查本地前置条件。
    # 输入：只读取系统专用 registration.json 和 Settings 中的 Registry 凭据。
    # 结果与约束：为缺失的完整新目录初始化候选；已有文件不覆盖，不从 Keycloak 或普通用户账户取得注册凭据。
    def configuration(self):
        paths = system_identity_paths(self.settings.data_dir)
        if not paths.acs.exists() and not paths.registration.exists():
            initialize_system_identity(self.settings.data_dir, base_url=self.settings.oidc_public_url,
                                       aip_url=self.settings.system_aip_public_url)
        config = system_registration_config(self.settings.data_dir)
        config = replace(config, registry_credentials=RegistryServerCredentials(
            username=self.settings.registry_username, password=self.settings.registry_password))
        validate_system_registration(self.settings, config)
        return config

    # 职责：推进一个 ATR 步骤，并在释放写侧身份锁后尝试启动系统协议。
    # 输入：settings 和系统持久状态；使用 WIT 的 TRUSTED_REGISTRATION 公开服务，不依赖教程中的其他版本命名。
    # 结果与约束：只根据 RegistrationStatus.phase 判断成功；系统 active 后直接传递其真实 AIC，blocked/rejected 不新建身份、不自动更换目录。
    async def step(self):
        self.stage = 'registration'
        try:
            config = await run_blocking(self.configuration)
        except RegistrationConfigurationError:
            raise
        except (OSError, KeyError, ValueError):
            raise RegistrationConfigurationError('configuration_unavailable') from None
        # 已完成的系统身份只从本地装载；重启平台不是一次新的注册或 CA 健康查询。
        def active_state():
            path = config.paths.state_dir / 'atr-state.json'
            if config.mode in ('integrated', 'entity') and path.is_file():
                state = identity_document.identity_json(path)
                if state.get('phase') == 'active':
                    return state

        state = await run_blocking(active_state)
        if state:
            entity=await identity_document.LocalEntityDirectory({state['aic']:config}).select(state['aic'])
            self.phase,self.aic='active',entity.aic
            self.registration_id=state.get('registration_id')
            self.acs_fingerprint=state.get('fingerprint')
            self.code,self.reason,self.updated_at='active','',now()
            await self.activate(config)
            return self.phase
        await validate_ca_directory(config)
        async with blocking_context(wit_candidate_config(config)) as input_config:
            component = await run_blocking(registration_component, input_config)
            agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'entry': type(self).__name__})
            agent.install(component, required=True)
            try:
                async with agent:
                    registration = agent.context.get(TRUSTED_REGISTRATION)
                    result = await registration.up()
                    phase = str(result.phase)
                    if phase not in ('initial', 'pending_review', 'approved', 'active', 'blocked', 'rejected'):
                        raise RegistrationConfigurationError('invalid_registration_phase')
                    self.phase = phase
                    self.registration_id, self.aic = result.registration_id, result.aic
                    self.acs_fingerprint = result.acs_fingerprint
                    # 审核原因只对根管理员返回，并替换已知敏感凭据；异常响应正文从不进入状态。
                    self.reason = str(result.reason or '')
                    for secret in (self.settings.registry_password, self.settings.registry_username):
                        if secret:
                            self.reason = self.reason.replace(secret, '[已隐藏]')
                    self.code, self.updated_at = phase, now()
                    if phase == 'active':
                        if not result.aic or component.final_acs is None:
                            raise RegistrationConfigurationError('registered_identity_mismatch')
                        document = dict(component.final_acs)
            finally:
                await run_blocking(sync_component_acs, config, component, publisher=self.publish)
            if phase == 'active':
                await run_blocking(self.publish, document, result.aic)
        # AtrEntityDirectory 与 ATR 写组件共享文件锁，必须等上面的容器完成 teardown 后才接通监听。
        if self.phase == 'active':
            await self.activate(config)
        return self.phase

    # 职责：管理系统身份就绪后直接启动独立协议监听。
    # 输入：config 是该系统唯一身份刚完成推进的 integrated 配置。
    # 结果与约束：不注册或派生额外管理身份；个人能力本体覆盖此方法且不监听端口。
    async def activate(self, config):
        self.stage = 'ready'
        await self.aip.start(aic=self.aic)

    # 职责：循环推进注册，待审或临时失败每小时检查一次，管理员可唤醒立即检查。
    # 输入：使用可取消的 asyncio 任务、单次超时和管理员唤醒事件。
    # 结果与约束：blocked/rejected 停止；active 交接成功后结束，监听暂未就绪继续重试；HTTP 200 不代替状态，超时不清理持久状态、不调用 staff 审批。
    async def run(self):
        while True:
            self.wake.clear()
            try:
                async with asyncio.timeout(self.settings.system_atr_call_timeout):
                    phase = await self.step()
                if phase in ('blocked', 'rejected'):
                    return
                if phase == 'active':
                    if (self.aip is None or not self.settings.system_aip_enabled or self.aip.status()['serving']
                            or getattr(self.aip, 'agent', None) is not None):
                        return
                    self.code = 'aip_not_ready'
            except RegistrationConfigurationError as exc:
                self.phase, self.code, self.updated_at = 'blocked', exc.code, now()
                return
            except (KeyError, ValueError) as exc:
                self.phase, self.code, self.updated_at = 'blocked', 'configuration_unavailable', now()
                logger.warning('系统 ATR 配置不可用（%s）。', type(exc).__name__)
                return
            except Exception as exc:
                self.code, self.updated_at = 'remote_error', now()
                logger.warning('系统 ATR 将重试（%s）；持久状态保留。', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=3600)
            except TimeoutError:
                pass

    async def refresh(self):
        """Coalesce manual checks into the existing task without cancelling an ATR write."""
        if not self.enabled:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name=type(self).__name__ + '-registration')
        else:
            self.wake.set()

    # 职责：按显式开关启动独立的注册推进后台任务。
    # 输入：无参数，使用当前配置和唯一任务持有者。
    # 结果与约束：立即返回，不让 Registry 待审阻塞 Web 启动；重复 start 不创建第二个任务。
    async def start(self):
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self.run(), name=type(self).__name__ + '-registration')

    # 职责：取消并等待注册任务完全退出。
    # 输入：无参数，由 FastAPI lifespan 在关闭 AIP 和 Web 容器之前调用。
    # 结果与约束：等待 wit teardown 释放文件锁和网络资源；保留注册状态、正式身份、证书及候选，不把取消当成申请驳回。
    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    # 职责：提供根管理员可查看的状态投影和固定操作提示。
    # 输入：读取内存中的最近真实 RegistrationStatus，不主动请求 Registry。
    # 结果与约束：返回当前身份阶段、注册 ID、AIC 和指纹；不返回密码、Token、私钥或原始 ACS；关闭时提示尚未启用。
    def status(self):
        return {'enabled': self.enabled, 'running': bool(self.task and not self.task.done()),
                'phase': self.phase, 'registration_id': self.registration_id, 'aic': self.aic,
                'stage': self.stage, 'poll_interval_seconds': 3600,
                'acs_fingerprint': self.acs_fingerprint, 'reason': self.reason, 'code': self.code,
                'message': MESSAGES.get(self.code, MESSAGES['blocked']), 'updated_at': self.updated_at,
                'aip': self.aip.status() if self.aip is not None else None}
