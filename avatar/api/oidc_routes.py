# 模块说明：统一登录 HTTP 接口；浏览器绑定 Cookie 与短期状态隔离登录 CSRF，根管理员继续走原本地登录接口。
from typing import Literal
import asyncio
import re
import secrets
from urllib.parse import urlencode
from avatar.api.middleware import client_address
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from avatar.accounts.login_devices import device_context

router = APIRouter(prefix='/api/auth')


# 职责：限定登录发起参数，不接受调用者指定 issuer、角色、主体或任意跳转 URL。
# 输入：个人/管理返回入口及原平台临时令牌；不从登录页接收学生/教师身份。
# 结果与约束：未知字段拒绝；认证仅识别统一账号，身份类型在本人资料保存事务中处理。
class OidcStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    return_to: Literal['/', '/admin'] = '/'
    platform_entry_token: str | None = Field(default=None, min_length=32, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


# 职责：公开当前认证方式及固定客户端标识。
# 输入：request 获取当前 OIDC 服务实例。
# 结果与约束：不联网、不返回 Secret 或用户信息，供登录页决定普通账号与本地根管理员入口。
@router.get('/config')
def authentication_config(request: Request):
    return {**request.app.state.oidc.public_config(), 'version': request.app.version}


# 职责：发起统一认证并设置只供当前浏览器使用的短期凭据。
# 输入：经过校验的身份/入口参数，client_address(request) 用于限流。
# 结果与约束：返回固定提供者授权 URL；Cookie 为 HttpOnly、Lax、5 分钟且限定认证路径，支持跨站顶层回调。
@router.post('/oidc/start')
async def oidc_start(body: OidcStartRequest, request: Request, response: Response):
    url, browser = await request.app.state.oidc.start(body.return_to, body.platform_entry_token, client_address(request))
    response.set_cookie('avatar_oidc_browser', browser, max_age=300, httponly=True, samesite='lax',
                        secure=request.app.state.settings.secure_cookie, path='/api/auth/oidc')
    return {'authorization_url': url}


# 职责：接受 Keycloak 授权响应并建立本地业务会话。
# 输入：短期 state/code、绑定浏览器 Cookie、可选响应 issuer 或取消标记。
# 结果与约束：验证失败仅返回安全错误到本地登录页，不泄露令牌或远端正文；成功会话不长于 ID Token 有效期，回调禁止缓存和 Referrer。
@router.get('/oidc/callback')
async def oidc_callback(request: Request, state: str = Query(default='', max_length=128),
                        code: str = Query(default='', max_length=4096), iss: str | None = Query(default=None, max_length=500),
                        error: str | None = Query(default=None, max_length=100)):
    target = '/'
    try:
        if not request.app.state.settings.oidc_enabled:
            raise HTTPException(409, '统一认证尚未启用')
        browser = request.cookies.get('avatar_oidc_browser', '')
        if error or not code:
            await asyncio.to_thread(request.app.state.oidc.consume,state,browser)
            raise HTTPException(400, '统一认证已取消或未返回授权码，请重新登录')
        identifier=request.cookies.get('avatar_device','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',identifier):identifier=secrets.token_urlsafe(32)
        (token, _, _, duration), target = await request.app.state.oidc.finish(state, code, browser, iss, device_context(request,identifier))
        response = RedirectResponse(target, status_code=303)
        response.set_cookie('avatar_session', token, max_age=duration, httponly=True, samesite='strict',
                            secure=request.app.state.settings.secure_cookie)
        response.set_cookie('avatar_device',identifier,max_age=31536000,httponly=True,samesite='lax',secure=request.app.state.settings.secure_cookie)
    except HTTPException as exc:
        response = RedirectResponse('/?' + urlencode({'login_error': exc.detail}), status_code=303)
    response.delete_cookie('avatar_oidc_browser', path='/api/auth/oidc')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response
