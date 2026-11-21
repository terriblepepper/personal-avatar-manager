import { afterEach, describe, expect, it, vi } from 'vitest'
import { request, setCsrfToken, ApiError } from '../src/api/client'

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken('') })
describe('existing REST and CSRF contract', () => {
  it('sends writes once with the session CSRF and never retries lost responses', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('connection lost'))
    vi.stubGlobal('fetch', fetcher)
    setCsrfToken('test-csrf')
    await expect(request('/chat/generations', 'POST', { request_id: 'original' })).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/chat/generations')
    const options = fetcher.mock.calls[0]?.[1] as RequestInit
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('test-csrf')
    expect(options.credentials).toBe('same-origin')
  })
  it('keeps multipart boundaries for uploads', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher)
    const form = new FormData(); form.append('file', new Blob(['x']), 'x.txt')
    await request('/me/attachments', 'POST', form)
    expect(new Headers((fetcher.mock.calls[0]?.[1] as RequestInit).headers).has('Content-Type')).toBe(false)
  })
  it('preserves HTTP error status and reports non-JSON server failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>failure</html>', { status: 502 })))
    await expect(request('/me')).rejects.toMatchObject({ status: 502 })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"会话已过期"}', { status: 401 })))
    await expect(request('/auth/me')).rejects.toBeInstanceOf(ApiError)
  })
  it('accepts a successful null pending result without treating it as invalid JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('null')))
    expect(await request('/chat/pending?conversation_id=empty')).toBeNull()
  })
})

it('limits reads to 15 seconds and writes to 60 seconds without replaying either', async () => {
  vi.useFakeTimers()
  const fetcher = vi.fn((_url: string, { signal }: RequestInit) => new Promise((_resolve, reject) => {
    signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
  vi.stubGlobal('fetch', fetcher)
  try {
    for (const [method, deadline] of [['GET', 15000], ['POST', 60000]] as const) {
      const pending = expect(request('/agent-sessions/session', method)).rejects.toMatchObject({ name: 'TimeoutError' })
      await vi.advanceTimersByTimeAsync(deadline - 1)
      expect(fetcher.mock.lastCall?.[1].signal?.aborted).toBe(false)
      await vi.advanceTimersByTimeAsync(1)
      await pending
    }
    expect(fetcher).toHaveBeenCalledTimes(2)
  } finally { vi.useRealTimers() }
})
