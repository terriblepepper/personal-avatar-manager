export const labels: Record<string, string> = {
  draft: '待填写资料', pending: '待审核', updating: '更新审核中', rejected: '审核未通过',
  creating: '创建 / 同步中', active: '创建成功', approved: '审核通过', failed: '执行失败',
  done: '已完成', queued: '等待执行', running: '执行中', waiting: '等待注册条件就绪',
  certificate_reused: '已核验并沿用原证书', certificate_revocation: '核验旧证书注销', identity_verification: '核验分身证书', superseded: '已被更新资料替代',
  registry_submitting: '提交 Registry 中', registry_review: '等待 Registry 审核',
  certificate_issuing: 'Registry 已通过 · 正在签证', registry_rejected: 'Registry 已拒绝',
  registry_blocked: 'Registry 申请受阻', configuration: '等待注册配置', draining: '等待当前对话结束', identity_cleanup: '等待清理旧身份',
};
export function date(value?: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
}
export function statusLabel(status?: string | null): string {
  return labels[status || ''] || status || '—';
}

export function fileSizeMB(bytes = 0): string {
  if (bytes === 0) return '0 MB'
  return bytes < 10000 ? '<0.01 MB' : (bytes / 1000000).toFixed(2) + ' MB'
}

export interface StorageQuota { used_bytes: number; limit_bytes: number }

export function quotaSummary(quota: StorageQuota) {
  function size(bytes: number) {
    const units = ['B', 'KiB', 'MiB', 'GiB']
    let unit = 0
    while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit++ }
    return Number(bytes.toFixed(2)) + ' ' + units[unit]
  }
  return `已用 ${size(quota.used_bytes)}，剩余 ${size(Math.max(0, quota.limit_bytes - quota.used_bytes))}，总容量 ${size(quota.limit_bytes)}`
}

export function splitFilename(filename: string) {
  const dot = filename.lastIndexOf('.')
  const suffix = dot > 0 && dot < filename.length - 1 ? filename.slice(dot) : ''
  return { stem: suffix ? filename.slice(0, dot) : filename, suffix }
}
