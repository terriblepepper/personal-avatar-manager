"""Read-only operational metrics restricted to complete local admin sessions."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from avatar.accounts import auth
from avatar.api.routes import password_session
from avatar.core.schemas import LoginRequest

router = APIRouter(prefix='/api/ops', tags=['运行监控'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def local_admin(user=Depends(auth.current_user)):
    auth.require_admin(user)
    if user.get('auth_method') != 'local' or user.get('access_scope') != 'full':
        raise HTTPException(403, '请使用本地管理员账号登录运行监控')
    return user


@router.post('/login')
def login(body: LoginRequest, request: Request, response: Response):
    _, csrf = password_session(body, request, response, admin_only=True)
    response.headers.update(PRIVATE)
    return {'csrf': csrf}


@router.get('/metrics')
async def metrics(request: Request, window: Literal['1h', '24h', '7d'] = '1h',
                  user=Depends(local_admin)):
    snapshot = await request.app.state.monitor.read(window)
    return JSONResponse({**snapshot, 'csrf': user['csrf']}, headers=PRIVATE)
