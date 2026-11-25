"""Authenticated SSE invalidation hints; clients fetch their authorized snapshots."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from avatar.accounts.auth import current_user
from avatar.accounts.roles import ADMIN_ROLES
from avatar.core.dispatch import wait_change

router = APIRouter(prefix='/api', tags=['平台更新'])


@router.get('/updates')
async def updates(request: Request, user=Depends(current_user)):
    changes = request.app.state.changes
    if not changes.can_push:
        raise HTTPException(503, '实时更新暂不可用')
    uid = user['id']
    keys = [('inbox', uid), ('dialogue', uid)]
    if user['role'] in ADMIN_ROLES:
        keys.append(('reviews',))

    async def events():
        with changes.watch(*keys) as event:
            yield 'event: ready\ndata: {}\n\n'
            auth_check = asyncio.get_running_loop().time() + 25.
            while changes.can_push:
                await wait_change(event, max(0., auth_check - asyncio.get_running_loop().time()))
                if not changes.can_push:
                    break
                changed = event.take()
                if 'reviews' in changed or asyncio.get_running_loop().time() >= auth_check:
                    try:
                        actor = await asyncio.to_thread(current_user, request)
                    except HTTPException:
                        return
                    if actor['id'] != uid or actor['role'] != user['role']:
                        return
                    auth_check = asyncio.get_running_loop().time() + 25.
                if changed:
                    yield 'event: change\ndata: ' + json.dumps(sorted(changed)) + '\n\n'
                else:
                    yield ': keep-alive\n\n'

    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
