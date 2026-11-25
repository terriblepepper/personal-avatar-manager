from fastapi import Depends
import avatar.accounts.auth as auth


# 职责：提供管理路由共用的权限依赖。
# 输入：user 由 auth.current_user 注入，已完成会话与 CSRF 校验。
# 结果与约束：检查 admin 角色并返回用户；临时资料会话不允许进入管理路由。
def admin(user=Depends(auth.current_user)):
    auth.require_admin(user)
    return user
