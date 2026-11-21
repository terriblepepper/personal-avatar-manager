import { ref, watch, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import { useSessionStore } from '../stores/session'

export const workspaceDebugKey: InjectionKey<Readonly<Ref<boolean>>> = Symbol('workspace debug')

export function useWorkspaceGuide(admin: ComputedRef<boolean>) {
  const session = useSessionStore()
  const phase = ref<'welcome' | 'tour' | null>(null)
  const debug = ref(false), busy = ref(false), error = ref('')
  const key = 'avatar:guide-debug:' + session.user?.id
  let preview = false
  try { debug.value = localStorage.getItem(key) === '1' } catch { /* 调试开关仍可在本次会话使用。 */ }
  function toggleDebug() {
    debug.value = !debug.value
    try { localStorage.setItem(key, debug.value ? '1' : '0') } catch { /* 忽略不可用的浏览器存储。 */ }
  }
  function replay(start: 'welcome' | 'tour') {
    if (admin.value || !debug.value) return
    preview = true
    error.value = ''
    phase.value = start
  }
  watch([admin, () => session.user?.onboarding_stage], ([management, stage]) => {
    if (management) { phase.value = null; preview = false; return }
    if (preview || busy.value) return
    phase.value = session.user?.role === 'user' && (stage === 'welcome' || stage === 'tour') ? stage : null
  }, { immediate: true })
  async function advance(stage: 'tour' | 'done') {
    if (!phase.value || busy.value) return
    busy.value = true
    error.value = ''
    try {
      const next = preview ? stage : await session.advanceOnboarding(stage)
      phase.value = next === 'done' || admin.value ? null : 'tour'
      if (!phase.value) preview = false
    } catch { error.value = '引导进度保存失败。请检查连接，再点一次当前按钮。' }
    finally { busy.value = false }
  }
  return { phase, debug, busy, error, toggleDebug, replay, advance }
}
