import json
from avatar.protocols.identity.acs import AcsPreviewRequest
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from typing import Literal
import avatar.accounts.auth as auth
from avatar.profiles.schemas import DraftRequest
from avatar.profiles.schemas import SubmitRequest


router = APIRouter()


# 职责：提供当前用户可使用的资料字段目录。
# 输入：user 仅用于保证已登录及临时会话范围允许。
# 结果与约束：返回字段名称、控件、身份范围和默认隐私定义，不返回个人数据。
@router.get("/profile-fields")
def profile_fields(user=Depends(auth.current_user)):
    from avatar.contracts.profile_fields import catalog
    return catalog()


# 职责：提供按 AIC 访问的分身公开档案。
# 输入：aic 为路径目标，request 提供业务服务。
# 结果与约束：无需登录但仅返回经过隐私过滤的已生效资料，不包含聊天历史或模型 Key。
@router.get("/avatars/{aic}/public")
def public_avatar(aic: str, request: Request):
    return request.app.state.service.public_avatar(aic)


# 职责：提供当前账户个人中心业务概览。
# 输入：user 提供本人 ID，request 提供 AvatarService。
# 结果与约束：仅查询本人资料、申请、分身与用量，并由服务脱敏配置。
@router.get("/me")
def overview(request: Request, user=Depends(auth.current_user)):
    return request.app.state.service.overview(user["id"], access_scope=user["access_scope"])


# 职责：保存当前账户的个人资料草稿。
# 输入：body 含 Profile和修改日期，user 经过会话与 CSRF 检查。
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
