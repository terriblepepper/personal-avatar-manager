from typing import Literal

AccountRole = Literal['user', 'admin', 'super_admin']
ADMIN_ROLES = ('admin', 'super_admin')


def can_view_ops(user):
    return user['account_status'] == 'active' and (user['is_root'] or user['role'] == 'super_admin')
