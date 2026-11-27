# 模块说明：系统独立 AIP mTLS 服务的装配与生命周期；读取 wit 已注册身份，不自动注册、申请证书或调用分身。
import asyncio
import json
import logging
from urllib.parse import urlsplit
from avatar.core.execution import finish_on_cancel
from wit import WitAgent
from wit.acps.aip.direct import DIRECT_MTLS_SERVER
from avatar.protocols.identity.system import (
    system_identity_paths,
    system_aip_endpoint,
    private_directory,
    SYSTEM_ENTITY_ID,
)
from avatar.protocols.tasks.management import SystemManagementService
from avatar.runtime.system_partner import SystemPartnerHandler
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.transport.directory import CachedEntityDirectory,PassiveTlsListener
from avatar.protocols.identity.document import LocalEntityDirectory

logger = logging.getLogger(__name__)


# 职责：读取已存在的系统 ATR 配置，供监听器只读加载正式身份。
# 输入：data_dir 为业务数据根；注册配置相对路径按其自身目录解析。
# 结果与约束：不获取注册密码，不允许读取保存的业务服务 URL，不调用 Registry；AtrEntityDirectory 仅读取身份并核验 CA 状态。
def system_registration_config(data_dir):
    paths = system_identity_paths(data_dir)
    return read_atr_config(paths.registration)


# 职责：管理仅供小镇使用的系统 Partner 容器与原生 mTLS 监听。
# 输入：settings 提供开关、正式 AIC、监听地址及公开 URL，database 保留为兼容装配参数，当前入口回复不读取业务资料。
# 结果与约束：与 Web 问答容器隔离；没有证书或 CA 不可达时关闭协议入口，Web 可继续运行，不降级为匿名 HTTP。
class SystemAipRuntime:
    # 职责：准备协议组件持有者，不打开端口、不创建注册任务。
    # 输入：settings 为配置，database 为已初始化的应用业务存储。
    # 结果与约束：默认关闭，任务库仅在显式启用且身份校验成功后创建于 system_identity 下。
    def __init__(self, settings, database):
        self.settings = settings
        self.management = SystemManagementService(database, settings.oidc_public_url)
        self.agent = None
        self.listener = None
        self.reason = 'disabled'

    # 职责：校验端点配置与证书身份，启动框架原生 TLS 监听和持久任务容器。
    # 输入：无参数；仅 AVATAR_SYSTEM_AIP_ENABLED=1 时读取现有 ATR 身份。
    # 结果与约束：不发起注册或签发；任何配置/身份错误都关闭协议并记录异常类型，状态接口返回统一原因而不暴露凭据或文件内容。
    async def start(self, aic=None):
        if not self.settings.system_aip_enabled or self.agent is not None:
            return
        agent = None
        try:
            settings = self.settings
            endpoint = system_aip_endpoint(settings.system_aip_public_url)
            parsed = urlsplit(endpoint['url'])
            selected_aic = aic or settings.system_aip_aic
            if aic and settings.system_aip_aic and aic != settings.system_aip_aic:
                raise ValueError('自动注册身份与运维固定 AIC 不一致')
            if not selected_aic or selected_aic.startswith('DEMO-'):
                raise ValueError('需要已注册管理系统的正式 AIC')
            if not 1 <= settings.system_aip_port <= 65535:
                raise ValueError('监听端口无效')
            paths = system_identity_paths(settings.data_dir)
            config = system_registration_config(settings.data_dir)
            if config.mode != 'integrated':
                raise ValueError('管理系统必须使用唯一普通服务身份，不能使用本体或个人派生身份')
            candidate = json.loads(config.paths.acs_path.read_text())
            if candidate.get('entityUserId') != SYSTEM_ENTITY_ID:
                raise ValueError('个人身份不能作为管理系统身份')
            declared = candidate.get('endPoints', [])
            if endpoint not in declared:
                raise ValueError('ACS 与配置中的协议端点不一致')
            registrations = {selected_aic: config}
            directory = CachedEntityDirectory(registrations, LocalEntityDirectory(registrations), ttl=60)
            # 在创建任务库或监听器之前核验证书、ACS 与 CA 状态；失败不产生半启用状态。
            await directory.select(selected_aic)
            private_directory(paths.root)
            agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'entry': 'town-management'})
            listener = PassiveTlsListener(directory, local_aic=selected_aic,
                                                      bind_address=settings.system_aip_bind_address, bind_port=settings.system_aip_port,
                                                      max_connections=64, max_streams=1, request_timeout=15.0, max_body_bytes=65536)
            agent.install(SystemPartnerHandler(self.management, paths.root,
                                               admission_directory=directory,
                                               server_name=parsed.hostname, base_path='/aip', public_endpoint=endpoint['url'],
                                               listener=listener), required=True)
            await agent.start()
            self.agent, self.listener = agent, agent.context.get(DIRECT_MTLS_SERVER)
            self.reason = 'ready'
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            self.reason = 'stopped' if cancelled else 'identity_or_configuration_not_ready'
            if agent is not None:
                try:
                    await finish_on_cancel(agent.stop())
                except Exception as cleanup_error:
                    if cancelled:
                        raise exc from cleanup_error
                    raise
            if cancelled:
                raise
            logger.warning('系统 AIP 未启用（%s）；请核对正式身份、证书、CA 和端点配置。', type(exc).__name__)

    # 职责：关闭协议端口并等待所有活动调用及任务存储清理。
    # 输入：无参数，可在启动失败或应用退出时调用。
    # 结果与约束：幂等关闭，不删除持久任务或正式证书，不影响 Web 会话数据。
    async def stop(self):
        if self.agent is not None:
            await self.agent.stop()
            self.agent = None
            self.listener = None
        self.reason = 'stopped' if self.settings.system_aip_enabled else 'disabled'

    # 职责：返回可公开的协议就绪状态。
    # 输入：读取配置和框架 listener.serving，不通过配置开关推测服务可用。
    # 结果与约束：不返回证书、路径、Key、用户身份或异常正文；serving 反映监听器的实际 admission 状态。
    def status(self):
        serving = bool(self.listener is not None and self.listener.serving)
        return {'enabled': self.settings.system_aip_enabled, 'serving': serving,
                'endpoint': self.settings.system_aip_public_url, 'transport': 'JSONRPC',
                'security': 'mutualTLS', 'reason': 'ready' if serving else self.reason if self.reason != 'ready' else 'identity_unavailable',
                'avatar_answers': False, 'requester_identity_api_connected': False}
