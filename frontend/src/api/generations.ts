import { ApiError, getCsrfToken, request } from './client'
import type { GenerationRequest, GenerationSnapshot } from '../types'

export type StreamEvent = Record<string, unknown> & { type: string }
function eventValue(value: unknown): value is StreamEvent { return !!value && typeof value === 'object' && 'type' in value && typeof value.type === 'string' }
export async function parseSSE(stream: ReadableStream<Uint8Array>, onEvent: (value: StreamEvent) => void, signal?: AbortSignal) {
  const reader = stream.getReader(), decoder = new TextDecoder()
  let buffer = ''
  const cancel = () => { void reader.cancel().catch(() => {}) }
  signal?.addEventListener('abort', cancel, { once: true })
  try {
    while (!signal?.aborted) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      let match: RegExpExecArray | null
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, match.index); buffer = buffer.slice(match.index + match[0].length)
        const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n')
        if (data) { const value: unknown = JSON.parse(data); if (eventValue(value)) onEvent(value) }
      }
      if (chunk.done) break
    }
  } finally { signal?.removeEventListener('abort', cancel); await reader.cancel().catch(() => {}); reader.releaseLock() }
}
const terminal = (value: GenerationSnapshot) => value.done || !!value.error || ['succeeded', 'failed', 'cancelled', 'indeterminate', 'awaiting_input'].includes(value.state || '')
function pause(signal: AbortSignal, ms: number) {
  return new Promise<void>(resolve => {
    const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve() }
    const timer = setTimeout(done, ms); signal.addEventListener('abort', done, { once: true }); if (signal.aborted) done()
  })
}
function waitForForeground(signal: AbortSignal) {
  return new Promise<void>(resolve => {
    const listeners = new AbortController()
    const options = {signal: listeners.signal}
    const wake = () => {
      if (signal.aborted || (!document.hidden && navigator.onLine !== false)) {
        listeners.abort()
        resolve()
      }
    }
    document.addEventListener('visibilitychange', wake, options)
    window.addEventListener('online', wake, options)
    signal.addEventListener('abort', wake, options)
    wake()
  })
}
export async function observeGeneration(requestId: string, onSnapshot: (value: GenerationSnapshot) => void, signal: AbortSignal): Promise<GenerationSnapshot> {
  let missing = 0, delay = 1000
  while (!signal.aborted) {
    if (document.hidden || navigator.onLine === false) {
      await waitForForeground(signal)
      continue
    }
    try {
      const value = await request<GenerationSnapshot | null>('/chat/generations/' + encodeURIComponent(requestId), 'GET', undefined, signal)
      delay = 1000
      if (value?.request_id === requestId) { missing = 0; onSnapshot(value); if (terminal(value)) return value }
      else if (++missing >= 10) throw new ApiError('尚未确认服务器接收结果，请查看会话记录；不会自动重发', 409)
    } catch (error) {
      if (signal.aborted) break
      if (error instanceof ApiError && error.status < 500 && error.status !== 429) throw error
      delay = Math.min(delay * 2, 8000)
    }
    await pause(signal, delay)
  }
  throw new DOMException('已停止观察，后台任务继续执行', 'AbortError')
}
export async function submitAndObserve(body: GenerationRequest, onSnapshot: (value: GenerationSnapshot) => void, signal: AbortSignal, onAccepted?: () => void): Promise<GenerationSnapshot> {
  try {
    await request('/chat/generations', 'POST', body, signal)
    onAccepted?.()
  }
  catch (error) {
    if (signal.aborted || error instanceof ApiError && error.status < 500 && error.status !== 429) throw error
    return observeGeneration(body.request_id, onSnapshot, signal)
  }
  // A proxy can keep a silent response open even after the job has finished.
  const stream = new AbortController()
  const closeStream = () => stream.abort()
  let deadline: ReturnType<typeof setTimeout> | undefined
  const expectSnapshot = () => { clearTimeout(deadline); deadline = setTimeout(closeStream, 15000) }
  signal.addEventListener('abort', closeStream, { once: true })
  if (signal.aborted) closeStream()
  expectSnapshot()
  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST', credentials: 'same-origin', signal: stream.signal,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() }, body: JSON.stringify(body),
    })
    if (!response.ok || !response.body) throw new Error('流连接暂不可用')
    let latest: GenerationSnapshot | undefined
    await parseSSE(response.body, event => {
      if (event.type === 'snapshot' && event.snapshot && typeof event.snapshot === 'object') {
        const snapshot = event.snapshot as GenerationSnapshot
        if (stream.signal.aborted || snapshot.request_id !== body.request_id) return
        latest = snapshot
        onSnapshot(snapshot)
        if (terminal(snapshot)) closeStream()
        else expectSnapshot()
      }
    }, stream.signal)
    if (latest && terminal(latest)) return latest
  } catch (error) { if (signal.aborted) throw error }
  finally {
    clearTimeout(deadline)
    signal.removeEventListener('abort', closeStream)
    closeStream()
  }
  return observeGeneration(body.request_id, onSnapshot, signal)
}
