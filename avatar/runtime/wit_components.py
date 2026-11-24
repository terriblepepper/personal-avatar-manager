# 模块说明：WIT 3.2 组件与共享服务契约；领域服务保持独立，由内核管理依赖激活和后台任务资源。
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from wit.components import Context, ServiceKey, WitComponent
from avatar.accounts.management import AccountManagement
from avatar.models.client import ModelClient, ModelResolver
from avatar.protocols.registration.base import RegistrationProvider, build_provider
from avatar.accounts.auth import bootstrap
from avatar.conversation.chat import AvatarChat
from avatar.core.config import Settings
from avatar.core.db import Database
from avatar.conversation.memory import MemoryPolicy
from avatar.models.model_config import SecretVault
from avatar.accounts.platform_binding import PlatformBindingService
from avatar.profiles.services import AvatarService
from avatar.runtime.worker import ProvisionWorker

# 服务名是本系统内部契约，不与正式 ACPS 身份、模型 Key 或浏览器可声明的身份混用。
DATABASE = ServiceKey[Database]('avatar.database')
SECRET_VAULT = ServiceKey[SecretVault]('avatar.secret-vault')
REGISTRATION = ServiceKey[RegistrationProvider]('avatar.registration')
MODEL_CLIENT = ServiceKey[ModelClient]('avatar.model-client')
AVATAR_SERVICE = ServiceKey[AvatarService]('avatar.profiles')
ACCOUNT_SERVICE = ServiceKey[AccountManagement]('avatar.accounts')
PLATFORM_BINDING = ServiceKey[PlatformBindingService]('avatar.platform-binding')
AVATAR_CHAT = ServiceKey[AvatarChat]('avatar.chat')
PROVISION_WORKER = ServiceKey[ProvisionWorker]('avatar.provision-worker')


# 职责：保存一个应用实例共用的领域服务，与 wit 激活上下文分离。
# 输入：数据库、密钥管理、注册器、模型客户端以及账户、资料、对话、任务服务。
# 结果与约束：同步 HTTP 管理接口和 wit Entry 使用同一实例；持久状态在数据库，生命周期资源交给组件。
@dataclass(frozen=True)
class ApplicationServices:
    database: Database
    vault: SecretVault
    provider: RegistrationProvider
    model_client: ModelClient
    profiles: AvatarService
    accounts: AccountManagement
    binding: PlatformBindingService
    chat: AvatarChat
    worker: ProvisionWorker

    # 职责：按配置初始化本地存储并组装可测试的服务图。
    # 输入：settings 为启动配置，provider、model_client 可注入已实现接口的隔离测试对象。
    # 结果与约束：返回共享服务对象；兼容初始化数据库与根账户，不启动任务、不发起正式注册或模型请求。
    @classmethod
    def create(cls, settings: Settings, provider=None, model_client=None):
        database = Database(settings.data_dir, settings.database_url)
        database.dispatch_enabled = bool(settings.redis_socket or settings.kafka_bootstrap)
        try:
            database.initialize()
            from avatar.core.user_storage import migrate_user_storage
            migrate_user_storage(database)
            bootstrap(database, settings.data_dir)
            provider = provider or build_provider(settings.provider, settings)
            vault = SecretVault(settings.data_dir, database)
            model_client = model_client or ModelClient(
                proxy=settings.model_proxy, resolver=ModelResolver(settings.model_dns_aliases))
            profiles = AvatarService(database, provider, vault)
            chat = AvatarChat(profiles, model_client)
            chat.memory.policy = MemoryPolicy(
                recent_messages=settings.memory_recent_messages)
            return cls(database, vault, provider, model_client, profiles,
                       AccountManagement(database), PlatformBindingService(database), chat,
                       ProvisionWorker(database, provider))
        except BaseException:
            database.close()
            raise


# 职责：向 wit 内核发布数据库、保险库、注册器与多平台模型网络适配器。
# 输入：services 为当前应用服务集合，四个基础服务由此组件唯一提供。
# 结果与约束：仅注册本地服务引用；不默认安装 ATR/AIP，模型 Key 仍按分身从保险库读取。
class InfrastructureComponent(WitComponent[None]):
    name = 'avatar-infrastructure'
    provides = (DATABASE, SECRET_VAULT, REGISTRATION, MODEL_CLIENT)

    # 职责：保存待发布的基础依赖。
    # 输入：services 为 ApplicationServices。
    # 结果与约束：构造不启动网络连接或后台任务。
    def __init__(self, services):
        self.services = services

    # 职责：在组件激活时发布所有声明的基础服务。
    # 输入：ctx 为 wit 管理的当前组件 Context。
    # 结果与约束：ctx.provide 与 provides 一一对应，由内核按组件生命周期清理绑定；模型格式与 SSRF 防护保持原实现。
    async def setup(self, ctx: Context):
        ctx.provide(DATABASE, self.services.database)
        ctx.provide(SECRET_VAULT, self.services.vault)
        ctx.provide(REGISTRATION, self.services.provider)
        ctx.provide(MODEL_CLIENT, self.services.model_client)


# 职责：在基础依赖就绪后激活账户、资料、平台绑定、对话与创建任务服务。
# 输入：通过 requires 显式声明基础依赖，发布领域能力 ServiceKey。
# 结果与约束：业务实现与框架解耦，HTTP 和 Entry 共用同一对象，避免重构导致双数据库或重复会话记忆。
class AvatarBusinessComponent(WitComponent[None]):
    name = 'avatar-business'
    requires = (DATABASE, SECRET_VAULT, REGISTRATION, MODEL_CLIENT)
    provides = (AVATAR_SERVICE, ACCOUNT_SERVICE, PLATFORM_BINDING, AVATAR_CHAT, PROVISION_WORKER)

    # 职责：保存应用共享的领域服务。
    # 输入：services 为 ApplicationServices。
    # 结果与约束：不在构造阶段提前运行服务任务，依赖激活由内核负责。
    def __init__(self, services):
        self.services = services

    # 职责：验证服务图的基础绑定并发布领域能力。
    # 输入：ctx 为已满足 requires 的组件上下文。
    # 结果与约束：若安装了与应用资源不同的基础依赖则启动失败；避免领域对象引用与实际发布依赖分叉。
    async def setup(self, ctx: Context):
        expected = ((DATABASE, self.services.database), (SECRET_VAULT, self.services.vault),
                    (REGISTRATION, self.services.provider), (MODEL_CLIENT, self.services.model_client))
        for key, value in expected:
            if ctx.get(key) is not value:
                raise RuntimeError('wit 基础服务与应用资源不一致')
        ctx.provide(AVATAR_SERVICE, self.services.profiles)
        ctx.provide(ACCOUNT_SERVICE, self.services.accounts)
        ctx.provide(PLATFORM_BINDING, self.services.binding)
        ctx.provide(AVATAR_CHAT, self.services.chat)
        ctx.provide(PROVISION_WORKER, self.services.worker)


# 职责：将持久化创建队列轮询绑定到 wit 组件的 Effect 生命周期。
# 输入：requires 声明 PROVISION_WORKER，enabled 控制是否启动轮询。
# 结果与约束：正式启动只创建一个协程，停止、卸载或依赖变化时取消并等待完成；隔离测试可禁用轮询。
class ProvisioningComponent(WitComponent[None]):
    name = 'avatar-provisioning'
    requires = (PROVISION_WORKER,)

    # 职责：设置是否启动自动创建任务。
    # 输入：enabled 为布尔开关，与应用 run_worker 对应。
    # 结果与约束：只保存开关；任务在 setup 中由 Effect 创建。
    def __init__(self, enabled):
        self.enabled = enabled
        self.task = None

    # 职责：在任务服务就绪后登记可自动清理的后台任务资源。
    # 输入：ctx 提供创建服务及资源所有权。
    # 结果与约束：disabled 时不运行恢复或轮询；enabled 时由 effect 确保清理回调只执行一次。
    async def setup(self, ctx: Context):
        worker = ctx.get(PROVISION_WORKER)
        if self.enabled:
            await ctx.effect(lambda: self._start_worker(worker), label='avatar-provision-worker')

    # 职责：启动持久化队列协程并返回对应的异步清理函数。
    # 输入：worker 为当前依赖激活提供的 ProvisionWorker。
    # 结果与约束：返回的回调由 wit Effect 接管；取消后等待任务退出，不仅调用 cancel 后遗留后台协程。
    def _start_worker(self, worker):
        self.task = asyncio.create_task(worker.run(), name='avatar-provision-worker')
        task = self.task

        # 职责：释放本次激活拥有的队列轮询任务。
        # 输入：闭包中的 task 为当前 Effect 创建的任务。
        # 结果与约束：等待取消完成，抑制预期 CancelledError，其他任务异常仍保留给框架清理诊断。
        async def close_worker():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        return close_worker
