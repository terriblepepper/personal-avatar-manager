from typing import Annotated

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictBool
import avatar.accounts.auth as auth


# 职责：提供管理路由共用的权限依赖。
# 输入：user 由 auth.current_user 注入，已完成会话与 CSRF 校验。
# 结果与约束：检查 admin 角色并返回用户；临时资料会话不允许进入管理路由。
def admin(user=Depends(auth.current_user)):
    auth.require_admin(user)
    return user


class SkillToggle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool


PageOffset = Annotated[int, Query(ge=0, le=2**31-1)]
PageSearch = Annotated[str, Query(max_length=200)]
ShortPageSearch = Annotated[str, Query(max_length=100)]


def owner(user=Depends(auth.current_user)):
    if user.get('access_scope', 'full') != 'full':
        raise HTTPException(403, '知识库仅允许本人完整会话访问')
    return user
