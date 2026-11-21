import { MAX_ANIMATION_BYTES } from './animation'

/** Read an authorized attachment without buffering an oversized response. */
export async function readAnimation(path: string, signal: AbortSignal): Promise<string> {
  const url = new URL(path, location.href)
  if (url.origin !== location.origin || url.username || url.password || !url.pathname.startsWith('/api/')) {
    throw Error('只能读取本平台有权访问的动画包')
  }
  const idle = new AbortController()
  const deadline = () => setTimeout(() => idle.abort(new DOMException('读取动画包超时', 'TimeoutError')), 15000)
  let timer = deadline()
  try {
    const response = await fetch(url, { credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      signal: AbortSignal.any([signal, idle.signal]) })
    if (!response.ok || !response.body) throw Error('动画包不存在或没有访问权限')
    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })
    let source = '', size = 0
    try {
      if (!response.headers.get('Content-Encoding') && Number(response.headers.get('Content-Length')) > MAX_ANIMATION_BYTES) {
        throw Error('动画包超过 100 MiB')
      }
      while (true) {
        clearTimeout(timer)
        timer = deadline()
        const { done, value } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > MAX_ANIMATION_BYTES) throw Error('动画包超过 100 MiB')
        source += decoder.decode(value, { stream: true })
      }
      return source + decoder.decode()
    } finally {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  } finally { clearTimeout(timer) }
}
