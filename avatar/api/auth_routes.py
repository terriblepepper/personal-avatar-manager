import hashlib
import json
import asyncio
from avatar.api.middleware import client_address
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import HTTPException
from typing import Literal
import avatar.accounts.auth as auth
from avatar.accounts.schemas import LoginRequest
from avatar.accounts.schemas import RecoveryRequest
from avatar.accounts.schemas import PasswordChangeRequest
from avatar.accounts.schemas import ContactChangeRequest
from avatar.accounts.schemas import RegistrationRequest
from avatar.accounts.schemas import PlatformEntryRequest
from urllib.parse import urlencode


router = APIRouter()


# 职责：注册本地账户，按有效平台入口可同时绑定并登录。
# 输入：body 为注册请求，request 提供数据库与配置，response 用于写 Cookie。
# 结果与约束：普通注册只返回提示；带入口注册成功后设置 30 分钟 profile Cookie 并返回受限账户及 CSRF。
@router.post("/auth/register", status_code=201)
def register(body: RegistrationRequest, request: Request, response: Response):
    if request.app.state.settings.oidc_enabled:
        raise HTTPException(403, '请通过 Keycloak 注册并登录')
    user_id=auth.create_user(request.app.state.database, body)
    if body.platform_entry_token:
        with request.app.state.database.connect(write=True, account_scope=user_id) as db:
            token,csrf,user=auth.issue_session(db,user_id,1800,'profile')
        response.set_cookie('avatar_session',token,httponly=True,samesite='strict',secure=request.app.state.settings.secure_cookie,max_age=1800)
        return {'message':'注册并绑定成功','user':user,'csrf':csrf}
    return {"message": "注册成功，请登录"}


# 职责：预览临时入口对应的绑定状态。
# 输入：body.token 为随机入口令牌，request 提供数据库。
# 结果与约束：返回服务校验后的 profile/register 模式，不消费令牌、不签发链接、不接受账号声明。
@router.post('/platform/entry/preview')
def platform_preview(body: PlatformEntryRequest, request: Request):
    return request.app.state.runtime.services.binding.preview(body.token)


# 职责：将已绑定账号的一次性入口兑换为资料会话。
# 输入：body.token 为已签发的有效入口，response 用于设置 Cookie。
# 结果与约束：消费令牌并设置 HttpOnly、SameSite Cookie，返回 30 分钟受限用户及 CSRF，不授予原账户管理权限。
@router.post('/platform/entry/session')
def platform_session(body: PlatformEntryRequest, request: Request, response: Response):
    token,csrf,user=request.app.state.runtime.services.binding.redeem(body.token)
    response.set_cookie('avatar_session',token,httponly=True,samesite='strict',secure=request.app.state.settings.secure_cookie,max_age=1800)
    return {'user':user,'csrf':csrf}


# 职责：将入口对应平台身份绑定到当前本地账户。
# 输入：body.token 证明平台核验结果，user 由会话依赖及 CSRF 校验得到。
# 结果与约束：要求完整本地登录会话，一对一冲突拒绝，返回绑定提示，不变更原角色。
@router.post('/platform/bind')
def platform_bind(body: PlatformEntryRequest, request: Request, user=Depends(auth.current_user)):
    return request.app.state.runtime.services.binding.bind_existing(user,body.token)


# 职责：读取当前账户自身的平台绑定信息。
# 输入：request 提供数据库，user 为认证账户。
# 结果与约束：返回绑定记录列表，不允许指定其他用户 ID 查询。
@router.get('/platform/binding')
def platform_binding(request: Request, user=Depends(auth.current_user)):
    return request.app.state.runtime.services.binding.bindings(user['id'])


# 职责：验证登录标识和密码并设置完整会话 Cookie。
# 输入：body 为 LoginRequest，client_address(request) 用于限流，response 写 Cookie。
# 结果与约束：成功返回账户和 CSRF，Cookie 有效 12 小时且 HttpOnly；失败与限流由认证服务处理。
def password_session(body, request, response, *, ops_only=False):
    settings = request.app.state.settings
    token, csrf, user = auth.login(request.app.state.database, body, client_address(request),
                                  root_only=settings.oidc_enabled, ops_only=ops_only)
    response.set_cookie("avatar_session", token, httponly=True, samesite="strict",
                        secure=settings.secure_cookie, max_age=43200)
    return user, csrf


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    user, csrf = password_session(body, request, response)
    return {"user": user, "csrf": csrf}


# 职责：返回当前会话的账户信息与 CSRF 标记。
# 输入：user 来自 current_user 权限依赖。
# 结果与约束：账户字典剔除内部 csrf 字段后单独返回 csrf；profile 会话使用受限角色呈现。
@router.get("/auth/me")
def me(request: Request, user=Depends(auth.current_user)):
    with request.app.state.database.connect() as db:
        row = db.execute("SELECT draft FROM profile_records WHERE user_id=?", (user["id"],)).fetchone()
    public = {k: v for k, v in user.items() if k != "csrf"}
    public["profile_name"] = json.loads(row[0]).get("name", "") if row else ""
    return {"user": public, "csrf": user["csrf"]}


@router.post("/me/onboarding/{stage}")
def advance_onboarding(stage: Literal['tour', 'done'], request: Request, user=Depends(auth.current_user)):
    with request.app.state.database.connect(write=True, account_scope=user['id']) as db:
        db.execute("""UPDATE users SET onboarding_stage=? WHERE id=? AND role='user'
                      AND (onboarding_stage='welcome' OR (onboarding_stage='tour' AND ?='done'))""",
                   (stage, user['id'], stage))
        row = db.execute("SELECT onboarding_stage,role FROM users WHERE id=?", (user['id'],)).fetchone()
    return {"stage": row['onboarding_stage'] if row['role'] == 'user' else 'done'}


# 职责：撤销当前服务端会话并清除浏览器 Cookie。
# 输入：user 依赖完成登录与 CSRF 校验，request 提供原 Cookie，response 删除 Cookie。
# 结果与约束：按 Cookie 哈希删除当前会话，不注销其他会话，返回退出提示。
@router.post("/auth/logout")
async def logout(request: Request, response: Response, user=Depends(auth.current_user)):
    def revoke_session():
        with request.app.state.database.connect(write=True, account_scope=user['id']) as db:
            db.execute("DELETE FROM sessions WHERE token=? AND user_id=?", (hashlib.sha256(request.cookies["avatar_session"].encode()).hexdigest(), user["id"]))
    await asyncio.to_thread(revoke_session)
    response.delete_cookie("avatar_session")
    result = {'message': '已退出'}
    if user.get('auth_source') == 'oidc':
        settings = request.app.state.settings
        try:
            endpoint = (await request.app.state.oidc.discovery()).get('end_session_endpoint')
        except HTTPException:
            endpoint = None
        if endpoint:
            result['logout_url'] = endpoint + '?' + urlencode({'client_id': settings.oidc_client_id, 'post_logout_redirect_uri': settings.oidc_public_url + '/'})
    return result


# 职责：提供按注册邮箱或手机号匹配的 Demo 找回接口。
# 输入：body 为规范化联系人与新密码，client_address(request) 用于限流。
# 结果与约束：无需旧会话，统一返回匹配提示；身份强度受 Demo 简化要求限制，实际重置和撤销由认证服务完成。
@router.post("/auth/recover-password")
def recover_password(body: RecoveryRequest, request: Request):
    if request.app.state.settings.oidc_enabled:
        raise HTTPException(403, '请在 Keycloak 账户中心找回登录凭据；根管理员由本地运维恢复')
    return auth.recover_password(request.app.state.database, body, client_address(request))


# 职责：修改登录账户密码并清除当前 Cookie。
# 输入：body 包含旧密码和新密码，user 通过完整登录与 CSRF 检查，response 删除 Cookie。
# 结果与约束：认证服务撤销全部会话与未使用入口，返回须重新登录的提示。
@router.post("/auth/change-password")
def change_password(body: PasswordChangeRequest, request: Request, response: Response, user=Depends(auth.current_user)):
    result = auth.change_password(request.app.state.database, user, body)
    response.delete_cookie("avatar_session")
    return result


# 职责：修改本人登录邮箱和可选手机号。
# 输入：body 含当前密码和新联系方式，user 为已认证账户。
# 结果与约束：服务验证旧密码与唯一性后更新，不覆盖公开资料联系方式。
@router.put("/auth/contacts")
def change_contacts(body: ContactChangeRequest, request: Request, user=Depends(auth.current_user)):
    return auth.change_contacts(request.app.state.database, user, body)
