# 模块说明：分身注册契约与可恢复等待：Demo 与正式本体派生适配器可配置选择，演示 AIC 和证书不代表正式 ACPS 身份。
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol


# 职责：表示本体待审、证书暂不可用等可以恢复的注册等待状态。
# 输入：message 为应用固定提示，phase 是有限注册阶段。
# 结果与约束：Worker 延迟重试同一身份目录；不将等待当成功，也不删除远端注册状态。
class RegistrationPending(RuntimeError):
    # 职责：保存安全提示及下一次尝试的等待时间。
    # 输入：message 不得含远端正文或凭据，delay 以秒计。
    # 结果与约束：最少等待一秒，避免队列忙循环。
    def __init__(self, message, delay=30, *, phase="waiting"):
        self.phase = phase
        self.delay = max(1, delay)
        super().__init__(message)


# 职责：表示需要运维修复或对账后才能人工重试的注册失败。
# 输入：message 为固定中文提示，不能包含证书私钥或远端响应。
# 结果与约束：沿用原状态和 AIC，禁止以删除目录方式自动重新申请。
class RegistrationFailure(RuntimeError):
    def __init__(self,message,*,phase="failed"):
        self.phase=phase
        super().__init__(message)


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


# 职责：提供无需正式注册平台的演示身份实现。
# 输入：以稳定本地用户 ID 生成 DEMO AIC，证书仅为模拟元数据。
# 结果与约束：便于任务重放与更新验收，不表示已注册正式 AIC 或获得 ACPS 证书。
class DemoRegistrationProvider:
    mode = 'demo'

    # 职责：幂等生成演示分身或复用原分身身份。
    # 输入：user_id 为稳定账户，profile 为审核资料，previous 为已有分身对象。
    # 结果与约束：返回 AIC 与模拟证书；首次用 UUID5 生成稳定身份，更新沿用既有 AIC 和证书。
    async def provision(self, user_id, profile, previous):
        # 用户 ID 决定稳定身份，任务重放、资料更新及进程重启均不会产生新 AIC。
        aic = previous["aic"] if previous else "DEMO-" + uuid.uuid5(uuid.NAMESPACE_URL, "avatar:"+user_id).hex[:20].upper()
        certificate = previous["certificate"] if previous else self.renew({"id": "DEMO-CERT-" + aic[5:], "generation": 0})
        return {"aic": aic, "certificate": certificate}

    # 职责：更新模拟证书代次与到期时间。
    # 输入：certificate 须包含证书 ID 和 generation。
    # 结果与约束：返回代次加一、90 天后到期的演示证书元数据，并明确标注非 ACPS 证书；不调用 CA。
    def renew(self, certificate):
        if certificate.get('mode') != 'demo' and not certificate['id'].startswith('DEMO-CERT-'):
            from fastapi import HTTPException
            raise HTTPException(409, '正式证书请使用 wit 证书工具续期，不能生成演示证书覆盖')
        return {"id": certificate["id"], "generation": certificate["generation"]+1,
                "expires_at": (datetime.now(timezone.utc)+timedelta(days=90)).isoformat(),
                "issuer": "本地演示注册器", "mode": "demo", "status": "模拟有效（非 ACPS 证书）"}


# 职责：按启动配置选择已支持的注册适配器。
# 输入：name 为 demo 或 acps-direct；正式模式还需要 settings 提供服务器配置。
# 结果与约束：按显式开关返回 Demo 或派生适配器；未知模式拒绝启动，正式模式不会自动替换历史 Demo AIC。
def build_provider(name, settings=None):
    if name == 'demo':
        return DemoRegistrationProvider()
    if name == 'acps-direct' and settings is not None:
        from avatar.protocols.registration.direct import DirectRegistrationProvider
        return DirectRegistrationProvider(settings)
    raise RuntimeError('注册适配器只支持 demo 或 acps-direct；正式模式需要完整 Settings')
