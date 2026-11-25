# 模块说明：WIT 3.2 Agent Entry 和运行时；所有 Web 问答经统一调用边界执行，权限仍由认证层和领域服务校验。
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from fastapi import HTTPException
from wit import WitAgent
from wit.agents import BaseAgentEntryComponent, InvocationEvent, InvocationRequest, InvocationResult
from avatar.conversation.schemas import ChatRequest
from avatar.runtime.wit_components import AVATAR_CHAT, ApplicationServices, AvatarBusinessComponent, InfrastructureComponent, ProvisioningComponent


# 职责：承载认证层已经校验的本次问答输入。
# 输入：user 为服务端当前用户，body 为已通过 Pydantic 校验的 ChatRequest。
# 结果与约束：仅用于已认证 Web 问答，不供小镇管理协议转发分身提问；身份不放入浏览器可声明的 metadata，字段禁止 repr 输出。
@dataclass(frozen=True)
class AvatarInvocation:
    user: dict = field(repr=False)
    body: ChatRequest = field(repr=False)


# 职责：以 wit Agent Entry 契约执行完整回答与流式回答。
# 输入：依赖 AVATAR_CHAT 服务，InvocationRequest.input 必须为服务端构造的 AvatarInvocation。
# 结果与约束：成功只产生一个 final，失败抛异常而不伪装成功；沿用分身计费、隐私过滤、会话隔离与记忆策略。
class AvatarEntryComponent(BaseAgentEntryComponent):
    name = 'avatar-entry'
    requires = (AVATAR_CHAT,)

    # 职责：在框架入口激活时获取当前对话服务。
    # 输入：ctx 为 wit Entry 的激活上下文。
    # 结果与约束：使用基类 setup_entry 扩展点，保留框架内置注册、取消与活动调用清理机制。
    async def setup_entry(self, ctx):
        self.chat = ctx.get(AVATAR_CHAT)

    # 职责：实现一次受管理调用的具体执行流。
    # 输入：invocation 提供业务输入 及取消状态，身份不从 metadata 读取。
    # 结果与约束：转发可见事件；error 作为 HTTPException 传播，done 转为唯一 terminal。
    async def _stream(self, invocation):
        invocation.ensure_active()
        call = invocation.request.input
        if not isinstance(call, AvatarInvocation) or not isinstance(call.body, ChatRequest):
            raise HTTPException(422, '智能体入口需要已认证的问答请求')
        if call.user.get('access_scope', 'full') != 'full':
            raise HTTPException(403, '临时入口仅允许个人资料管理')
        # 显式关闭领域生成器，取消时继续释放模型连接与数据库会话租约。
        async with aclosing(self.chat.stream(call.user, call.body)) as source:
            async for event in source:
                invocation.ensure_active()
                if event['type'] == 'error':
                    raise HTTPException(event['code'], event['message'])
                if event['type'] in ('done','awaiting_input'):
                    yield InvocationEvent.final(InvocationResult(output=event))
                    return
                yield InvocationEvent(event['type'], data=event)
        raise HTTPException(502, '智能体回答流未返回完成结果')


# 职责：组装并管理应用级 wit 容器，为 HTTP 层提供稳定问答门面。
# 输入：启动配置及可注入注册器、模型客户端；每个应用拥有独立服务集合与组件图。
# 结果与约束：正式服务由 lifespan 启停一次；无 lifespan 的嵌入调用使用临时容器且禁用轮询，不绕过 Entry。
class AvatarRuntime:
    # 职责：创建本地服务集合，准备尚未启动的 wit 运行时。
    # 输入：settings、provider、run_worker、model_client 与 create_app 的注入参数一致。
    # 结果与约束：初始化持久化数据但不启动任务，不安装正式 ATR/AIP；服务重构不覆盖原账户与分身。
    def __init__(self, settings, provider=None, run_worker=True, model_client=None):
        self.services = ApplicationServices.create(settings, provider, model_client)
        self.run_worker = run_worker
        self.agent = None
        self.handles = ()
        self.closed = False

    # 职责：安装当前应用完整的必要组件依赖图。
    # 输入：run_worker 决定是否启用创建队列，资源与领域服务使用当前共享集合。
    # 结果与约束：返回 WitAgent 与组件句柄；先安装入口也可由内核按 requires 激活，required=True 保证缺失能力时启动失败。
    def _build_agent(self, run_worker):
        agent = WitAgent(metadata={'application': 'personal-avatar-manager', 'mode': 'demo'})
        components = (AvatarEntryComponent(), ProvisioningComponent(run_worker),
                      AvatarBusinessComponent(self.services), InfrastructureComponent(self.services))
        handles = tuple(agent.install(component, required=True) for component in components)
        return agent, handles

    # 职责：启动生产生命周期中的 wit 容器。
    # 输入：无参数，使用构造时的 run_worker 配置。
    # 结果与约束：等待所有 required 组件就绪后才发布 agent；容器停止为终态，禁止原运行时再次启动。
    async def start(self):
        if self.closed:
            raise RuntimeError('wit 运行时已关闭，请创建新实例')
        if self.agent is not None:
            return
        agent, handles = self._build_agent(self.run_worker)
        await agent.start()
        self.agent, self.handles = agent, handles

    # 职责：关闭入口活动调用、组件绑定和后台队列资源。
    # 输入：无参数，可在应用退出或测试结束时调用。
    # 结果与约束：wit 按依赖顺序清理并等待 Effect；关闭后业务门面拒绝新请求，数据库历史保持不变。
    async def stop(self):
        self.closed = True
        if self.agent is not None:
            await self.agent.stop()

    # 职责：为生产或无生命周期的嵌入调用选择合法容器。
    # 输入：无参数，读取 runtime 的启动和关闭状态。
    # 结果与约束：生产复用已启动容器；单次嵌入请求临时启动并自动关闭，不缓存跨事件循环对象或重复启动轮询任务。
    @asynccontextmanager
    async def _execution_agent(self):
        if self.closed:
            raise HTTPException(503, '智能体运行时已停止')
        if self.agent is not None:
            yield self.agent
        else:
            agent, _ = self._build_agent(False)
            async with agent:
                yield agent

    # 职责：将已认证的 Web 请求转换成 wit 调用契约。
    # 输入：user 为 current_user 返回值，body 为 ChatRequest。
    # 结果与约束：仅复制对话需要的账户字段，不携带 Cookie、CSRF、登录联系方式或 Key；metadata 仅用于观测，不作为认证依据。
    @staticmethod
    def request(user, body):
        identity = {key: user[key] for key in ('id', 'username', 'account_status', 'access_scope') if key in user}
        return InvocationRequest(input=AvatarInvocation(identity, body),
                                 metadata={'channel': body.channel})

    # 职责：将 wit InvocationEvent 映射回现有 Web SSE 事件。
    # 输入：user、body 为已经认证的问答参数。
    # 结果与约束：增量事件原样保留，final 恢复 done；HTTPException 映射为 error，取消不吞掉，aclosing 保证释放活动调用。
    async def stream(self, user, body):
        try:
            async with self._execution_agent() as agent:
                async with aclosing(agent.stream(self.request(user, body))) as source:
                    async for event in source:
                        if event.is_terminal:
                            yield event.result.output
                        else:
                            yield event.data
        except HTTPException as exc:
            yield {'type': 'error', 'code': exc.status_code, 'message': exc.detail}
