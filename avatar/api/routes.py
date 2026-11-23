# 模块说明：HTTP 路由：认证依赖确定调用者，接口模型校验参数，再调用账户、绑定与分身服务。
import hashlib
import json
import asyncio
from avatar.protocols.identity.acs import AcsPreviewRequest
from fastapi import APIRouter, Body, Depends, Request, Response, Query, HTTPException
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from wit import __version__ as wit_version
from fastapi.responses import FileResponse
from avatar.files.library.folders import MaterialFolders,FolderRequest,NameRequest,FolderRenameRequest,rename_folder,rename_file
from avatar.files.parsing.animation import MAX_SOURCE_BYTES
from avatar.files.uploads.batches import batch_id as upload_batch_id, UploadComplete
from avatar.profiles.profile_dates import ModifiedAt
from avatar.models.model_settings import ModelSettingsRequest
from avatar.files.library.attachments import ARTIFACT_SUFFIXES, AttachmentDescriptionRequest
import avatar.accounts.auth as auth
from avatar.core.schemas import Credentials, LoginRequest, RecoveryRequest, PasswordChangeRequest, ContactChangeRequest, RegistrationRequest, DraftRequest, SubmitRequest, ReviewRequest, ChatRequest, PlatformSettings
from avatar.models.model_config import catalog
from avatar.core.schemas import ConversationRequest, ConversationTitleRequest, ModelConfigInput
from avatar.core.schemas import AccountRoleRequest
from avatar.core.schemas import PlatformEntryRequest

router = APIRouter(prefix="/api")


# 职责：提供管理路由共用的权限依赖。
# 输入：user 由 auth.current_user 注入，已完成会话与 CSRF 校验。
# 结果与约束：检查 admin 角色并返回用户；临时资料会话不允许进入管理路由。
def admin(user=Depends(auth.current_user)):
    auth.require_admin(user)
    return user


# 职责：检查服务是否能访问数据库。
# 输入：request 提供应用数据库依赖。
# 结果与约束：执行 SELECT 1 后返回 ok、实际运行模式和 wit 版本，不检查外部模型或正式平台状态。
@router.get("/health")
def health(request: Request):
    with request.app.state.database.connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "mode": request.app.state.settings.provider, "framework": "wit", "framework_version": wit_version}

@router.get('/help/{document}')
def help_document(document:Literal['用户操作手册.md','知识库与soul.md','资料RAG与嵌入.md'],user=Depends(auth.current_user)):
    """仅提供固定的用户帮助文档，不公开目录或接受任意文件路径。"""
    from avatar.core.config import ROOT
    return FileResponse(ROOT/'docs'/document,media_type='text/markdown; charset=utf-8',filename=document)



# 职责：注册本地账户，按有效平台入口可同时绑定并登录。
# 输入：body 为注册请求，request 提供数据库与配置，response 用于写 Cookie。
# 结果与约束：普通注册只返回提示；带入口注册成功后设置 30 分钟 profile Cookie 并返回受限账户及 CSRF。
@router.post("/auth/register", status_code=201)
def register(body: RegistrationRequest, request: Request, response: Response):
    if request.app.state.settings.oidc_enabled:
        from fastapi import HTTPException
        raise HTTPException(403, '请通过 Keycloak 注册并登录')
    user_id=auth.create_user(request.app.state.database, body)
    if body.platform_entry_token:
        with request.app.state.database.connect(write=True) as db:
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
# 输入：body 为 LoginRequest，request.client.host 用于限流，response 写 Cookie。
# 结果与约束：成功返回账户和 CSRF，Cookie 有效 12 小时且 HttpOnly；失败与限流由认证服务处理。
@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    token, csrf, user = auth.login(request.app.state.database, body, request.client.host, root_only=request.app.state.settings.oidc_enabled)
    response.set_cookie("avatar_session", token, httponly=True, samesite="strict", secure=request.app.state.settings.secure_cookie, max_age=43200)
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
    with request.app.state.database.connect(write=True) as db:
        db.execute("""UPDATE users SET onboarding_stage=? WHERE id=? AND role='user'
                      AND (onboarding_stage='welcome' OR (onboarding_stage='tour' AND ?='done'))""",
                   (stage, user['id'], stage))
        row = db.execute("SELECT onboarding_stage,role FROM users WHERE id=?", (user['id'],)).fetchone()
    return {"stage": row['onboarding_stage'] if row['role'] == 'user' else 'done'}


# 职责：提供当前用户可使用的资料字段目录。
# 输入：user 仅用于保证已登录及临时会话范围允许。
# 结果与约束：返回字段名称、控件、身份范围和默认隐私定义，不返回个人数据。
@router.get("/profile-fields")
def profile_fields(user=Depends(auth.current_user)):
    from avatar.profiles.profile_fields import catalog
    return catalog()


# 职责：提供按 AIC 访问的分身公开档案。
# 输入：aic 为路径目标，request 提供业务服务。
# 结果与约束：无需登录但仅返回经过隐私过滤的已生效资料，不包含聊天历史或模型 Key。
@router.get("/avatars/{aic}/public")
def public_avatar(aic: str, request: Request):
    return request.app.state.service.public_avatar(aic)


# 职责：撤销当前服务端会话并清除浏览器 Cookie。
# 输入：user 依赖完成登录与 CSRF 校验，request 提供原 Cookie，response 删除 Cookie。
# 结果与约束：按 Cookie 哈希删除当前会话，不注销其他会话，返回退出提示。
@router.post("/auth/logout")
async def logout(request: Request, response: Response, user=Depends(auth.current_user)):
    def revoke_session():
        with request.app.state.database.connect(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(request.cookies["avatar_session"].encode()).hexdigest(),))
    await asyncio.to_thread(revoke_session)
    response.delete_cookie("avatar_session")
    result = {'message': '已退出'}
    if user.get('auth_source') == 'oidc':
        from urllib.parse import urlencode
        settings = request.app.state.settings
        try:
            endpoint = (await request.app.state.oidc.discovery()).get('end_session_endpoint')
        except HTTPException:
            endpoint = None
        if endpoint:
            result['logout_url'] = endpoint + '?' + urlencode({'client_id': settings.oidc_client_id, 'post_logout_redirect_uri': settings.oidc_public_url + '/'})
    return result


# 职责：提供当前账户个人中心业务概览。
# 输入：user 提供本人 ID，request 提供 AvatarService。
# 结果与约束：仅查询本人资料、申请、分身与用量，并由服务脱敏配置。
@router.get("/me")
def overview(request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.overview(user["id"], access_scope=user["access_scope"])


# 职责：返回模型平台及模型建议列表。
# 输入：user 作为已登录依赖，不需要调用上游。
# 结果与约束：返回目录默认值与自定义平台选项，不承诺当前账户具有全部模型权限。
@router.get("/model-providers")
def model_providers(user=Depends(auth.current_user)):
    return catalog()


@router.get("/model-providers/effort-levels")
def model_effort_levels(provider: str, api_format: Literal['openai','anthropic','gemini'],
                        model: str = Query(max_length=160), user=Depends(auth.current_user)):
    from avatar.models.effort import levels
    return levels({'provider': provider, 'api_format': api_format, 'model': model})


# 职责：保存当前账户的个人资料草稿。
# 输入：body 含 Profile、修改日期和旧客户端可选模型配置，user 经过会话与 CSRF 检查。
# 结果与约束：委托服务校验身份、选填学号、状态和修改日期后保存；独立文件及模型设置沿用各自入口。
@router.put("/me/profile")
def profile(body: DraftRequest, request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.save_draft(user, body)


# 职责：提交当前账户指定修改日期的资料进行审核。
# 输入：body.last_modified_at 为读取的草稿修改日期，user 为本人账户。
# 结果与约束：委托业务服务创建审核快照并返回概览；审核通过后进入分身创建。
@router.post("/me/submit")
def submit(body: SubmitRequest, request: Request, user=Depends(auth.current_user)):
    result = request.app.state.service.submit(user, body.last_modified_at)
    request.app.state.pending_reviews.invalidate()
    return result


# 职责：获取本人指定频道与会话的聊天历史。
# 输入：channel 默认 assistant，conversation_id 可选，user 为当前账户。
# 结果与约束：先通过会话归属校验，再返回最近历史；内部子会话不能被冒用为本人 Web 会话。
@router.get("/chat")
def messages(request: Request, channel: str = "assistant", conversation_id: str | None = None, personal: bool=False,before:int|None=Query(default=None,ge=0,le=2**63-1),limit:int=Query(default=100,ge=1,le=100), user=Depends(auth.current_user)):
    return request.app.state.chat.history(user["id"], channel, conversation_id,personal=personal,before=before,limit=limit)


# 职责：列出本人频道会话及当前目标的记忆状态。
# 输入：channel 默认为 avatar，user 用于作用域检索，request 提供对话服务。
# 结果与约束：返回默认会话 ID 和列表；摘要需匹配目标当前资料、文件和技能集合日期，显示元数据而不泄露摘要或私人历史。
@router.get('/conversations')
def conversations(request: Request, channel: str = 'avatar', personal: bool=False, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    default = chat.session(user['id'],channel)
    rows = chat.memory.list(user['id'],channel)
    if personal:
        other='assistant' if channel=='avatar' else 'avatar'
        rows+=chat.memory.list(user['id'],other)
        rows.sort(key=lambda row:row['updated_at'],reverse=True)
    with chat.service.database.connect() as db:
        agent = db.execute('SELECT last_modified_at FROM avatar_records WHERE user_id=?', (user['id'],)).fetchone()
    targets={t['aic']:t for t in chat.assistant_router.catalog()} if channel == 'assistant' and not personal else {}
    for row in rows:
        row['memory_active'] = bool(row['summary_through'] and agent and row['summary_profile_modified_at'].split('|',1)[0] == max(agent['last_modified_at'],request.app.state.service.attachments.collection_date(user['id']),request.app.state.chat.skills.collection_date(user['id']))) if channel == 'avatar' else False
        row['selected_target'] = None
        if channel == 'assistant' and not personal and row.get('selected_aic'):
            target=targets.get(row['selected_aic'])
            if target:
                row['selected_target']={'aic':target['aic'],'name':target['name']}
                with chat.service.database.connect() as db:
                    child=db.execute("SELECT summary_through,summary_profile_modified_at,compression_count FROM chat_conversations WHERE parent_id=? AND target_aic=? AND platform='dingdang'",(row['id'],target['aic'])).fetchone()
                row['memory_active']=bool(child and child['summary_through'] and child['summary_profile_modified_at']==max(target['row']['last_modified_at'],request.app.state.service.attachments.collection_date(target['row']['user_id']),request.app.state.chat.skills.collection_date(target['row']['user_id'])))
                row['compression_count']=child['compression_count'] if child else 0
    if personal:
        for row in rows:
            row['selected_target']=None
            row['memory_active']=bool(row['summary_through'] and agent and row['summary_profile_modified_at'].split('|',1)[0]==max(agent['last_modified_at'],request.app.state.service.attachments.collection_date(user['id']),chat.skills.collection_date(user['id'])))
    default_id=next((row['id'] for row in rows if row['message_count']),default['id']) if personal else default['id']
    return {'default_id':default_id,'items':rows}


# 职责：为本人新建助手或分身对话会话。
# 输入：body 含频道和标题，user 提供认证身份。
# 结果与约束：调用 session 创建隔离会话并返回 ID、标题；本人分身未创建时不能新建其对话。
@router.post('/conversations', status_code=201)
def create_conversation(body: ConversationRequest, request: Request, user=Depends(auth.current_user)):
    row = request.app.state.chat.session(user['id'],body.channel,create=True,title=body.title)
    if body.title not in ('新会话','默认会话'):
        with request.app.state.database.connect(write=True) as db:
            db.execute("UPDATE chat_conversations SET title_source='manual' WHERE id=?", (row['id'],))
    return {'id':row['id'],'title':row['title']}


@router.get('/conversations/{conversation_id}/compact')
def compact_usage(conversation_id:str,request:Request,user=Depends(auth.current_user)):
    return request.app.state.chat.compact_usage(user,conversation_id)


@router.post('/conversations/{conversation_id}/compact')
async def compact_conversation(conversation_id:str,request:Request,focus:str=Body(default='',embed=True,max_length=2000),user=Depends(auth.current_user)):
    return await request.app.state.chat.compact(user,conversation_id,focus)


@router.patch('/conversations/{conversation_id}')
def rename_conversation(conversation_id: str, body: ConversationTitleRequest, request: Request, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    row = chat.session(user['id'], 'assistant', conversation_id, personal=True)
    with chat.service.database.connect(write=True) as db:
        db.execute("UPDATE chat_conversations SET title=?,title_source='manual' WHERE id=? AND user_id=?", (body.title,row['id'],user['id']))
    return {'id':row['id'],'title':body.title}


# 职责：删除本人会话、消息与关联目标子会话。
# 输入：conversation_id 为路径会话，channel 指定频道，user 为本人。
# 结果与约束：先校验完整作用域再删除，处理中拒绝操作，模型计费记录保留。
@router.delete('/conversations/{conversation_id}')
def delete_conversation(conversation_id: str, request: Request, channel: str = 'avatar', personal: bool=False, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    session = chat.session(user['id'],channel,conversation_id,personal=personal)
    chat.memory.remove(session['id'])
    return {'message':'会话及记忆已删除'}


# 职责：清空本人会话历史和压缩记忆。
# 输入：conversation_id、channel 经本人 session 校验。
# 结果与约束：保留父会话 ID，删除历史及子会话并重置摘要与选中目标；处理中返回冲突。
@router.post('/conversations/{conversation_id}/clear')
def clear_conversation(conversation_id: str, request: Request, channel: str = 'avatar', personal: bool=False, user=Depends(auth.current_user)):
    chat = request.app.state.chat
    session = chat.session(user['id'],channel,conversation_id,personal=personal)
    chat.memory.remove(session['id'],clear=True)
    return {'message':'历史记录和压缩记忆已清空'}


# 职责：提供返回完整回答的非流式问答接口。
# 输入：body 为消息请求，user 为认证账户，request 提供 AvatarChat。
# 结果与约束：通过 wit Runtime.invoke 等待 Entry 完成后返回统一结果；目标权限、计费与保存继续由领域服务校验。
@router.post("/chat")
async def chat(body: ChatRequest, request: Request, user=Depends(auth.current_user)):
    return await request.app.state.runtime.reply(user, body)


@router.get("/admin/pending-count")
def pending_review_count(request: Request, user=Depends(admin)):
    return {"pending": request.app.state.pending_reviews.get()}



@router.get('/admin/overview')
def admin_overview(request: Request, user=Depends(admin)):
    from avatar.profiles.admin_reads import counts
    with request.app.state.database.connect() as db:
        return {'counts': counts(db)}


@router.get('/admin/settings')
def admin_settings(request: Request, user=Depends(admin)):
    from avatar.profiles.admin_reads import settings_view
    return settings_view(request.app.state)


@router.get('/admin/agents/operations')
def agent_operations(request: Request, offset: int=Query(default=0,ge=0,le=2147483647),
                     search: str=Query(default='',max_length=100), user=Depends(admin)):
    from avatar.profiles.admin_reads import operations
    return operations(request.app.state, search, offset)


@router.get('/admin/agents/{user_id}/detail')
def admin_agent_detail(user_id: str, request: Request, user=Depends(admin)):
    from avatar.profiles.admin_reads import agent_detail
    with request.app.state.database.connect() as db:
        row = db.execute('SELECT a.*,u.username FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.user_id=?',
                         (user_id,)).fetchone()
    return agent_detail(request.app.state, row)


# 职责：提供管理员控制台汇总数据。
# 输入：user 通过 admin 依赖，request 提供业务服务。
# 结果与约束：返回统计、审核、任务、分身和事件，模型配置由服务脱敏。
@router.get("/admin/dashboard")
def dashboard(request: Request, user=Depends(admin)):
    result = request.app.state.service.dashboard(user['id'])
    result['environment'] = request.app.state.environment
    from avatar.protocols.registration.entity import staged_entity_metadata
    for agent in result['agents']:
        agent['protocol'] = request.app.state.personal_aip.status(agent['user_id'])
        agent['staged_identity'] = staged_entity_metadata(request.app.state.settings.data_dir, agent['user_id'])
    result['entity_registration_enabled'] = request.app.state.settings.entity_aip_enabled
    result['personal_registration_mode'] = request.app.state.settings.provider
    from avatar.protocols.transport.endpoints import entity_port_pool
    with request.app.state.database.connect() as db:
        result['entity_port_pool'] = entity_port_pool(request.app.state.settings, db=db)
    result['entity_port_pool']['online'] = sum(bool(agent['protocol']['serving']) for agent in result['agents'])
    return result


@router.post('/admin/agents/{user_id}/health')
async def agent_health(user_id:str,request:Request,user=Depends(admin)):
    return await request.app.state.personal_aip.health(user_id)


# 职责：按搜索、角色与偏移列出管理员可管理账户。
# 输入：search 最多 100 字符，role 限 all/user/admin，offset 非负，user 必须管理员。
# 结果与约束：委托账户管理服务分页检索，返回 items 与 total，不返回密码哈希。
@router.get('/admin/accounts')
def account_list(request: Request, search: str = Query(default='',max_length=100), role: Literal['all','user','admin'] = 'all', offset: int = Query(default=0,ge=0), user=Depends(admin)):
    return request.app.state.runtime.services.accounts.list(user,search,role,offset)


# 职责：修改指定账户管理员角色。
# 输入：user_id 为目标，body.role 为 user/admin，user 为经鉴权的操作者。
# 结果与约束：委托事务内再鉴权，遵守只可自主降级及根账户保护，保留目标业务数据。
@router.get('/admin/accounts/{user_id}/profile')
def account_profile(user_id:str,request:Request,user=Depends(admin)):
    return request.app.state.runtime.services.accounts.profile(user,user_id)


@router.put('/admin/accounts/{user_id}/role')
def account_role(user_id: str, body: AccountRoleRequest, request: Request, user=Depends(admin)):
    return request.app.state.runtime.services.accounts.set_role(user,user_id,body.role)


# 职责：执行管理员对分身资料快照的审核。
# 输入：submission_id 为申请，body 为审核决定，user 通过管理员依赖。
# 结果与约束：通过后创建持久化任务，驳回须说明原因；未通过的资料不发布到分身。
@router.post("/admin/reviews/{submission_id}")
def review(submission_id: str, body: ReviewRequest, request: Request, user=Depends(admin)):
    result = request.app.state.service.review(user, submission_id, body)
    request.app.state.pending_reviews.invalidate()
    return result


# 职责：重新排队一条失败的分身创建任务。
# 输入：job_id 指定任务，user 为经认证管理员。
# 结果与约束：仅 failed 任务可重试，返回 queued；路由不直接调用外部注册服务。
@router.post("/admin/jobs/{job_id}/retry")
def retry(job_id: str, request: Request, user=Depends(admin)):
    return request.app.state.service.retry(user, job_id)


# 职责：更新指定用户分身的演示证书。
# 输入：user_id 为分身所有人，user 为管理员。
# 结果与约束：委托已配置注册适配器，当前返回模拟续期证书，不宣称为正式 CA 签发。
@router.post("/admin/agents/{user_id}/renew")
def renew(user_id: str, request: Request, user=Depends(admin)):
    return request.app.state.service.renew(user, user_id)


# 职责：保存管理员填写的平台 URL 配置。
# 输入：body 为经过校验的 PlatformSettings，user 为管理员。
# 结果与约束：写入 settings 并返回配置，不因保存 URL 自动启用正式 ACPS。
@router.put("/admin/settings")
def settings(body: PlatformSettings, request: Request, user=Depends(admin)):
    return request.app.state.service.save_settings(user, body)


# 职责：提供按注册邮箱或手机号匹配的 Demo 找回接口。
# 输入：body 为规范化联系人与新密码，request.client.host 用于限流。
# 结果与约束：无需旧会话，统一返回匹配提示；身份强度受 Demo 简化要求限制，实际重置和撤销由认证服务完成。
@router.post("/auth/recover-password")
def recover_password(body: RecoveryRequest, request: Request):
    if request.app.state.settings.oidc_enabled:
        from fastapi import HTTPException
        raise HTTPException(403, '请在 Keycloak 账户中心找回登录凭据；根管理员由本地运维恢复')
    return auth.recover_password(request.app.state.database, body, request.client.host)


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


# 职责：提供带登录与 CSRF 校验的 SSE 问答入口。
# 输入：body 为 ChatRequest，user 为当前账户，request 提供对话服务。
# 结果与约束：浏览器断开仅停止 SSE 投递，后台继续生成并保存；刷新通过 pending 恢复状态。
@router.post("/chat/stream")
def chat_stream(body: ChatRequest, request: Request, user=Depends(auth.current_user)):
    from avatar.api.streaming import chat_response
    if not body.conversation_id:
        session=request.app.state.chat.session(user['id'],body.channel,None,personal=body.personal)
        body=body.model_copy(update={'conversation_id':session['id']})
    return chat_response(request.app.state.runtime, user, body, request.app.state.database)


@router.post('/chat/generations',status_code=202)
def submit_chat_generation(body: ChatRequest,request: Request,user=Depends(auth.current_user)):
    from avatar.api.streaming import submit_generation
    return submit_generation(request.app.state.database,user,body)


@router.get('/chat/pending')
def pending_chat(conversation_id: str, request: Request, user=Depends(auth.current_user)):
    from avatar.conversation.jobs.store import GenerationStore
    return GenerationStore(request.app.state.database).latest(user['id'],conversation_id)


@router.post('/chat/generations/{request_id}/stop')
async def stop_generation(request_id:str,request:Request,user=Depends(auth.current_user)):
    return await request.app.state.generations.cancel(user,request_id)


@router.get('/chat/generations/{request_id}')
def chat_generation(request_id: str,request: Request,user=Depends(auth.current_user)):
    from avatar.conversation.jobs.store import GenerationStore
    return GenerationStore(request.app.state.database).get(user['id'],request_id)


# 职责：校验和预览本人未保存的固定 ACS 表单。
# 输入：body 为 AcsPreviewRequest，user 已通过认证及 CSRF 校验。
# 结果与约束：返回完整只读候选及错误，不保存草稿、调用模型或向注册平台提交。
@router.post('/me/acs/preview')
def acs_preview(body: AcsPreviewRequest, request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.preview_acs(user, body)


# 职责：读取或下载本人保存的草稿/已生效 ACS。
# 输入：source 明确区分 draft 与 approved，download 为是否返回文件。
# 结果与约束：下载仅允许校验通过的内容，返回固定文件名；不提供任意用户的公开 ACS 读取入口。
@router.get('/me/acs')
def acs_document(request: Request, source: Literal['draft', 'approved'] = 'draft', download: bool = False, user=Depends(auth.current_user)):
    result = request.app.state.service.saved_acs(user, source)
    if download:
        if not result['validation']['valid']:
            from fastapi import HTTPException
            raise HTTPException(422, '；'.join(result['validation']['errors']))
        return Response(json.dumps(result['document'], ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="avatar-acs.json"'})
    return result


# 职责：接收本人选填资料原件，实际读取时限制大小。
# 输入：原始文件请求体、URL 编码文件名、可选文件集合日期和旧客户端说明请求头；会话与 CSRF 由依赖校验。
# 结果与约束：账户资料文件总额度 10 GiB，上传说明选填且最多 2000 字符，默认私密暂存；不影响资料提交。
@router.get('/me/material-folders')
def material_folders(request:Request,user=Depends(auth.current_user)):
    return MaterialFolders(request.app.state.service.attachments).list(user['id'])


@router.post('/me/material-folders',status_code=201)
def create_material_folder(body:FolderRequest,request:Request,user=Depends(auth.current_user)):
    return MaterialFolders(request.app.state.service.attachments).create(user,body)


@router.patch('/me/material-folders')
def rename_material_folder(body:FolderRenameRequest,request:Request,user=Depends(auth.current_user)):
    return rename_folder(request.app.state.service.attachments,user,body)


@router.patch('/me/attachments/{attachment_id}/name')
def rename_material_file(attachment_id:str,body:NameRequest,request:Request,user=Depends(auth.current_user)):
    return rename_file(request.app.state.service.attachments,user,attachment_id,body)


@router.delete('/me/material-folders')
def delete_material_folder(body:FolderRequest,request:Request,user=Depends(auth.current_user)):
    files=request.app.state.service.attachments
    result=files.remove(user,last_modified_at=body.last_modified_at,folder_path=body.path,recursive=body.recursive)
    return {**result,**MaterialFolders(files).list(user['id']),'files':files.inventory(user['id'])}


@router.post('/me/attachments', status_code=201)
async def upload_attachment(request: Request, filename: str | None = Query(default=None, min_length=1, max_length=200), last_modified_at: ModifiedAt | None = Query(default=None), user=Depends(auth.current_user)):
    from urllib.parse import unquote
    import base64
    encoded = request.headers.get('x-attachment-caption-base64')
    try:
        # UTF-8 Base64 避免长中文说明的百分号编码超过常见 HTTP 头大小上限。
        caption = (base64.b64decode(encoded, validate=True).decode('utf-8') if encoded is not None
                   else unquote(request.headers.get('x-attachment-caption', ''))).strip()
    except (ValueError, UnicodeError):
        from fastapi import HTTPException
        raise HTTPException(422, '资料说明编码无效')
    if len(caption) > 2000:
        from fastapi import HTTPException
        raise HTTPException(422, '资料说明最多 2000 字')
    if filename is None:
        filename = unquote(request.headers.get("x-attachment-filename", ""))
    from tempfile import TemporaryFile
    from avatar.files.uploads.buffer import upload_buffer, upload_work
    from fastapi import HTTPException
    files = request.app.state.service.attachments
    def read_quota():
        with files.database.connect() as db:
            return files.quota(db,user['id'])
    quota = await asyncio.to_thread(read_quota)
    remaining = max(0,quota['limit_bytes']-quota['used_bytes'])
    artifact_upload = filename.lower().endswith(ARTIFACT_SUFFIXES)
    limit = min(remaining,MAX_SOURCE_BYTES) if artifact_upload else remaining
    reason = 'HTML 或动画文件不能超过 100 MiB' if artifact_upload and limit==MAX_SOURCE_BYTES else '资料文件剩余额度不足'
    if int(request.headers.get('content-length') or 0) > limit:
        raise HTTPException(413,reason)
    with TemporaryFile() as payload:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413,reason)
            await upload_work(payload.write,chunk)
        with upload_buffer(payload) as data:
            return await files.upload(user,last_modified_at,filename,data,caption,
                relative_path=unquote(request.headers.get("x-attachment-relative-path","")),batch_id=upload_batch_id(request))


# 职责：提供个人资料文件管理所需的完整本人文件清单。
# 输入：request 定位应用服务，user 为认证账户，包括受限个人资料会话。
# 结果与约束：返回暂存与已保存独立文件元数据，不暴露磁盘路径或他人资料，不改变任何审核快照。
@router.get('/me/attachments')
def list_attachments(request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.attachments.inventory(user['id'])


# 职责：提供本人附件原件下载。
# 输入：attachment_id 指定文件，user 为已认证所有人。
# 结果与约束：不能通过 ID 访问他人文件；强制附件下载且禁止缓存，不在页面内执行文件内容。
@router.get('/me/attachments/{attachment_id}')
def own_attachment(attachment_id: str, request: Request, user=Depends(auth.current_user)):
    path, row = request.app.state.service.attachments.file_for(user, attachment_id)
    return FileResponse(path, media_type=row['content_type'], filename=row['filename'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


@router.get('/me/attachments/{attachment_id}/preview-text')
async def own_word_preview(attachment_id: str, request: Request, user=Depends(auth.current_user)):
    import subprocess
    from avatar.files.uploads.buffer import upload_work
    from avatar.rag.indexing.service import native_text
    files=request.app.state.service.attachments
    async with files.parsing:
        path,row=await asyncio.to_thread(files.file_for,user,attachment_id)
        if not row['filename'].lower().endswith('.docx'):
            raise HTTPException(415,'此文件不支持 Word 正文预览')
        try:
            result=await upload_work(native_text,path,'.docx')
        except (ValueError,OSError,subprocess.SubprocessError):
            raise HTTPException(422,'无法解析 Word 正文，请下载原件查看') from None
        await asyncio.to_thread(files.file_for,user,attachment_id)
    text=result['text']
    if len(text)>240000:text=text[:240000]+'\n…正文预览已截断，完整内容请下载原件。'
    return Response(text or '文档没有可预览的文字。',media_type='text/plain',headers={'Cache-Control':'private, no-store'})


# 职责：让管理员读取审核所需附件原件。
# 输入：attachment_id 为快照引用文件，user 必须通过 admin 依赖。
# 结果与约束：管理员可核验私密材料，普通账户及临时资料会话不能访问管理路径。
@router.get('/admin/attachments/{attachment_id}')
def review_attachment(attachment_id: str, request: Request, user=Depends(admin)):
    path, row = request.app.state.service.attachments.file_for(user, attachment_id, admin=True)
    return FileResponse(path, media_type=row['content_type'], filename=row['filename'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


# 职责：提供目标所有人当前已保存公开文件下载。
# 输入：aic 指定当前分身，attachment_id 指定其公开文件。
# 结果与约束：暂存、私密、已删除或不属于目标的文件统一拒绝，保存为私密后旧链接立即失效。
@router.get('/avatars/{aic}/attachments/{attachment_id}')
def public_attachment(aic: str, attachment_id: str, request: Request):
    path, row = request.app.state.service.attachments.public_file(aic, attachment_id)
    return FileResponse(path, media_type=row['content_type'], filename=row['filename'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


# 职责：彻底删除本人附件原件及全部资料引用和说明。
# 输入：attachment_id 为本人文件，last_modified_at 可选校验文件集合日期；user 经认证和 CSRF 校验。
# 结果与约束：当前及历史文件立即清除；撤销公开下载、推进受影响的生效日期以排除旧记忆，失败回滚。
@router.delete('/me/attachments/{attachment_id}')
def remove_attachment(attachment_id: str, request: Request, last_modified_at: ModifiedAt | None = Query(default=None), user=Depends(auth.current_user)):
    return request.app.state.service.attachments.remove(user, attachment_id, last_modified_at)


# 职责：让本人修改当前或历史文件说明。
# 输入：attachment_id 为本人文件，body 含非空说明和可选日期，user 经认证及 CSRF 检查。
# 结果与约束：历史文件可管理，说明和可见性保存立即生效，无需资料审核；不能修改他人文件或伪造路径。
@router.patch('/me/attachments/{attachment_id}')
def update_attachment_description(attachment_id: str, body: AttachmentDescriptionRequest, request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.attachments.update_description(user,attachment_id,body)


# 职责：读取本人独立模型配置。
# 输入：user 为认证账户，request 提供应用服务。
# 结果与约束：仅返回脱敏配置和更新时间，不返回 Key，不依赖资料审核状态。
@router.get('/me/model-settings')
def own_model_settings(request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.models.get(user['id'])


# 职责：保存立即生效的本人模型配置。
# 输入：body 为模型设置请求，user 由认证及 CSRF 确定。
# 结果与约束：无需资料审核，后续问答及记忆压缩使用新配置，已有资料审核任务不会覆盖它。
@router.put('/me/model-settings')
async def save_model_settings(body: ModelSettingsRequest, request: Request, user=Depends(auth.current_user)):
    service=request.app.state.service
    def prepare():
        with service.database.connect() as db:
            row=db.execute('SELECT updated_at FROM model_settings WHERE user_id=?',(user['id'],)).fetchone()
            baseline=row[0] if row else None
            if body.updated_at is not None and body.updated_at!=baseline:raise HTTPException(409,'模型配置已变化，请刷新后重新保存')
            config=service.vault.resolve(body.model_config_input,service.models.current(db,user['id']))
        return baseline,config
    baseline,config=await asyncio.to_thread(prepare)
    from avatar.models.model_settings import verify_api
    await verify_api(request.app.state.chat.model_client,config,service.vault)
    saved=await asyncio.to_thread(service.models.save,user,body,validated=config,baseline=baseline)
    from avatar.models.capabilities import detect
    asyncio.create_task(detect(service.database,user['id'],config,request.app.state.chat.model_client,service.vault),name='avatar-model-capability-detect')
    return saved


# 职责：根据本人输入的 Base URL/Key 读取模型目录，不保存设置、不使用其他账户密钥。
# 结果与约束：只有平台、格式、地址与已存配置一致才沿用空白 Key；网络请求复用生成客户端防护。
@router.post('/me/model-options')
async def own_model_options(body: ModelConfigInput, request: Request, user=Depends(auth.current_user)):
    from fastapi import HTTPException
    from avatar.models.discovery import discover
    service=request.app.state.service
    def prepare():
        with service.database.connect() as db:previous=service.models.current(db,user['id'])
        return service.vault.resolve(body,previous)
    config=await asyncio.to_thread(prepare)
    if not config.get('encrypted_key'):raise HTTPException(422,'请填写此 Base URL 对应的 API Key 后获取模型')
    result=await discover(request.app.state.chat.model_client,config,service.vault.decrypt(config['encrypted_key']))
    from avatar.models.capabilities import remember
    await asyncio.to_thread(remember,service.database,user['id'],config,result['models'])
    return result


# 职责：提供十条一页的记录列表，统计不受旧概览快照上限影响。
# 输入：collection 为固定业务类别，scope 决定本人/管理视图，offset/search/status 均有边界。
# 结果与约束：管理视图再次核验管理员；不接受 user_id，不返回凭据、技能正文或他人私有聊天。
class RecordSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids:list[str]=Field(default_factory=list,max_length=100)
    all:bool=Field(default=False,strict=True)


@router.post('/records/{collection}/delete')
def delete_records(collection:Literal['model_calls','identity_jobs','jobs','history','events'],body:RecordSelection,request:Request,user=Depends(auth.current_user)):
    from avatar.profiles.records import dismiss_records,dismiss_all_records
    if body.all:
        if body.ids:raise HTTPException(422,'全部删除不能同时指定记录 ID')
        return dismiss_all_records(request.app.state,user,collection)
    return dismiss_records(request.app.state,user,collection,body.ids)


@router.get('/records/{collection}')
def records(collection: Literal['history','submissions','events','model_calls','dialogues','agents','jobs','identity_jobs','registrations','account_reviews'],request: Request,
            scope: Literal['user','admin']='user',offset: int=Query(default=0,ge=0,le=2147483647),
            search: str=Query(default='',max_length=100),status: Literal['all','pending','approved','rejected','reviewed']='all',user=Depends(auth.current_user)):
    from avatar.profiles.records import record_page
    return record_page(request.app.state,user,collection,scope,offset,search,status)


# 职责：协商结束时把当前助手会话恢复为本人分身，不发送问题或调用模型。
# 输入：已认证用户及本人 Web 助手父会话 ID。
# 结果与约束：仅改变这一会话的目标，保持历史/摘要；临时资料会话不能调用，不接受其他用户或目标 AIC。
@router.post('/conversations/{conversation_id}/self')
def conversation_self(conversation_id: str,request: Request,user=Depends(auth.current_user)):
    from fastapi import HTTPException
    from avatar.core.db import now
    with request.app.state.database.connect(write=True) as db:
        if not db.execute("SELECT 1 FROM chat_conversations WHERE id=? AND user_id=? AND channel='assistant' AND platform='web' AND parent_id IS NULL",(conversation_id,user['id'])).fetchone():raise HTTPException(404,'助手会话不存在')
        own=db.execute("SELECT aic FROM avatar_records WHERE user_id=? AND identity_status='active'",(user['id'],)).fetchone()
        if not own:raise HTTPException(409,'本人分身暂不可用')
        db.execute('UPDATE chat_conversations SET selected_aic=?,updated_at=? WHERE id=?',(own['aic'],now(),conversation_id))
    return {'selected_aic':own['aic']}


# 职责：显示本人会话实际使用及移出的技能。
# 输入：认证账户与本人 Web 父会话 ID。
# 结果与约束：返回有限名称元数据，不返回技能正文或其他账户目录。
@router.get('/conversations/{conversation_id}/skills')
def conversation_skills(conversation_id: str,request: Request,user=Depends(auth.current_user)):
    return request.app.state.chat.session_skills.list(user['id'],conversation_id)


from pydantic import BaseModel,ConfigDict,StrictBool


# 职责：校验会话技能开关，只接受布尔值，不接受全局设置或账户 ID。
class SessionSkillToggle(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: StrictBool


# 职责：在本会话移出/恢复技能，不卸载用户安装文件。
# 输入：会话、本人技能 ID 和 enabled 布尔值，CSRF 由会话依赖校验。
# 结果与约束：会话生成期间拒绝修改，变更仅影响当前父会话的技能加载与旧摘要。
@router.patch('/conversations/{conversation_id}/skills/{skill_id}')
def conversation_skill_update(conversation_id: str,skill_id: str,body: SessionSkillToggle,request: Request,user=Depends(auth.current_user)):
    from avatar.skills.policy import require_skills_enabled
    require_skills_enabled()
    chat=request.app.state.chat
    with request.app.state.database.connect() as db:chat.session_skills.owned(db,user['id'],conversation_id)
    chat.memory.acquire(conversation_id)
    try:return chat.session_skills.set_enabled(user['id'],conversation_id,skill_id,body.enabled)
    finally:chat.memory.release(conversation_id)


from avatar.files.library.batch import BatchRequest


@router.post('/me/attachments/batch')
def batch_attachments(body:BatchRequest,request:Request,user=Depends(auth.current_user)):
    from avatar.files.library.batch import apply
    return apply(request.app.state.service.attachments,user,body)


@router.post('/me/upload-batches/{batch_id}/complete')
def complete_upload(batch_id:str,body:UploadComplete,request:Request,user=Depends(auth.current_user)):
    from avatar.files.uploads.batches import complete
    return complete(request.app.state.database,user,batch_id,body)
