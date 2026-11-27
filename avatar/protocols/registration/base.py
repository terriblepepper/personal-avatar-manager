# 模块说明：分身注册契约与可恢复等待：Demo 与正式本体派生适配器可配置选择，演示 AIC 和证书不代表正式 ACPS 身份。
from typing import Protocol

from avatar.protocols.registration.demo import DemoRegistrationProvider

from avatar.protocols.errors import RegistrationFailure, RegistrationPending


# 职责：约定分身创建、更新及证书续期的注册适配器接口。
# 输入：实际实现负责本地 Demo 或后续 Registry、CA 与 ACPS 平台操作。
# 结果与约束：分身 ACS/证书可按稳定 user_id 入库，系统自身身份不复用该账户记录；不确定写入须先对账，私钥由适配器保护，不进入普通业务/API 投影。
class RegistrationProvider(Protocol):
    """Create/update must be idempotent for the stable user + revision key.

    A real adapter must reconcile uncertain Registry writes before retrying.
    Public certificates and ACS may be stored per user in the business database.
    Private keys stay in adapter-controlled protected storage, never ordinary API projections.
    """
    # 职责：定义注册器创建或更新分身的异步契约。
    # 输入：user_id 为稳定本地身份，profile 为审核快照，previous 为可选原分身。
    # 结果与约束：实现返回含 aic 和 certificate 的字典；更新必须保留原身份，协议声明本身不执行操作。
    async def provision(self, user_id: str, profile: dict, previous: dict | None) -> dict: ...
    # 职责：定义按现有证书元数据续期的契约。
    # 输入：certificate 为注册器当前证书信息。
    # 结果与约束：实现返回更新证书信息；正式证书校验、凭据与远程调用由具体适配器负责。
    def renew(self, certificate: dict) -> dict: ...


# 职责：按启动配置选择已支持的注册适配器。
# 输入：name 为 demo 或 acps-direct；正式模式还需要 settings 提供服务器配置。
# 结果与约束：按显式开关返回 Demo 或派生适配器；未知模式拒绝启动，正式模式不会自动替换历史 Demo AIC。
def build_provider(name, settings, database):
    if name == 'demo':
        return DemoRegistrationProvider()
    if name == 'acps-direct' and settings is not None:
        # Demo providers can run without loading the live registry adapter.
        from avatar.protocols.registration.direct import DirectRegistrationProvider
        return DirectRegistrationProvider(settings, database)
    raise RuntimeError('注册适配器只支持 demo 或 acps-direct；正式模式需要完整 Settings')
