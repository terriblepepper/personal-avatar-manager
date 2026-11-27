"""Offline registration provider for demo identities."""
import uuid
from datetime import datetime, timedelta, timezone


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
