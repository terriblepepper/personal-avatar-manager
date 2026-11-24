export type WindowName = '1h' | '24h' | '7d'
export type ComponentState = 'ready' | 'disabled' | 'unavailable'

export interface Traffic {
  requests: number
  client_errors: number
  server_errors: number
  interrupted: number
  received_bytes: number
  sent_bytes: number
}
export interface Point extends Traffic { time: string; sample_minutes: number }
export interface RecentTraffic extends Traffic {
  span_seconds: number
  in_flight: number
  open_streams: number
  p50_ms: number | null
  p95_ms: number | null
  p99_ms: number | null
  first_byte_p95_ms: number | null
  routes: { route: string; count: number; p95_ms: number | null }[]
}
export interface Services {
  sampled_at: string
  process: { cpu_percent: number; memory_bytes: number; threads: number } | null
  postgres: {
    state: ComponentState
    pool: Record<string, number>
    connections?: number
    commits?: number
    rollbacks?: number
    deadlocks?: number
    lock_waits?: number
  }
  redis: { state: ComponentState; hits?: number; misses?: number; evicted?: number; memory_bytes?: number; limit_bytes?: number }
  kafka: { state: ComponentState; relay_pending: number | null }
  queues: Record<string, { queued: number; running: number }> | null
}
export interface Dashboard {
  now: string
  started_at: string
  http: RecentTraffic
  series: Point[]
  bucket_minutes: number
  history_ready: boolean
  services: Services | null
  timings: Record<string, { p95_ms: number; p99_ms: number; window_count: number }>
  csrf: string
}

const decimal = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const wallTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})
export const amount = (n?: number | null) => n == null ? '—' : decimal.format(n)
export const latency = (n?: number | null) => n == null ? '—' : amount(n) + ' ms'
export const timestamp = (date: string | number) => wallTime.format(new Date(date))
export function bytes(n?: number | null) {
  if (n == null) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let unit = 0
  while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit++ }
  return amount(n) + ' ' + units[unit]
}
