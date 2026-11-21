import { afterEach, describe, expect, it, vi } from 'vitest'
import { submitAndObserve, parseSSE } from '../src/api/generations'

afterEach(() => vi.unstubAllGlobals())
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
    expect(values).toEqual(['部分', '完整回答'])
    expect(result.reply).toBe('完整回答')
    expect(fetcher.mock.calls.map(call => JSON.parse((call[1] as RequestInit).body as string).request_id)).toEqual(['streamed', 'streamed'])
  })
})
