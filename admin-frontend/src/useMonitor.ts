import { onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { Dashboard, Point, WindowName } from './metrics'

class ResponseError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

async function call<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, credentials: 'same-origin', cache: 'no-store' })
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ResponseError(typeof body?.detail === 'string' ? body.detail : '请求失败', response.status)
  }
  if (body == null) throw new Error('无法读取监控响应')
  return body
}

export async function fetchMinutes(time: string, count: number, signal: AbortSignal) {
  const start = Math.floor(Date.parse(time) / 60000)
  const { series } = await call<{ series: Point[] }>('/api/ops/metrics/minutes?start=' + start + '&count=' + count, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
  })
  return series
}

export function useMonitor() {
  const snapshot = shallowRef<Dashboard>()
  const period = ref<WindowName>('1h')
  const phase = ref<'checking' | 'login' | 'monitor'>('checking')
  const paused = ref(false)
  const loading = ref(false)
  const submitting = ref(false)
  const failure = ref('')
  let active: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let mounted = false

  function stop() {
    clearTimeout(timer)
    active?.abort()
    active = undefined
    loading.value = false
  }
  function schedule() {
    clearTimeout(timer)
    if (mounted && phase.value !== 'login' && !paused.value && !document.hidden && navigator.onLine !== false) {
      timer = setTimeout(() => { void refresh() }, 15000)
    }
  }
  async function refresh() {
    if (!mounted || submitting.value) return
    stop()
    const controller = new AbortController()
    active = controller
    loading.value = true
    try {
      const value = await call<Dashboard>('/api/ops/metrics?window=' + period.value, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
      })
      if (controller.signal.aborted) return
      snapshot.value = value
      phase.value = 'monitor'
      failure.value = ''
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof ResponseError && [401, 403].includes(error.status)) {
        snapshot.value = undefined
        phase.value = 'login'
        failure.value = error.status === 403 ? '请使用本地管理员账号登录。' : ''
      } else {
        failure.value = '监控暂不可用，请稍后重试。'
      }
    } finally {
      if (active === controller) {
        active = undefined
        loading.value = false
        schedule()
      }
    }
  }
  async function login(username: string, password: string) {
    if (submitting.value) return
    stop()
    submitting.value = true
    try {
      await call('/api/ops/login', {
        method: 'POST', signal: AbortSignal.timeout(15000),
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
    } catch (error) {
      failure.value = error instanceof ResponseError ? error.message : '登录失败，请重试。'
      return
    } finally {
      submitting.value = false
    }
    await refresh()
  }
  async function logout() {
    if (submitting.value || !snapshot.value) return
    stop()
    submitting.value = true
    try {
      await call('/api/auth/logout', {
        method: 'POST', signal: AbortSignal.timeout(10000),
        headers: { 'X-CSRF-Token': snapshot.value.csrf },
      })
      snapshot.value = undefined
      phase.value = 'login'
      failure.value = ''
    } catch (error) {
      if (error instanceof ResponseError && [401, 403].includes(error.status)) {
        snapshot.value = undefined
        phase.value = 'login'
      }
      failure.value = '登出请求未确认，请刷新页面核对会话。'
    } finally {
      submitting.value = false
      schedule()
    }
  }
  function visibility() {
    if (document.hidden || navigator.onLine === false) stop()
    else if (phase.value !== 'login' && !paused.value) void refresh()
  }
  onMounted(() => {
    mounted = true
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('online', visibility)
    window.addEventListener('offline', visibility)
    void refresh()
  })
  onUnmounted(() => {
    mounted = false
    stop()
    document.removeEventListener('visibilitychange', visibility)
    window.removeEventListener('online', visibility)
    window.removeEventListener('offline', visibility)
  })
  watch(period, () => { void refresh() })
  watch(paused, value => { if (value) clearTimeout(timer); else void refresh() })
  return { snapshot, period, phase, paused, loading, submitting, failure, refresh, login, logout }
}
