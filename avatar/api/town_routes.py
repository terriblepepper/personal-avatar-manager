"""小镇公开接口；浏览器登录与动画由小镇承担，服务端不创建访客账号。"""
import asyncio
import json
import secrets
from types import SimpleNamespace
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from avatar.accounts.auth import current_user
from avatar.core.text_budget import AgentText
from avatar.protocols.tasks.visitor_quota import TownAccessLimits, get_limits, save_limits
from avatar.protocols.tasks.town_files import NONCE
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.core.db import now

router = APIRouter(prefix='/api/town', tags=['小镇公开接入'])
account_router = APIRouter(prefix='/api/me', tags=['小镇访问额度'])


@account_router.get('/town-access-limits')
def own_access_limits(request: Request, user=Depends(current_user)):
    with request.app.state.database.connect() as db:
        return get_limits(db,user['id'])


@account_router.put('/town-access-limits')
def update_access_limits(body: TownAccessLimits, request: Request, user=Depends(current_user)):
    with request.app.state.database.connect(write=True,account_scope=user['id']) as db:
        save_limits(db,user['id'],body)
    return body


def service(request):
    return request.app.state.personal_aip.tasks


def target_row(request, target):
    with request.app.state.database.connect() as db:
        row = db.execute('''SELECT a.*,u.account_status FROM avatar_records a JOIN users u ON u.id=a.user_id
                            WHERE a.aic=? OR a.user_id=?''', (target, target)).fetchone()
    if not row:
        raise HTTPException(404, '公开分身不存在')
    return dict(row)


def context(request, aic='', visitor_session=''):
    return SimpleNamespace(peer_aic='town-http', local_aic=aic, group_id=None,
                           headers={**dict(request.headers), 'x-avatar-access': 'public','x-avatar-visitor-session':visitor_session})


def failure(exc):
    status = {-32008: 401, -32009: 403, -32001: 404, -32010: 429, -32602: 422, -32603: 502}.get(exc.code, 409)
    return HTTPException(status, {'code': exc.code, 'message': exc.message})


@router.get('/me/avatar')
async def own_avatar(request: Request):
    try:
        principal = await service(request).public_authorization.authorize(context(request))
    except ManagementTaskError as exc:
        raise failure(exc) from None
    if principal.get('anonymous'):raise HTTPException(401,'识别本人分身需要登录；访客可直接访问公开分身')
    return await asyncio.to_thread(bound_avatar,request.app.state.database,principal)


def bound_avatar(database,principal):
    with database.connect() as db:
        owners = db.execute('''SELECT user_id FROM oidc_identities WHERE issuer=? AND subject=?
          UNION SELECT user_id FROM oidc_identity_links WHERE issuer=? AND subject=?''',
          (principal['issuer'], principal['subject'], principal['issuer'], principal['subject'])).fetchall()
        if len(owners) != 1:
            raise HTTPException(404, '当前统一身份尚未绑定分身；未自动创建账户')
        row = db.execute('SELECT aic,identity_status FROM avatar_records WHERE user_id=?', (owners[0]['user_id'],)).fetchone()
    if not row:
        raise HTTPException(404, '当前账户尚未创建分身')
    return {'avatar_user_id': owners[0]['user_id'], 'avatar_aic': row['aic'], 'identity_status': row['identity_status']}


@router.get('/avatars/{target}')
def public_avatar(target: str, request: Request):
    row = target_row(request, target)
    available = row['account_status'] == 'active' and row['identity_status'] == 'active' and not row['aic'].startswith('DEMO-')
    runtime = request.app.state.personal_aip.status(row['user_id'])
    public = request.app.state.service.public_avatar(row['aic']) if available else {'aic': row['aic'], 'profile': {}}
    return {**public, 'public_id': row['user_id'], 'identity_status': row['identity_status'],
            'available': available, 'serving': available and runtime['serving'],
            'public_chat_enabled': request.app.state.settings.town_public_enabled and available,
            'endpoint': runtime['endpoint'] if available else None}


@router.get('/avatars/{target}/acs')
def completed_acs(target: str, request: Request):
    """直接实体返回经核验的 Registry ACS；旧实体仍明确标记本地补齐来源。"""
    row = target_row(request, target)
    if row['identity_status'] != 'active' or row['account_status'] != 'active' or row['aic'].startswith('DEMO-'):
        raise HTTPException(409, '正式分身当前不可用')
    document = json.loads(row['acs_document'])
    from wit.acps.identity.acs import validate_registry_acs, AcsContractError
    try:
        validate_registry_acs(document)
    except AcsContractError:
        raise HTTPException(503, '本地完整 ACS 尚未就绪') from None
    direct=document.get('entityMeta',{}).get('registrationMode')=='direct'
    return {'source':'registry-verified' if direct else 'local-validated-copy','registry_updated':direct,'acs':document}


class PublicTaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    task_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    session_id: str = Field(default='', max_length=128, pattern=r'^[A-Za-z0-9_-]*$')
    message: AgentText = ''
    operation: str = Field(default='start', pattern=r'^(start|continue|get|complete|cancel)$')
    question_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')

    @model_validator(mode='after')
    def validate_answer(self):
        if self.question_id and (self.operation!='continue' or not self.message.strip()):
            raise ValueError('回答分身提问时请使用 continue 并填写回答')
        return self


class TownFileResponse(StreamingResponse):
    def __init__(self, stream, item):
        self.stream = stream
        super().__init__(iter(lambda:stream.read(256*1024),b''),media_type=item['mimeType'],headers={
            'Content-Length':str(item['size']),
            'Content-Disposition':"attachment; filename*=UTF-8''"+quote(item['name'],safe=''),
            'Content-Security-Policy':"sandbox; default-src 'none'",
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope,receive,send)
        finally:
            self.stream.close()


@router.get('/files/{rowid}/{token}/{identifier}', include_in_schema=False)
def download_file(request: Request, rowid: int = Path(gt=0,le=2**63-1), token: str = Path(pattern='^'+NONCE+'$'),
                  identifier: str = Path(pattern=r'^[0-9a-f-]{36}$')):
    stream, item = service(request).download(rowid,token,identifier)
    return TownFileResponse(stream,item)


@router.post('/avatars/{target}/tasks')
async def public_task(target: str, body: PublicTaskRequest, request: Request, response: Response):
    row = await asyncio.to_thread(target_row,request,target)
    if not request.app.state.settings.town_public_enabled:
        raise HTTPException(403, '小镇公开访客对话尚未启用')
    visitor=request.cookies.get('avatar_visitor','')
    if not visitor or len(visitor)>128:
        visitor=secrets.token_urlsafe(32)
        response.set_cookie('avatar_visitor',visitor,httponly=True,samesite='lax',secure=request.url.scheme=='https')
    wire = {'type': 'task-command', 'id': body.command_id, 'sentAt': now(), 'senderRole': 'leader',
            'senderId': 'town-http', 'mentions': [row['aic']], 'command': body.operation,
            'taskId': body.task_id, 'sessionId': body.session_id,
            'dataItems': [{'type': 'text', 'text': body.message}] if body.operation in ('start', 'continue') else []}
    if body.question_id:
        wire['dataItems'].append({'type':'data','data':{'question_id':body.question_id}})
    try:
        return await service(request).handle(wire, context(request, row['aic'],visitor), row['user_id'], row['aic'])
    except ManagementTaskError as exc:
        raise failure(exc) from None
