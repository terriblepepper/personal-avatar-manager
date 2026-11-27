import { ElMessage } from 'element-plus'

let csrfToken = ''
let onExpired: (() => void) | undefined
export const setCsrfToken = (value: string) => { csrfToken = value }
export const getCsrfToken = () => csrfToken
export const setSessionExpiryHandler = (handler: () => void) => { onExpired = handler }
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); this.name = 'ApiError' }
}
export function notifyError(error: unknown) {
  if (error instanceof DOMException && error.name === 'AbortError') return
  ElMessage.error(error instanceof Error ? error.message : String(error))
}
export async function request<T = unknown>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal,
    timeoutMs: number | null = method === 'GET' ? 15000 : 60000, extraHeaders?: HeadersInit): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  if (signal?.aborted) abort()
  else signal?.addEventListener('abort', abort, { once: true })
  const timeout = timeoutMs === null ? undefined : setTimeout(() => controller.abort(new DOMException('请求超时', 'TimeoutError')), timeoutMs)
  const token = csrfToken, headers = new Headers(extraHeaders)
  headers.set('X-CSRF-Token', token)
  const multipart = body instanceof FormData
  const binary = body instanceof Blob
  if (body !== undefined && !multipart) headers.set('Content-Type', binary ? 'application/octet-stream' : 'application/json')
  try {
    const response = await fetch(path.startsWith('/api/') ? path : '/api' + path, {
      method, credentials: 'same-origin', cache: 'no-store', headers, signal: controller.signal,
      ...(body !== undefined ? { body: multipart || binary ? body : JSON.stringify(body) } : {}),
    })
    let invalidJSON = false
    const data: unknown = await response.json().catch(() => { invalidJSON = true; return null })
    if (!response.ok) {
      if (response.status === 401 && token && token === csrfToken) {
        const expired = path === '/auth/me' || await fetch('/api/auth/me', { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(8000) }).then(r => r.status === 401).catch(() => false)
        if (expired && token === csrfToken) onExpired?.()
      }
      const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : null
      throw new ApiError(typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}），请检查填写内容或稍后重试`, response.status)
    }
    if (invalidJSON && response.status !== 204) throw new ApiError('服务器返回了无效的数据', response.status)
    return data as T
  } finally {
    clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}
