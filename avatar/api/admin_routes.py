from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import HTTPException
from typing import Literal
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
import avatar.accounts.auth as auth
from avatar.profiles.schemas import ReviewRequest
from avatar.core.platform_settings import PlatformSettings
from avatar.accounts.schemas import AccountRoleRequest
from avatar.api.dependencies import PageOffset, ShortPageSearch, admin
from avatar.profiles.admin_reads import agent_detail, counts, operations, settings_view
from avatar.profiles.records import dismiss_all_records, dismiss_records, record_page


router = APIRouter()


@router.get("/admin/pending-count")
def pending_review_count(request: Request, user=Depends(admin)):
    return {"pending": request.app.state.pending_reviews.get()}


@router.get('/admin/overview')
def admin_overview(request: Request, user=Depends(admin)):
    with request.app.state.database.connect() as db:
        return {'counts': counts(db)}


@router.get('/admin/settings')
def admin_settings(request: Request, user=Depends(admin)):
    return settings_view(request.app.state)


@router.get('/admin/agents/operations')
def agent_operations(request: Request, offset: PageOffset = 0,
                     search: ShortPageSearch = '', user=Depends(admin)):
    return operations(request.app.state, search, offset)


@router.get('/admin/agents/{user_id}/detail')
def admin_agent_detail(user_id: str, request: Request, user=Depends(admin)):
    with request.app.state.database.connect() as db:
        row = db.execute('SELECT a.*,u.username FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.user_id=?',
                         (user_id,)).fetchone()
    return agent_detail(request.app.state, row)


@router.post('/admin/agents/{user_id}/health')
async def agent_health(user_id:str,request:Request,user=Depends(admin)):
    return await request.app.state.personal_aip.health(user_id)


# 职责：按搜索、角色与偏移列出管理员可管理账户。
# 输入：search 最多 100 字符，role 限 all/user/admin，offset 非负，user 必须管理员。
# 结果与约束：委托账户管理服务分页检索，返回 items 与 total，不返回密码哈希。
@router.get('/admin/accounts')
def account_list(request: Request, search: ShortPageSearch = '', role: Literal['all','user','admin','super_admin'] = 'all', offset: PageOffset = 0, user=Depends(admin)):
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


# 职责：提供十条一页的记录列表，统计不受旧概览快照上限影响。
# 输入：collection 为固定业务类别，scope 决定本人/管理视图，offset/search/status 均有边界。
# 结果与约束：管理视图再次核验管理员；不接受 user_id，不返回凭据、技能正文或他人私有聊天。
class RecordSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids:list[str]=Field(default_factory=list,max_length=100)
    all:bool=Field(default=False,strict=True)


@router.post('/records/{collection}/delete')
def delete_records(collection:Literal['model_calls','identity_jobs','jobs','history','events'],body:RecordSelection,request:Request,user=Depends(auth.current_user)):
    if body.all:
        if body.ids:raise HTTPException(422,'全部删除不能同时指定记录 ID')
        return dismiss_all_records(request.app.state,user,collection)
    return dismiss_records(request.app.state,user,collection,body.ids)


@router.get('/records/{collection}')
def records(collection: Literal['history','submissions','events','model_calls','dialogues','agents','jobs','identity_jobs','registrations','account_reviews'],request: Request,
            scope: Literal['user','admin']='user',offset: PageOffset = 0,
            search: ShortPageSearch = '',status: Literal['all','pending','approved','rejected','reviewed']='all',user=Depends(auth.current_user)):
    return record_page(request.app.state,user,collection,scope,offset,search,status)
