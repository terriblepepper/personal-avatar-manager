"""小镇负责注册/登录流程；本模块仅校验请求令牌及目标访问权限，不引导登录或创建账户。"""
from fastapi import HTTPException
from avatar.accounts.oidc import OidcService
from avatar.protocols.transport.endpoints import entity_oidc_identity
from avatar.protocols.tasks.store import ManagementTaskError


# 职责：校验小镇通过 Authorization 转发的 access token，并绑定不可变 OIDC 主体。
# 调用固定 issuer 的 UserInfo；不信任明文用户名/请求体、不记录或持久化 Bearer token。
# 当前只开放本人分身；其他用户即使传入同名声明也不能访问该端点的私有资料。
class TownUserAuthorization:
    # 保存固定 issuer 与 OIDC 客户端；构造不联网、不创建本地登录会话。
    def __init__(self, database, settings):
        self.settings = settings
        self.oidc = OidcService(database, settings)

    # 在任务与模型调用前验证 Bearer；网络故障和认证失败分别返回，不输出 Token。
    async def authorize(self, context, user_id):
        headers = {k.lower(): v for k, v in getattr(context, 'headers', {}).items()}
        header = headers.get('authorization', '')
        if not isinstance(header, str) or not header.startswith('Bearer ') or not 1 <= len(header[7:]) <= 16384:
            raise ManagementTaskError(-32008, '缺少有效的 Keycloak access token')
        if not self.settings.oidc_enabled:
            raise ManagementTaskError(-32008, '统一认证尚未启用')
        try:
            configuration = await self.oidc.discovery()
            identity = await self.oidc.request_json('GET', configuration['userinfo_endpoint'], authentication=True,
                                                   headers={'Authorization': header})
        except HTTPException as exc:
            code = -32008 if exc.status_code == 401 else -32603
            message = 'Keycloak access token 无效或已过期' if code == -32008 else '统一认证服务暂不可用'
            raise ManagementTaskError(code, message) from None
        expected = entity_oidc_identity(self.settings, user_id)
        if identity.get('sub') != expected['keycloakSubject']:
            raise ManagementTaskError(-32009, '当前 Keycloak 账户无权访问此分身')
        return expected
