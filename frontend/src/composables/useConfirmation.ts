import { onScopeDispose, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { notifyError } from '../api/client'
import { useSessionStore } from '../stores/session'

export function useConfirmation() {
  let active = true
  let pending = false
  onScopeDispose(() => {
    active = false
  })

  return async function confirmAction(
    message: string,
    title: string,
    target: () => unknown,
    perform: () => unknown
  ) {
    if (!active || pending) return
    const session = useSessionStore()
    if (!session.user) return
    const scope = () =>
      JSON.stringify([
        session.csrf,
        session.user?.id,
        session.user?.role,
        session.user?.is_root,
        session.user?.access_scope,
        session.user?.account_status,
        target()
      ])
    const initial = scope(),
      page = location.href
    let valid = true
    const stop = watch(
      scope,
      () => {
        valid = false
      },
      { flush: 'sync' }
    )
    pending = true
    try {
      await ElMessageBox.confirm(message, title, {
        type: 'warning',
        distinguishCancelAndClose: true,
        confirmButtonText: '确认',
        cancelButtonText: '取消'
      })
      stop()
      if (active && valid && scope() === initial && page === location.href) await perform()
    } catch (error) {
      if (error !== 'cancel' && error !== 'close') notifyError(error)
    } finally {
      stop()
      pending = false
    }
  }
}
