import { inject, onMounted, onUnmounted, shallowRef, watch, type InjectionKey } from 'vue'
import { usePolling } from './usePolling'

type Topic = 'inbox' | 'dialogue' | 'reviews' | 'overview'
const topics: Topic[] = ['inbox', 'dialogue', 'reviews', 'overview']

export function createPlatformUpdates(account: () => string | undefined) {
  const connected = shallowRef(false)
  const handlers = new Map<Topic, Set<() => void>>()
  const pending = new Set<Topic>()
  let source: EventSource | undefined
  let reconnect: ReturnType<typeof setTimeout> | undefined
  let refresh: ReturnType<typeof setTimeout> | undefined
  let attempts = 0

  function queue(changes: Topic[]) {
    for (const topic of changes) pending.add(topic)
    refresh ??= setTimeout(() => {
      refresh = undefined
      for (const topic of pending) {
        for (const listener of handlers.get(topic) ?? []) listener()
      }
      pending.clear()
    }, 150)
  }
  function stop() {
    source?.close()
    source = undefined
    clearTimeout(reconnect)
    reconnect = undefined
    connected.value = false
  }
  function open() {
    if (source || reconnect || !account() || document.hidden || navigator.onLine === false || typeof EventSource === 'undefined') return
    const stream = new EventSource('/api/updates')
    source = stream
    stream.addEventListener('ready', () => {
      if (source !== stream) return
      attempts = 0
      connected.value = true
      queue(topics)
    })
    stream.addEventListener('change', event => {
      if (source !== stream) return
      const changes: unknown = JSON.parse((event as MessageEvent<string>).data)
      if (Array.isArray(changes)) queue(topics.filter(topic => changes.includes(topic)))
    })
    stream.onerror = () => {
      if (source !== stream) return
      const wasConnected = connected.value
      stop()
      if (wasConnected) queue(topics)
      reconnect = setTimeout(() => {
        reconnect = undefined
        open()
      }, Math.min(60000, 5000 * 2 ** Math.min(attempts++, 4)))
    }
  }
  function sync() {
    if (!account() || document.hidden || navigator.onLine === false) stop()
    else open()
  }
  watch(account, () => { stop(); attempts = 0; sync() })
  onMounted(() => {
    document.addEventListener('visibilitychange', sync)
    window.addEventListener('online', sync)
    window.addEventListener('offline', sync)
    sync()
  })
  onUnmounted(() => {
    stop()
    clearTimeout(refresh)
    document.removeEventListener('visibilitychange', sync)
    window.removeEventListener('online', sync)
    window.removeEventListener('offline', sync)
  })

  function on(topic: Topic, listener: () => void) {
    let listeners = handlers.get(topic)
    if (!listeners) handlers.set(topic, listeners = new Set())
    onMounted(() => listeners.add(listener))
    onUnmounted(() => listeners.delete(listener))
  }
  return { connected, on }
}

export const platformUpdatesKey: InjectionKey<ReturnType<typeof createPlatformUpdates>> = Symbol('platformUpdates')

export function useLivePolling(topic: Topic, read: Parameters<typeof usePolling>[0],
    delay: () => number, options?: Parameters<typeof usePolling>[2]) {
  const updates = inject(platformUpdatesKey, null)
  const polling = usePolling(read, () => updates?.connected.value ? (topic === 'dialogue' ? 15000 : 60000) : delay(), options)
  updates?.on(topic, polling.refresh)
  return polling
}
