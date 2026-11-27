import { afterEach, describe, expect, it, vi } from 'vitest'
import { request, setCsrfToken, setSessionExpiryHandler, ApiError } from '../src/api/client'
import { submitAndObserve, parseSSE } from '../src/api/generations'
import { requestId } from '../src/utils/requestId'

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
  it('uploads the original bytes and expires a confirmed stale session on a non-JSON 401', async () => {
    const expired = vi.fn()
    setSessionExpiryHandler(expired)
    setCsrfToken('current')
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('expired', { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
    vi.stubGlobal('fetch', fetcher)
    const file = new File(['bytes'], 'source.txt')
    await expect(request('/me/attachments', 'POST', file, undefined, 60000, { 'X-Upload-Batch': 'batch' }))
      .rejects.toMatchObject({ status: 401 })
    const options = fetcher.mock.calls[0][1]
    expect(options.body).toBe(file)
    expect(options.headers.get('Content-Type')).toBe('application/octet-stream')
    expect(options.headers.get('X-Upload-Batch')).toBe('batch')
    expect(options.headers.get('X-CSRF-Token')).toBe('current')
    expect(expired).toHaveBeenCalledOnce()
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

describe('durable streamed generation', () => {
  it('decodes split UTF-8 SSE frames, comments and CRLF without duplicate deltas', async () => {
    const bytes = new TextEncoder().encode(': ping\r\nevent: delta\r\ndata: {"type":"delta","text":"你好"}\r\n\r\nevent: done\ndata: {"type":"done"}\n\n')
    const stream = new ReadableStream<Uint8Array>({ start(c) { for (const byte of bytes) c.enqueue(new Uint8Array([byte])); c.close() } })
    const items: unknown[] = []
    await parseSSE(stream, value => items.push(value))
    expect(items).toEqual([{ type: 'delta', text: '你好' }, { type: 'done' }])
  })
  it('recovers the original request after an uncertain submission without POST replay', async () => {
    const controller = new AbortController()
    const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('lost response')).mockResolvedValueOnce(
      new Response(JSON.stringify({ request_id: 'original', done: true, reply: '回答', state: 'succeeded', result: { type: 'done', reply: '回答' } })),
    )
    vi.stubGlobal('fetch', fetcher)
    const result = await submitAndObserve({ request_id: 'original', conversation_id: 'c1', message: '问题', personal: true, channel: 'assistant', attachment_ids: [] }, () => {}, controller.signal)
    expect(result.reply).toBe('回答')
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls[1]?.[0]).toBe('/api/chat/generations/original')
    expect(fetcher.mock.calls.filter(call => (call[1] as RequestInit)?.method === 'POST')).toHaveLength(1)
  })
  it('aborts an active stream reader when the component unmounts', async () => {
    const controller = new AbortController(); let cancelled = false
    const stream = new ReadableStream<Uint8Array>({ cancel() { cancelled = true } })
    const reading = parseSSE(stream, () => {}, controller.signal)
    controller.abort()
    await reading
    expect(cancelled).toBe(true)
  })
  it('delivers partial snapshots before completion while retaining one request identity', async () => {
    const values: string[] = []
    const frames = [
      { type: 'snapshot', snapshot: { request_id: 'streamed', reply: '部分', done: false, state: 'running' } },
      { type: 'snapshot', snapshot: { request_id: 'streamed', reply: '完整回答', done: true, state: 'succeeded' } },
    ].map(item => 'event: snapshot\ndata: ' + JSON.stringify(item) + '\n\n').join('')
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{"accepted":true}')).mockResolvedValueOnce(new Response(frames))
    vi.stubGlobal('fetch', fetcher)
    const result = await submitAndObserve({ request_id: 'streamed', conversation_id: 'c1', message: '问题', personal: true, channel: 'assistant', attachment_ids: [] }, value => values.push(value.reply), new AbortController().signal)
    expect(new Headers(fetcher.mock.calls[1]?.[1].headers).get('X-Avatar-Stream')).toBe('snapshots')
    expect(values).toEqual(['部分', '完整回答'])
    expect(result.reply).toBe('完整回答')
    expect(fetcher.mock.calls.map(call => JSON.parse((call[1] as RequestInit).body as string).request_id)).toEqual(['streamed', 'streamed'])
  })
})

it('generates distinct UUIDs when randomUUID is unavailable on an HTTP deployment', () => {
  const original = globalThis.crypto
  vi.stubGlobal('crypto', { getRandomValues: original.getRandomValues.bind(original) })
  const first = requestId(), second = requestId()
  expect(first).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/)
  expect(second).not.toBe(first)
})
