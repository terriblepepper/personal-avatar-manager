// @vitest-environment node
import { expect, it } from 'vitest'
import { createServer } from 'vite'

it('serves deep links and preview assets while preserving resource and page 404s', async () => {
  const server = await createServer({ server: { host: '127.0.0.1', port: 0, hmr: false }, logLevel: 'silent' })
  try {
    await server.listen()
    const address = server.httpServer!.address()
    if (!address || typeof address === 'string') throw Error('Expected a TCP listener')
    const get = (path: string) => fetch(`http://127.0.0.1:${address.port}${path}`)
    for (const path of ['/chat', '/admin/reviews?from=home']) expect(await (await get(path)).text()).toContain('id="app"')
    expect(await (await get('/static/preview/mermaid.html')).text()).toContain('/static/preview/render.js')
    expect((await get('/static/preview/render.js')).headers.get('content-type')).toContain('javascript')
    for (const path of ['/static/core/app.js', '/static/missing', '/assets/missing.js', '/admin/missing.css', '/unknown']) {
      expect((await get(path)).status, path).toBe(404)
    }
  } finally { await server.close() }
})
