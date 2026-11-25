# 模块说明：个人派生身份管理 API，沿用完整会话、管理员权限及 CSRF 校验。
from typing import Literal
from fastapi import APIRouter, Depends, Request
from avatar.api.dependencies import admin

router = APIRouter(prefix='/api/admin/agents', tags=['个人正式身份'])


# 职责：提交指定个人分身的正式注册、证书更新或注销任务。
# 输入：action 为固定枚举，user_id 来自所选分身，user 为经过认证的管理员。
# 结果与约束：仅排队并返回 202；不在 HTTP 请求中等待远端签发，不允许普通用户或临时资料会话操作。
@router.post('/{user_id}/identity/{action}', status_code=202)
def maintain_identity(user_id: str, action: Literal['register', 'renew', 'unregister'], request: Request, user=Depends(admin)):
    return request.app.state.entity_lifecycle.enqueue(user, user_id, action)


@router.post('/identity-jobs/{job_id}/refresh', status_code=202)
def refresh_identity(job_id: str, request: Request, user=Depends(admin)):
    return request.app.state.entity_lifecycle.refresh(user, job_id)
