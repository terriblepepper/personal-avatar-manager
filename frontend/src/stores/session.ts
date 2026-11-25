import { ref } from 'vue'
import { defineStore } from 'pinia'
import { request, ApiError, setCsrfToken, setSessionExpiryHandler } from '../api/client'
import type { AuthConfig, SessionResponse, User } from '../types'
import { isAdministrator } from '../utils/accountRole'

export const useSessionStore = defineStore('session', () => {
  const user = ref<User | null>(null), csrf = ref(''), authConfig = ref<AuthConfig | null>(null)
  const ready = ref(false), failure = ref(''), pending = ref(0)
  let revision = 0
  function clear() { revision++; pending.value = 0; user.value = null; csrf.value = ''; setCsrfToken('') }
  function accept(value: SessionResponse) {
    if (value.user.id !== user.value?.id || !isAdministrator(value.user)) pending.value = 0
    revision++
    user.value = value.user
    csrf.value = value.csrf
    setCsrfToken(value.csrf)
  }
  setSessionExpiryHandler(clear)
  async function refresh(signal?: AbortSignal) { const before = revision; const value = await request<SessionResponse>('/auth/me', 'GET', undefined, signal); if (!signal?.aborted && before === revision) accept(value) }
  async function initialize() {
    failure.value = ''
    try {
      authConfig.value = await request<AuthConfig>('/auth/config')
      await refresh()
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) clear()
      else failure.value = error instanceof Error ? error.message : '无法读取登录配置'
    } finally { ready.value = true }
  }
  async function advanceOnboarding(stage: 'tour' | 'done') {
    const account = user.value?.id
    const result = await request<{ stage: 'tour' | 'done' }>('/me/onboarding/' + stage, 'POST')
    if (user.value && user.value.id === account) {
      revision++
      user.value.onboarding_stage = result.stage
    }
    return result.stage
  }
  async function logout() {
    const value = await request<{ logout_url?: string }>('/auth/logout', 'POST')
    clear()
    if (value.logout_url) location.assign(value.logout_url)
  }
  return { user, csrf, authConfig, ready, failure, pending, clear, accept, refresh, initialize, advanceOnboarding, logout }
})
