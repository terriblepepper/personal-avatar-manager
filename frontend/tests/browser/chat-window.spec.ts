import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test'

const session = 'window-layout-peer'
const peerPath = '/api/agent-sessions/' + session
const stamp = '2026-01-01T12:00:00Z'
const longLink = 'https://example.test/' + 'long-path-'.repeat(18)
const table = [
  '| 指标 | ' + Array.from({ length: 14 }, (_, i) => '周期' + (i + 1)).join(' | ') + ' |',
  '| ' + Array(15).fill('---').join(' | ') + ' |',
  '| 记录 | ' + Array(14).fill('周期汇总数据').join(' | ') + ' |',
].join('\n')
const content = (id: number) => {
  if (id === 39) return `进度记录。\n\n<${longLink}>\n\n\`\`\`js\nconst data = "${'x'.repeat(160)}"\n\`\`\`\n\n${table}`
  if (id === 40) return '资料索引流程。\n\n```mermaid\nflowchart LR\nA[接收问题] --> B[检索资料] --> C[汇总结果]\n```\n'
  return `第 ${id} 条工作记录。已整理讨论结果并更新会话。\n\n本轮资料已复核。`
}

async function login(page: Page, username: string) {
  await page.goto('/')
  await page.locator('input[name=username]').fill(username)
  await page.locator('input[name=password]').fill(username + '-password')
  await page.getByRole('button', { name: /登录工作台/ }).click()
  await expect(page.locator('.shell')).toBeVisible()
}

// Provide only history pages. Login, conversation selection, settings, tools and file APIs use the smoke server.
async function history(page: Page, peer: boolean, resource: string) {
  let release!: () => void
  const held = new Promise<void>(resolve => { release = resolve })
  const requests: number[] = []
  await page.route(url => url.pathname === (peer ? peerPath : '/api/chat'), async route => {
    if (route.request().method() !== 'GET') return route.continue()
    const cursor = new URL(route.request().url()).searchParams.get('before')
    const end = cursor ? Number(cursor) - 1 : 40
    const size = peer ? 10 : 20
    if (cursor) { requests.push(Number(cursor)); await held }
    const ids = Array.from({ length: Math.min(end, size) }, (_, i) => end - Math.min(end, size) + 1 + i)
    if (peer) return route.fulfill({ json: {
      session_id: session, peer_name: '协作分身', history_revision: 'window-1', resource_conversation_id: resource,
      messages: ids.map(turn => ({ turn, dialogue_id: 'window-message', speaker_role: 'agent',
        speaker_name: turn % 2 ? '我的分身' : '协作分身', is_own: !!(turn % 2), content: content(turn) })),
      has_more: ids[0] > 1, task_page: { items: [], total: 0, offset: 0 },
    } })
    return route.fulfill({ json: ids.map(id => ({
      id, role: id % 2 ? 'user' : 'assistant', content: content(id), created_at: stamp,
    })) })
  })
  return { requests, release }
}

async function centerHits(items: Locator) {
  return items.evaluateAll(nodes => nodes.flatMap(el => {
    const box = el.getBoundingClientRect(), x = box.left + box.width / 2, y = box.top + box.height / 2
    const hit = document.elementFromPoint(x, y)
    const inBounds = box.left >= -1 && box.right <= innerWidth + 1 && box.top >= -1 && box.bottom <= innerHeight + 1
    return inBounds && hit && el.contains(hit) ? [] : [{
      label: el.textContent?.trim() || el.getAttribute('aria-label'), x, y, inBounds,
      hit: [hit, hit?.parentElement].map(node => [node?.tagName, node?.getAttribute('class')].filter(Boolean).join('.')).join(' in '),
    }]
  }))
}

async function desktopPanels(page: Page, scope: Locator) {
  const styles: Record<string, string> = {}
  async function inspect(label: string, panel: Locator, contrast = false) {
    await expect(panel).toBeVisible()
    const { filter, background } = await panel.evaluate(el => ({
      filter: getComputedStyle(el).backdropFilter, background: getComputedStyle(el).backgroundColor,
    }))
    expect(filter).toMatch(contrast ? /^none$/ : /blur\((?!0px\))[\d.]+px\)/)
    expect(background).not.toMatch(/^(?:transparent|rgba\(0, 0, 0, 0\))$/)
    styles[label] = filter + ' / ' + background
  }
  const more = scope.locator('summary[aria-label="会话操作"]')
  await more.click()
  await scope.getByText('表达风格学习', { exact: true }).click()
  await expect(scope.getByRole('checkbox', {name:'自动学习本人表达'})).toBeEnabled()
  await inspect('表达风格学习', scope.locator('.chat-menu-subpanel'))
  await more.click()
  await expect(scope.locator('.composer-tool > summary').filter({hasText:'技能'})).toHaveCount(0)
  for (const name of ['常用提问', '文件', '工具']) {
    const trigger = scope.locator('.chat-compose-actions .composer-tool > summary').filter({ hasText: name })
    await trigger.click()
    await inspect(name, trigger.locator('..').locator(':scope > .composer-popover'))
    await trigger.press('Escape')
  }
  await page.getByRole('button', { name: '账户与帮助' }).click()
  await page.getByRole('button', { name: /主题/ }).last().click()
  await page.getByRole('menuitemradio', { name: /深色/ }).last().click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await more.click()
  await inspect('dark', scope.locator('.chat-command-menu > .chat-command-panel'))
  await page.emulateMedia({ forcedColors: 'active' })
  await inspect('forcedColors', scope.locator('.chat-command-menu > .chat-command-panel'), true)
  await page.emulateMedia({ forcedColors: 'none' })
  await more.press('Escape')
  return styles
}

async function menu(page: Page, scope: Locator) {
  const trigger = scope.locator('summary[aria-label="会话操作"]')
  const panel = scope.locator('.chat-command-menu > .chat-command-panel')
  await trigger.focus()
  await page.keyboard.press('Enter')
  await expect(panel).toBeVisible()
  const menuBounds = await panel.evaluate(el => {
    const rect = el.getBoundingClientRect()
    const container = el.closest('#personal-chat-card, [role="dialog"]')!.getBoundingClientRect()
    return { leftInset: rect.left - Math.max(0, container.left), rightInset: Math.min(innerWidth, container.right) - rect.right }
  })
  expect(Math.min(menuBounds.leftInset, menuBounds.rightInset)).toBeGreaterThanOrEqual(-1)
  const items = panel.locator('.chat-menu-item:visible:not(:disabled)')
  expect.soft(await centerHits(items)).toEqual([])
  await page.keyboard.press('ArrowDown')
  await expect(items.first()).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(items.nth(1)).toBeFocused()
  const itemHeights = await items.evaluateAll(rows => rows.map(row => row.getBoundingClientRect().height))
  expect(itemHeights.length).toBeGreaterThanOrEqual(2)
  expect(Math.max(...itemHeights) - Math.min(...itemHeights)).toBeLessThanOrEqual(1)
  expect(Math.max(...itemHeights)).toBeLessThanOrEqual(40)
  const filter = await panel.evaluate(el => getComputedStyle(el).backdropFilter)
  expect(filter).toMatch(/blur\((?!0px\))[\d.]+px\)/)
  await page.keyboard.press('Escape')
  await expect(panel).toBeHidden()
  await expect(trigger).toBeFocused()
  await trigger.press('Enter')
  await expect(panel).toBeVisible()
  const input = scope.getByRole('textbox', { name: /聊天消息|分身会话消息/ })
  await input.focus()
  await expect(panel).toBeHidden()
  return { filter, itemHeights, menuBounds }
}

async function measure(log: Locator, commands: Locator) {
  await expect(log.locator('[data-history-message="39"] pre code')).toHaveCount(1)
  await expect(log.locator('a[href^="https://example.test/"]')).toHaveCount(1)
  await expect(log.locator('iframe')).toHaveCount(1)
  const tableScroll = await log.locator('table').evaluate(table => {
    for (let node: Element | null = table; node && !node.hasAttribute('data-history-message'); node = node.parentElement) {
      if (['auto', 'scroll'].includes(getComputedStyle(node).overflowX) && node.scrollWidth > node.clientWidth) {
        node.scrollLeft = 50
        const moved = node.scrollLeft
        node.scrollLeft = 0
        return moved
      }
    }
    return 0
  })
  expect(tableScroll).toBeGreaterThan(0)
  const widths = await log.evaluate(el => {
    const style = getComputedStyle(el)
    const available = el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
    const rows = Array.from(el.querySelectorAll<HTMLElement>('[data-history-message]'))
    return {
      available, largest: Math.max(...rows.map(row => row.getBoundingClientRect().width)),
      bubbleOverflow: Math.max(...rows.map(row => row.scrollWidth - row.clientWidth)), overflow: el.scrollWidth - el.clientWidth,
    }
  })
  expect(widths.largest).toBeLessThanOrEqual(widths.available / 2 + 1)
  expect(widths.overflow).toBeLessThanOrEqual(1)
  expect(widths.bubbleOverflow).toBeLessThanOrEqual(1)
  const heights = await commands.evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().height).filter(Boolean))
  expect(heights.length).toBeGreaterThanOrEqual(4)
  expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(1)
  expect(Math.max(...heights)).toBeLessThanOrEqual(44)
  return { widths, heights, tableScroll }
}

async function toTop(page: Page, log: Locator, mobile: boolean) {
  await log.scrollIntoViewIfNeeded()
  const box = (await log.boundingBox())!
  const top = Math.max(0, box.y), bottom = Math.min(page.viewportSize()!.height, box.y + box.height)
  const x = Math.round(box.x + box.width / 2), y = Math.round(top + (bottom - top) * 0.15)
  if (mobile) {
    const cdp = await page.context().newCDPSession(page)
    try {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] })
      const distance = Math.min(250, (bottom - y) * 0.9)
      for (let step = 1; step <= 8; step++) {
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y + distance * step / 8 }] })
        await page.evaluate(() => new Promise(requestAnimationFrame))
      }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
    } finally { await cdp.detach() }
  } else {
    await page.mouse.move(x, y)
    await page.mouse.wheel(0, -300)
  }
}

async function capture(page: Page, info: TestInfo, file: string) {
  if (process.env.UI_PARITY_OUTPUT) await page.screenshot({ path: info.outputPath(file + '.png'), animations: 'disabled' })
}

for (const mobile of [false, true]) {
  const device = mobile ? 'mobile' : 'desktop'
  test.describe(device, () => {
    test.use({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 950 }, isMobile: mobile, hasTouch: mobile })
    for (const peer of [false, true]) {
      const view = peer ? 'peer' : 'personal'
      test(`${view} window controls and anchored history`, async ({ page }, info) => {
        const styleEnabled = !peer && !mobile
        await login(page, styleEnabled ? 'browser-style' : 'browser-user')
        const conversations = await page.request.get('/api/conversations?channel=assistant&personal=true')
        expect(conversations.ok()).toBe(true)
        const {default_id: resource, embedding_configured} = await conversations.json()
        expect(embedding_configured).toBe(styleEnabled)
        expect(resource).toBeTruthy()
        const pages = await history(page, peer, resource)
        try {
          await page.goto(peer ? '/dialogues?session=' + session : '/chat')
          const scope = peer ? page.getByRole('dialog', { name: '分身间会话' }) : page.locator('#personal-chat-card')
          if (!peer && !styleEnabled) {
            await expect(scope.getByText('请配置嵌入模型', {exact: true})).toBeVisible()
            await expect(scope.getByText('表达风格学习', {exact: true})).toHaveCount(0)
          }
          const log = scope.getByRole('log')
          const rows = log.locator('[data-history-message]')
          const size = peer ? 10 : 20
          await expect(rows).toHaveCount(size)
          await expect(scope.getByRole('button', { name: /加载更早|加载更多消息/ })).toHaveCount(0)
          const panel = await menu(page, scope)
          const input = scope.getByRole('textbox', { name: /聊天消息|分身会话消息/ })
          await input.fill('核对发送位置')
          const send = scope.locator('.chat-send')
          await expect(send).toBeEnabled()
          await send.scrollIntoViewIfNeeded()
          expect(await centerHits(send)).toEqual([])
          await input.fill('')
          const controls = scope.locator('.chat-header-commands .chat-command, .chat-compose-actions .composer-tool > summary, .chat-compose-actions .chat-send')
          const geometry = await measure(log, controls)
          await scope.locator('summary[aria-label="会话操作"]').click()
          await expect.poll(() => scope.locator('.chat-command-menu').evaluate(el => getComputedStyle(el, '::details-content').opacity)).toBe('1')
          await capture(page, info, `${view}-${device}-menu`)
          await page.keyboard.press('Escape')
          await rows.last().scrollIntoViewIfNeeded()
          await capture(page, info, `${view}-${device}-messages`)

          // Start above the bottom but outside the history threshold, then use the native input path twice.
          await log.evaluate(node => { node.scrollTop = 120 })
          await toTop(page, log, mobile)
          const first = 41 - size
          await expect.poll(() => pages.requests).toEqual([first])
          await log.evaluate(node => { node.scrollTop = 120 })
          await toTop(page, log, mobile)
          await expect.poll(() => log.evaluate(node => node.scrollTop)).toBeLessThanOrEqual(80)
          const anchorId = await log.evaluate(el => Array.from(el.querySelectorAll('[data-history-message]'))
            .find(row => row.getBoundingClientRect().bottom > el.getBoundingClientRect().top)?.getAttribute('data-history-message'))
          expect(anchorId).toBeTruthy()
          const anchor = log.locator(`[data-history-message="${anchorId}"]`)
          const offset = () => anchor.evaluate(node => node.getBoundingClientRect().top - node.closest('[role="log"]')!.getBoundingClientRect().top)
          const before = await offset()
          pages.release()
          await expect(rows).toHaveCount(size * 2)
          await expect.poll(async () => Math.abs((await offset()) - before)).toBeLessThanOrEqual(2)
          const delta = (await offset()) - before
          expect(pages.requests).toEqual([first])
          console.log(JSON.stringify({ view, device, ...geometry, ...panel, anchorDelta: delta, requests: pages.requests }))
          if (!peer && !mobile) console.log('desktop panel styles', await desktopPanels(page, scope))
        } finally { pages.release() }
      })
    }
  })
}
