# 模块说明：根管理员旧账号迁移接口；输入指定业务账号及真实 User ID 或已登录用户名，先预览再确认，不提供普通用户自助接管。
from typing import Literal
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from avatar.accounts.auth import current_user

router = APIRouter(prefix='/api/admin/identity-migration')


# 职责：限定迁移预览的目标和显式空账号清理开关。
# 输入：user_id 为旧业务账号，subject 为已核验的 Keycloak User ID 或唯一已登录用户名。
# 结果与约束：不接受 issuer/角色/手机号/密码；默认不允许转移，未知字段拒绝。
class MigrationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=255, pattern=r'^[^\s\x00-\x1f\x7f]+$')
    allow_empty_transfer: bool = False


# 职责：限定预览后提交的核验确认。
# 输入：一次性预览凭据、归属核验确认和备注。
# 结果与约束：identity_verified 必须是真布尔 true，不能等同于手机号已验证；备注用于审计，不存 Keycloak 凭据。
class MigrationCommitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    preview_token: str = Field(min_length=32, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    identity_verified: Literal[True]
    reason: str = Field(min_length=3, max_length=500)

    # 职责：要求明确的布尔归属确认。
    # 输入：原始 identity_verified 值。
    # 结果与约束：拒绝整数或字符串代替确认；此确认不赋予手机号验证状态。
    @field_validator('identity_verified', mode='before')
    @classmethod
    def explicit_verification(cls, value):
        if value is not True:
            raise ValueError('必须明确确认已核验账号归属')
        return value


# 职责：提供旧账号关联状态及迁移配置。
# 输入：搜索和分页，user 经会话与 CSRF 依赖检查。
# 结果与约束：服务再次读取数据库确认根权限；不返回用户凭据，不调用 Keycloak 管理接口。
@router.get('')
def migration_inventory(request: Request, search: str = Query(default='', max_length=100), offset: int = Query(default=0, ge=0), user=Depends(current_user)):
    return request.app.state.identity_migration.inventory(user, search, offset)


# 职责：预览指定旧账号将保留的资产和可选空账号转移。
# 输入：固定字段预览请求与根管理员会话。
# 结果与约束：只创建短期预览记录，不执行迁移、不变更原资料，返回五分钟凭据。
@router.post('/preview')
def migration_preview(body: MigrationPreviewRequest, request: Request, user=Depends(current_user)):
    return request.app.state.identity_migration.preview(user, body)


# 职责：执行一次经归属核验确认的预览计划。
# 输入：一次性凭据和审计备注，不允许提交新的目标覆盖预览。
# 结果与约束：事务内再校验权限和指纹，冲突整体回滚；迁移不更改原角色/档案/AIC/Key/文件/会话历史。
@router.post('/commit')
def migration_commit(body: MigrationCommitRequest, request: Request, user=Depends(current_user)):
    return request.app.state.identity_migration.commit(user, body)
