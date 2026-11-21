import { onBeforeUnmount, onMounted, watch } from 'vue'
import { notifyError } from '../api/client'

export function usePolling(read: (signal: AbortSignal) => Promise<void>, delay: () => number,
                           { enabled = () => true }: { enabled?: () => boolean } = {}) {
  let mounted = false, queued = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let active: AbortController | undefined, flight: Promise<void> | undefined
  const allowed = () => mounted && !document.hidden && navigator.onLine !== false && enabled()

  function stop() {
    clearTimeout(timer)
    active?.abort()
    active = undefined
    flight = undefined
    queued = false
  }
  function refresh(): Promise<void> {
    if (!allowed()) return Promise.resolve()
    if (flight) { queued = true; return flight }
    clearTimeout(timer)
    const controller = new AbortController()
    active = controller
    flight = Promise.resolve().then(async () => {
      do {
        if (controller.signal.aborted || !allowed()) return
        queued = false
        try { await read(controller.signal) }
        catch (error) { if (!controller.signal.aborted) notifyError(error) }
      } while (!controller.signal.aborted && queued)
    }).finally(() => {
      if (active !== controller) return
      active = undefined
      flight = undefined
      if (allowed()) timer = setTimeout(wake, delay())
    })
    return flight
  }
  function restart() { stop(); return refresh() }
  function wake() {
    if (!allowed()) stop()
    else if (!flight) void refresh()
  }
  watch(enabled, restart, {flush:'sync'})
  onMounted(() => {
    mounted = true
    document.addEventListener('visibilitychange', wake)
    window.addEventListener('online', wake)
    window.addEventListener('offline', wake)
    wake()
  })
  onBeforeUnmount(() => {
    mounted = false
    stop()
    document.removeEventListener('visibilitychange', wake)
    window.removeEventListener('online', wake)
    window.removeEventListener('offline', wake)
  })
  return { refresh, restart }
}
