"""Read-only operational metrics for the root and super administrator roles."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from avatar.accounts import auth
from avatar.accounts.roles import can_view_ops
from avatar.api.routes import password_session
from avatar.accounts.schemas import LoginRequest

router = APIRouter(prefix='/api/ops', tags=['运行监控'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def ops_admin(user=Depends(auth.current_user)):
    if user['access_scope'] != 'full' or not can_view_ops(user):
        raise HTTPException(403, '运行监控仅向根管理员和超级管理员开放')
    return user


@router.post('/login')
def login(body: LoginRequest, request: Request, response: Response):
    _, csrf = password_session(body, request, response, ops_only=True)
    response.headers.update(PRIVATE)
    return {'csrf': csrf}


@router.get('/metrics')
async def metrics(request: Request, window: Literal['1h', '24h', '7d'] = '1h',
                  user=Depends(ops_admin)):
    snapshot = await request.app.state.monitor.read(window)
    return JSONResponse({**snapshot, 'csrf': user['csrf']}, headers=PRIVATE)


@router.get('/metrics/minutes')
async def minutes(request: Request, start: int = Query(ge=0), count: int = Query(ge=1, le=60),
                  user=Depends(ops_admin)):
    series = request.app.state.monitor.read_minutes(start, count)
    return JSONResponse({'series': series}, headers=PRIVATE)
