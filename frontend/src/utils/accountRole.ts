import type { User } from '../types'

export const accountRoleLabels: Record<User['role'], string> = {
  user: '普通用户',
  admin: '管理员',
  super_admin: '超级管理员',
}

export function isAdministrator(user: User | null | undefined) {
  return user?.role === 'admin' || user?.role === 'super_admin'
}

export function canViewOps(user: User | null) {
  return user?.access_scope === 'full' && user.account_status === 'active' &&
    (user.is_root || user.role === 'super_admin')
}
