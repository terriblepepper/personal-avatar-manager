# 模块说明：WIT 系统 Direct Partner；监听组件负责 mTLS，框架校验协议，应用保存任务状态，绝不转入 Web 问答。
from avatar.core.execution import run_blocking
from wit.acps.aip.direct import BaseDirectPartnerComponent, DirectRpcError
from wit.acps.identity import current_local_entity
from avatar.protocols.tasks.store import SystemTaskStore, ManagementTaskError
from avatar.protocols.transport.diagnostics import RpcDiagnostics


# 职责：按新版 Direct Partner 契约接收真实传输认证后的管理命令。
# 输入：AipContext 携带 TLS peer 和已接纳本地身份，command 是框架验证的 TaskCommand。
# 结果与约束：目录查询直接返回公开结果，其他管理请求返回 Web URL；持久任务状态支持 get/complete/cancel，不调用模型。
class SystemPartnerHandler(BaseDirectPartnerComponent):
    name = 'system-management-partner'

    # 职责：注入管理服务、专用任务文件与原生监听配置。
    # 输入：management 是只读业务，identity_dir 是系统身份目录，kwargs 是框架公开端点参数。
    # 结果与约束：基类负责端点和 listener 的组合，不再安装已移除的旧 Handler/Endpoint/TaskRuntime 组件。
    def __init__(self, management, identity_dir, admission_directory=None, **kwargs):
        super().__init__(**kwargs)
        self.management = management
        self.admission_directory=admission_directory
        self.tasks = SystemTaskStore(management.database)
        self.diagnostics_path = identity_dir / 'rpc-diagnostics.jsonl'
        self.diagnostics = None

    async def _teardown(self,ctx):
        if self.diagnostics is not None:
            await run_blocking(self.diagnostics.close)
        await super()._teardown(ctx)

    # 职责：在 Partner 激活时初始化系统专用任务存储。
    # 输入：ctx 是框架激活上下文。
    # 结果与约束：初始化 PostgreSQL 回执表。
    async def _setup(self, ctx):
        await run_blocking(self.tasks.initialize)
        self.diagnostics = await run_blocking(RpcDiagnostics, self.diagnostics_path)

    # 职责：执行可信 AipContext 约束的完整管理请求。
    # 输入：command 与 keyword-only context 遵循 WIT Direct 的 _handle 契约。
    # 结果与约束：要求当前实体、senderId 和 TLS peer 一致；域错误转为固定 JSON-RPC 错误，返回 TaskResult 字典。
    async def _handle(self, command, *, context):
        entity = current_local_entity()
        if entity is None or entity.aic != context.local_aic or command.get('senderId') != context.peer_aic:
            raise DirectRpcError({'code': -32003, 'message': '系统管理 RPC 必须通过真实 mTLS 连接访问'})
        if self.admission_directory is not None:
            try:
                if await self.admission_directory.select(entity.aic) != entity:
                    raise ValueError('本地身份已在接入后更新')
            except Exception:raise DirectRpcError({'code':-32003,'message':'当前证书状态未能核验，请稍后重试'}) from None
        try:
            await run_blocking(self.diagnostics.write, event='request',peer_aic=context.peer_aic,request=command,header_names=list(context.headers))
            result = await run_blocking(self.tasks.handle, command, context, self.management)
            await run_blocking(self.diagnostics.write, event='response',peer_aic=context.peer_aic,response=result)
            return result
        except ManagementTaskError as exc:
            await run_blocking(self.diagnostics.write, event='error',peer_aic=context.peer_aic,code=exc.code,message=exc.message)
            raise DirectRpcError({'code': exc.code, 'message': exc.message}) from None
