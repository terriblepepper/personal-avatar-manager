import { expect, test, type Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const screenshots = process.env.UI_PARITY_SCREENSHOTS
const stage = process.env.UI_PARITY_STAGE || 'vue'
const personal = ['overview','profile','avatar','messages','history']
const tabs = ['avatar','files','knowledge','chat','models','skills','dialogues']
const admin = ['overview','users','migration','agents','jobs','history','question_bank','messages','settings']

async function checkLibraryPicker(page: Page, id: string, temporary = false) {
  const folder = {id:'folder', source:'folder', path:'报告', filename:'报告', size:1500000}
  const file = {id:'sample', source:'profile', filename:'会议纪要.pdf', relative_path:'报告/会议纪要.pdf', size:1500000}
  await page.route('**/api/conversations/'+id+'/file-library?**', route => {
    const items = new URL(route.request().url()).searchParams.get('folder') ? [file] : [folder, {...file,id:'existing',filename:'已有文件.pdf',already_referenced:true}]
    return route.fulfill({json:{items,total:items.length,offset:0,limit:10}})
  })
  let submitted: unknown
  await page.route('**/api/conversations/'+id+'/files/reference', route => {
    submitted = route.request().postDataJSON()
    return route.fulfill({json:{file_id:file.id,items:[{...file,referenced:!temporary,origin:temporary?'attachment':'reference'}]}})
  })
  const open = page.getByRole('button',{name:temporary?'从资料库添加':'从资料库引用',exact:true})
  const upload = page.getByRole('button',{name:'上传本地文件',exact:true})
  const appearance = (el: Element) => {
    const css = getComputedStyle(el)
    return [css.font, css.padding, css.backgroundColor, css.borderRadius, css.height]
  }
  expect(await open.evaluate(appearance)).toEqual(await upload.evaluate(appearance))
  const chooser = page.waitForEvent('filechooser')
  await upload.focus()
  await page.keyboard.press('Enter')
  expect((await chooser).isMultiple()).toBe(true)
  const hints = open.locator('xpath=../..').locator(':scope > p.sub')
  expect(await hints.count()).toBeGreaterThanOrEqual(2)
  const hintStyle = (el: Element) => [getComputedStyle(el).font, getComputedStyle(el).color, getComputedStyle(el).lineHeight]
  expect(await hints.last().evaluate(hintStyle)).toEqual(await hints.first().evaluate(hintStyle))
  await open.click()
  const picker = page.getByRole('dialog',{name:temporary?'从资料库添加附件':'从资料库引用'})
  await expect(picker.getByRole('checkbox',{name:'选择 已有文件.pdf'})).toBeDisabled()
  await picker.getByRole('button',{name:'报告',exact:true}).click()
  await picker.getByRole('checkbox',{name:'选择 会议纪要.pdf'}).check()
  await expect(picker.getByRole('cell',{name:/1.5.* MB/})).toBeVisible()
  await picker.getByRole('button',{name:'上一级'}).click()
  await expect(picker.getByRole('button',{name:'移除 会议纪要.pdf'})).toBeVisible()
  await expect(picker.getByRole('checkbox',{name:'选择 已有文件.pdf'})).toBeVisible()
  await capture(page,'library-picker-'+id)
  const viewport = page.viewportSize()
  if (id === 'ui-chat-1' && viewport) {
    await page.setViewportSize({width:390,height:740})
    await expect.poll(() => picker.evaluate(el => el.getBoundingClientRect().right <= innerWidth && el.scrollWidth <= el.clientWidth + 1)).toBe(true)
    await capture(page,'library-picker-mobile')
    await page.setViewportSize(viewport)
  }
  await picker.getByRole('button',{name:temporary?'添加所选':'引用所选'}).click()
  await expect(picker).toBeHidden()
  expect(submitted).toEqual({source:'profile',id:'sample',temporary})
}

async function login(page: Page, management = false) {
  await page.goto(management ? '/admin' : '/')
  await page.locator('input[name=username]').fill(management ? 'admin' : 'browser-user')
  await page.locator('input[name=password]').fill(management ? 'browser-admin-password' : 'browser-user-password')
  await page.locator('form').filter({ has: page.locator('input[name=password]') }).getByRole('button', { name: /登录/ }).click()
  await expect(page.locator('.shell')).toBeVisible()
}

async function capture(page: Page, name: string) {
  if (!screenshots) return
  const dir = resolve(screenshots, stage)
  mkdirSync(dir, { recursive: true })
  await page.waitForLoadState('networkidle', {timeout: 3000}).catch(() => {})
  if (name.startsWith('acs-')) await page.locator('#acs-editor').evaluate(node => node.scrollIntoView({ block: 'start' }))
  await page.screenshot({ path: resolve(dir, name + '.png'), animations: 'disabled' })
}

test('walk every personal sidebar item and avatar tab', async ({ page }) => {
  await login(page)
  const nav = page.getByRole('navigation', { name: '主导航' })
  for (const key of personal) {
    const button = nav.locator(`[data-nav="${key}"]`)
    await button.click()
    await expect(button).toHaveClass(/active/)
    await capture(page, 'personal-' + key)
  }
  await nav.locator('[data-nav="avatar"]').click()
  const hub = page.getByRole('navigation', { name: '我的分身功能' })
  for (const [index, key] of tabs.entries()) {
    const button = hub.getByRole('button').nth(index)
    await button.click()
    await expect(button).toHaveClass(/active/)
    await expect(page.locator('main.content .avatar-panel')).toBeVisible()
    await capture(page, 'hub-' + key)
  }
})

test('walk every management sidebar item and switch consoles', async ({ page }) => {
  await login(page, true)
  const nav = page.getByRole('navigation', { name: '主导航' })
  for (const key of admin) {
    const button = nav.locator(`[data-nav="${key}"]`)
    await button.click()
    await expect(button).toHaveClass(/active/)
    await capture(page, 'admin-' + key)
  }
  await page.getByRole('button', { name: '账户与帮助' }).click()
  await expect(page.getByRole('menuitemcheckbox', { name: 'Debug 模式' })).toHaveCount(0)
  await expect(page.locator('dialog.workspace-guide')).toHaveCount(0)
  await capture(page, 'admin-more')
  await page.getByRole('menuitem', { name: '账户设置' }).click()
  await expect(page.getByRole('heading', { name: '账户设置' })).toBeVisible()
  await capture(page, 'admin-account')
  await page.reload()
  await expect(page.getByRole('heading', { name: '账户设置' })).toBeVisible()
  await page.getByRole('button', { name: '账户与帮助' }).click()
  await page.getByRole('menuitem', { name: '个人中心' }).click()
  await expect(nav.getByRole('button', { name: '我的工作台' })).toBeVisible()
})

test('more menu, theme, help, messages, resize and fullscreen', async ({ page }) => {
  await login(page)
  const more = page.getByRole('button', { name: '账户与帮助' })
  await more.focus()
  await page.keyboard.press('ArrowDown')
  await expect(page.getByRole('menuitem', { name: '退出登录' })).toBeFocused({ timeout: 1200 })
  await capture(page, 'personal-more')
  await page.keyboard.press('Escape')
  await expect(more).toBeFocused()
  await expect(more).toHaveAttribute('aria-expanded', 'false')
  await more.click()
  await page.getByRole('button', { name: /主题/ }).last().click()
  await expect(page.getByRole('menuitemradio', { name: /跟随系统/ }).last()).toBeFocused()
  await page.keyboard.press('End')
  await expect(page.getByRole('menuitemradio', { name: /深色/ }).last()).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await capture(page, 'dark-theme')
  await page.getByRole('menuitem', { name: '帮助文档' }).click()
  const preview = page.getByRole('dialog').filter({ hasText: '用户操作手册' }).last()
  await expect(preview).toBeVisible()
  await expect(preview.getByRole('heading', { name: '第一次使用' })).toBeVisible()
  await preview.getByRole('button', { name: '与自己的分身对话', exact:true }).click()
  await expect(preview.getByRole('heading', { name: '指定这次对话要参考的文件' })).toBeVisible()
  await capture(page, 'help-window')
  await page.keyboard.press('Escape')
  await expect(preview.locator('details[open]')).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(preview).toBeHidden()
  const bell = page.getByRole('button', { name: '查看消息' })
  await bell.click()
  const window = page.getByRole('dialog', { name: '消息通知' })
  await expect(window).toBeVisible()
  await capture(page, 'messages-window')
  const resize = window.getByRole('button', { name: '从左上角调整窗口大小' })
  await expect(window).not.toHaveClass(/avatar-pop-enter-active/)
  const before = (await window.boundingBox())!
  await resize.focus()
  await page.keyboard.press('ArrowRight')
  await expect.poll(async () => (await window.boundingBox())?.width).toBeLessThan(before.width)
  const handle = (await resize.boundingBox())!
  const frame = (await window.boundingBox())!
  expect(Math.abs(handle.x - frame.x)).toBeLessThan(4)
  expect(Math.abs(handle.y - frame.y)).toBeLessThan(4)
  expect(frame.x + frame.width).toBeCloseTo(before.x + before.width, 0)
  expect(frame.y + frame.height).toBeCloseTo(before.y + before.height, 0)
  const keyboardWidth = frame.width
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2)
  await page.mouse.down()
  await page.mouse.move(handle.x + handle.width / 2 + 30, handle.y + handle.height / 2 + 20, { steps: 3 })
  await page.mouse.up()
  await expect.poll(async () => (await window.boundingBox())?.width).toBeLessThan(keyboardWidth)
  const resized = (await window.boundingBox())!
  expect(resized.x + resized.width).toBeCloseTo(before.x + before.width, 0)
  expect(resized.y + resized.height).toBeCloseTo(before.y + before.height, 0)
  expect(frame.width - resized.width).toBeCloseTo(30, 0)
  expect(frame.height - resized.height).toBeCloseTo(20, 0)
  const resizedWidth = resized.width
  await window.getByRole('button', { name: '全屏' }).click()
  await expect(window.getByRole('button', { name: '还原小窗' })).toBeVisible()
  await capture(page, 'messages-fullscreen')
  await window.getByRole('button', { name: '还原小窗' }).click()
  expect(await window.evaluate(node => node.getAnimations().some(a => !(a instanceof CSSAnimation)))).toBe(true)
  await expect.poll(async () => Math.round((await window.boundingBox())!.width)).toBe(Math.round(resizedWidth))
  const restored = (await window.boundingBox())!
  expect(restored.x + restored.width).toBeCloseTo(before.x + before.width, 0)
  expect(restored.y + restored.height).toBeCloseTo(before.y + before.height, 0)
  await page.keyboard.press('Escape')
  await expect(window).toBeHidden()
})

test('navigation animates and respects reduced motion', async ({ page }) => {
  await login(page)
  await page.evaluate(() => {
    document.addEventListener('animationstart', event => {
      if (event.animationName === 'avatar-view-in') document.body.dataset.viewAnimation = event.animationName
    })
  })
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '我的分身' }).click()
  await expect(page.locator('body')).toHaveAttribute('data-view-animation', 'avatar-view-in')
  await page.evaluate(() => { delete document.body.dataset.viewAnimation })
  await page.getByRole('button', { name: '资料文件', exact: true }).click()
  await expect(page.locator('body')).toHaveAttribute('data-view-animation', 'avatar-view-in')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.evaluate(() => { delete document.body.dataset.viewAnimation })
  await page.getByRole('button', { name: '模型配置', exact: true }).click()
  const modelPanel = page.locator('.avatar-panel > div').filter({hasText:'聊天模型配置'})
  await expect(modelPanel).toBeVisible()
  await expect(modelPanel).toHaveCSS('animation-name', 'none')
  await expect(page.locator('body')).not.toHaveAttribute('data-view-animation', 'avatar-view-in')
})

test('peer conversation opens as a window with task controls and wrapped message content', async ({page}) => {
  await page.route('**/api/agent-sessions?**', route => route.fulfill({json:{items:[{session_id:'fixture-session',peer_name:'示例分身'}],total:1,offset:0}}))
  await page.route(url => url.pathname === '/api/agent-sessions/fixture-session', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('task_offset'))
    return route.fulfill({json:{
      peer_name:'示例分身',resource_conversation_id:'peer-fixture-files',
      messages:[{turn:1,dialogue_id:'fixture-message',speaker_name:'示例分身',speaker_role:'agent',content:'这是一条用来验证小窗排版的消息。',is_own:false}],
      task_page:{items:[{id:'task-'+offset,objective:'示例任务 '+offset,status:'running'}],total:11,offset},
      tasks:[],has_more:false,
    }})
  })
  await page.route('**/api/conversations/peer-fixture-files/files', route => route.fulfill({ json: [] }))
  await page.route('**/api/conversations/peer-fixture-files/tools', route => route.fulfill({ json: { items: [{id:'fixture',name:'示例工具',enabled:true}] } }))
  await login(page)
  await page.locator('nav.nav [data-nav=avatar]').click()
  await page.getByRole('navigation',{name:'我的分身功能'}).getByRole('button',{name:'分身会话'}).click()
  await page.getByRole('button',{name:'与 示例分身 的对话'}).click()
  const dialog = page.getByRole('dialog',{name:'分身间会话'})
  await expect(dialog).toContainText('这是一条用来验证小窗排版的消息。')
  expect(await dialog.evaluate(node=>getComputedStyle(node).position)).toBe('fixed')
  const tasks = dialog.getByRole('button',{name:'任务侧栏'})
  await tasks.click()
  await expect(tasks).toHaveAttribute('aria-expanded','true')
  await expect(dialog.getByRole('button',{name:'主会话'})).toBeVisible()
  await expect(dialog.getByText('示例任务 0')).toBeVisible()
  await expect(dialog.locator('.peer-task-actions').getByRole('button',{name:'删除全部任务记录'})).toBeVisible()
  const pager = dialog.getByRole('navigation',{name:'任务分页'})
  expect((await pager.locator('span').first().boundingBox())!.y).toBeGreaterThan((await pager.getByRole('spinbutton').boundingBox())!.y)
  await dialog.getByRole('navigation', {name:'任务分页'}).getByRole('button', {name:'下一页'}).click()
  await expect(dialog.getByText('示例任务 10')).toBeVisible()
  await capture(page,'peer-window')
  await dialog.locator('summary').filter({hasText:'工具'}).click()
  await expect(dialog.getByText('示例工具', {exact:true})).toBeVisible()
  await capture(page,'peer-tools')
  await dialog.locator('summary').filter({hasText:'技能'}).click()
  await expect(dialog.getByText('示例工具', {exact:true})).toBeHidden()
  await dialog.locator('summary').filter({hasText:'技能'}).click()
  await dialog.locator('summary').filter({hasText:'文件'}).click()
  await checkLibraryPicker(page,'peer-fixture-files')
  const close = dialog.getByRole('button', {name:'关闭', exact:true})
  await close.hover()
  await expect(close).toHaveCSS('background-color','rgb(196, 43, 28)')
  expect(await close.textContent()).toBe('')
  await expect(dialog.getByRole('button',{name:'还原小窗'})).toBeVisible()
  await capture(page,'peer-fullscreen')
  await dialog.getByRole('button',{name:'还原小窗'}).click()
  await dialog.evaluate(node => Promise.allSettled(node.getAnimations().map(animation => animation.finished)))
  const before = (await dialog.boundingBox())!
  const handle = dialog.getByRole('button', { name: '从左上角调整窗口大小' })
  await handle.press('Shift+ArrowRight')
  await handle.press('ArrowDown')
  const after = (await dialog.boundingBox())!
  expect(after.x + after.width).toBeCloseTo(before.x + before.width, 0)
  expect(after.y + after.height).toBeCloseTo(before.y + before.height, 0)
  expect(before.width - after.width).toBeCloseTo(50, 0)
  expect(before.height - after.height).toBeCloseTo(10, 0)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('button',{name:'与 示例分身 的对话'})).toBeFocused()
})

test('chat groups matching references and opens authorized source previews', async ({page}) => {
  await page.route(url=>url.pathname==='/api/chat',route=>route.fulfill({json:[{
    id:42,role:'assistant',created_at:'2026-11-18T00:00:00Z',responder_name:'测试分身',content:'以下是资料中的信息。\n\n> 引用来源：\n> 对照资料.txt · RAG  \n> 对照资料.txt · 源文件  \n',
    attachments:[{id:'fixture-file',filename:'对照资料.txt',download_url:'/api/fixture-source'}],
    used_skills:[{id:'fixture-skill',name:'摘要'}],
  }]}))
  await page.route('**/api/fixture-source',route=>route.fulfill({contentType:'text/plain',body:'测试原件正文'}))
  await login(page)
  await page.goto('/chat')
  await expect(page.locator('.reply-source')).toHaveText('测试分身')
  await expect(page.locator('.stream-note').first()).toContainText('本轮技能：摘要')
  await page.getByRole('button',{name:'1 份资料'}).click()
  const sources = page.getByRole('dialog',{name:'引用来源'})
  await expect(sources).toContainText('RAG / 源文件 / 会话文件')
  await capture(page,'chat-sources')
  await sources.getByRole('button',{name:/对照资料/}).click()
  await expect(page.getByRole('dialog').filter({hasText:'测试原件正文'})).toBeVisible()
  await capture(page,'chat-source-preview')
})

test('files keep a compact pager and a readable table on desktop and mobile', async ({ page }) => {
  const first = '测试资料'.repeat(14) + '.txt'
  const files = Array.from({ length: 12 }, (_, index) => {
    const filename = index === 0 ? first : `资料-${index + 1}.txt`
    return { id: 'file-' + index, filename, relative_path: filename, caption: '', visibility: 'private', size: 2048 }
  })
  await page.route('**/api/me/attachments', route => route.fulfill({ json: files }))
  await page.route('**/api/me/material-folders', route => route.fulfill({ json: { items: ['示例文件夹'], captions: {}, visibility: {} } }))
  await page.route('**/api/me/file-index?source=profile', route => route.fulfill({ json: {
    configured: true,
    items: files.map(file => ({ file_id: file.id, source: 'profile', status: 'ready', coverage: 'native_text' })),
    summary: { profile: { total: files.length, completed: files.length, remaining: 0 } },
  } }))
  await login(page)
  await page.goto('/files')
  const rows = page.locator('.material-file-table tbody tr')
  const pager = page.getByRole('navigation', { name: '文件分页' })
  await expect(rows).toHaveCount(10)
  const firstFile = rows.filter({hasText: first})
  await expect(firstFile.getByRole('checkbox')).toBeVisible()
  await expect(firstFile).toContainText('私密')
  await expect(firstFile).toContainText('已就绪')
  await firstFile.getByRole('button').click()
  await expect(page.locator('.material-inspector')).toBeVisible()
  const jump = pager.getByRole('spinbutton')
  expect((await jump.boundingBox())?.width).toBeLessThan(70)
  await capture(page, 'files-desktop')
  await pager.scrollIntoViewIfNeeded()
  await capture(page, 'files-pagination-desktop')
  await page.getByRole('checkbox', { name: '选择本页' }).check()
  await pager.getByRole('button', { name: '下一页' }).click()
  await expect(rows).toHaveCount(3)
  await expect(pager.getByRole('button', { name: '下一页' })).toBeDisabled()
  await jump.fill('1')
  await jump.press('Enter')
  await expect(firstFile.getByRole('checkbox')).toBeChecked()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  expect((await jump.boundingBox())?.width).toBeLessThan(70)
  await expect(firstFile.locator('.file-list-name')).toHaveCSS('-webkit-line-clamp','2')
  await capture(page, 'files-mobile')
  await pager.scrollIntoViewIfNeeded()
  await capture(page, 'files-pagination-mobile')
  await pager.getByRole('button', { name: '搜索文件' }).click()
  const search = pager.getByRole('searchbox', {name: '搜索文件内容'})
  await search.fill('资料-12')
  await search.press('Enter')
  await expect(rows).toHaveCount(1)
  await expect(pager).toContainText('第 1 / 1 页')
})

test('opening a notification preserves its detail window', async ({ page }) => {
  const notice = { id: 501, title: '资料审核提示', content: '审核已完成，请查看资料。', kind: 'review', read_at: null, created_at: '2026-11-18T00:00:00Z' }
  await page.route('**/api/notifications?**', route => route.fulfill({ json: { items: [notice], total: 1, offset: 0, unread: 1 } }))
  await page.route('**/api/notifications/501/read', route => route.fulfill({ json: {} }))
  await login(page)
  await page.getByRole('button', { name: '查看消息' }).click()
  const hub = page.getByRole('dialog', { name: '消息通知' })
  await hub.getByRole('button', { name: /资料审核提示/ }).click()
  const detail = page.getByRole('dialog', { name: '资料审核提示' })
  await expect(detail).toContainText(notice.content)
  await capture(page, 'notification-detail')
  await page.keyboard.press('Escape')
  await expect(detail).toBeHidden()
  await expect(hub).toBeVisible()
})

test('file address opens nested folders and preserves Explorer navigation', async ({ page }) => {
  await page.route('**/api/me/attachments', route => route.fulfill({ json: [
    { id: 'image-1', filename: '示例.png', relative_path: '图像/相册/示例.png', caption: '', visibility: 'private', size: 1024 },
    { id: 'doc-1', filename: '说明.docx', relative_path: '说明.docx', caption: '', visibility: 'private', size: 1024 },
  ] }))
  await page.route('**/api/me/material-folders', route => route.fulfill({ json: { items: ['图像', '图像/相册'], captions: {}, visibility: {} } }))
  await login(page)
  await page.goto('/files')
  const address = page.getByRole('navigation', { name: '文件夹路径' })
  const table = page.locator('.material-file-table')
  await expect(table.locator('svg.material-type-icon text')).toHaveText('DOC')
  await expect(table.locator('svg.material-type-icon[viewBox="0 0 40 36"]')).toBeVisible()
  await table.getByRole('button', { name: '图像' }).click()
  await table.getByRole('button', { name: '相册' }).click()
  await expect(address.getByText('相册')).toHaveAttribute('aria-current', 'location')
  await expect(address.getByRole('link', { name: '图像' })).toBeVisible()
  await expect(address.getByRole('button')).toHaveCount(0)
  await expect(table.locator('svg.material-type-icon text')).toHaveText('IMG')
  await capture(page, 'files-explorer-path')
  await page.getByRole('button', { name: '后退' }).click()
  await expect(address.getByText('图像')).toHaveAttribute('aria-current', 'location')
  await page.getByRole('button', { name: '前进' }).click()
  await expect(address.getByText('相册')).toHaveAttribute('aria-current', 'location')
  await page.getByRole('button', { name: '上一级' }).click()
  await address.getByRole('link', { name: '根目录' }).click()
  await expect(address.getByText('根目录')).toHaveAttribute('aria-current', 'location')
  await page.getByRole('button', { name: '编辑文件路径' }).click()
  const path = page.getByRole('textbox', { name: '文件路径' })
  await path.fill('/不存在')
  await path.press('Enter')
  await expect(page.getByRole('alert', { name: '' }).filter({ hasText: '目录不存在' })).toBeVisible()
  await path.fill('/图像/相册')
  await path.press('Enter')
  await expect(address.getByText('相册')).toHaveAttribute('aria-current', 'location')
  await page.reload()
  await expect(address.getByText('相册')).toHaveAttribute('aria-current', 'location')
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await capture(page, 'files-explorer-mobile')
})

test('chat displays history when a generation finishes during reload', async ({ page }) => {
  let committed = false
  await page.route('**/api/chat/pending?**', route => {
    committed = true
    return route.fulfill({ json: { request_id: 'finished', done: true, state: 'succeeded' } })
  })
  await page.route(url => url.pathname === '/api/chat', route => route.fulfill({
    json: committed ? [{ id: 91, role: 'assistant', content: '刷新时保存的回答' }] : [],
  }))
  await login(page)
  await page.goto('/chat')
  await expect(page.getByText('刷新时保存的回答')).toHaveCount(1)
})

test('ACS uses configuration tabs and a structured preview for draft and inherited documents', async ({ page }) => {
  const acsInput = {
    name: '示例分身', description: '示例能力说明', version: '1.0.0', icon_url: '', documentation_url: '', location: '',
    provider: { country_code: 'CN', organization: '示例学校', department: '', name: '示例组织', url: '', license: '', email: '', domain_registrations: [] },
    default_input_modes: ['text/plain'], default_output_modes: ['text/plain'],
    skills: [{ id:'personal-profile.introduction.qa', name:'资料问答', description:'**核对资料**的示例技能', version:'1.0.0', tags:[], examples:[], input_modes:['text/plain'], output_modes:['text/plain'] }],
  }
  const acsDocument = {
    ...acsInput, entityMeta:{environment:'1.0.0'}, certificate:{requestedValidity:49,altNames:{dns:['example.test'],ip:['192.0.2.10']}},
    aic:'sample-aic', active:true, protocolVersion:'02.02', entityUserId:'fixture', endPoints:[{url:'https://example.test/aip/rpc',transport:'JSONRPC',security:[{mtls:[]}]}],
  }
  let inherited = false
  await page.route(url => url.pathname === '/api/me', async route => {
    const response = await route.fetch()
    const data = await response.json()
    if (!data.profile) return route.fulfill({ response })
    return route.fulfill({ response, json: { ...data, profile: { ...data.profile, acs_input:acsInput, acs_document:acsDocument, acs_online:false,
      acs_inheritance: { enabled: inherited, registered: inherited, document: acsDocument } } } })
  })
  await page.route('**/api/me/acs/preview', route => route.fulfill({ json: { document:acsDocument, validation:{valid:true,errors:[]} } }))
  await login(page)
  await page.goto('/profile')
  const open = () => page.getByRole('button', {name:'ACS 配置'}).click({ timeout:8000 })
  await open()
  const acs = page.locator('#acs-editor')
  await expect(acs.getByRole('tab', {name:'基础信息'})).toHaveAttribute('aria-selected','true')
  await expect(acs.locator('.acs-preview')).toContainText('示例学校')
  await expect(acs.getByLabel('完整 ACS JSON 预览')).toBeVisible()
  await expect(acs.getByLabel('完整 ACS JSON 预览')).toContainText('"name": "示例分身"')
  await capture(page,'acs-basic')
  const deployment=acs.locator('.acs-deployment')
  const disclosure=deployment.locator('summary')
  await expect(disclosure).toHaveCSS('list-style-type','none')
  await disclosure.focus()
  await page.keyboard.press('Space')
  await expect(deployment).toHaveAttribute('open','')
  await expect(deployment).toContainText('mTLS（双向 TLS）')
  await expect(deployment).toContainText('49 天')
  await expect(deployment).toContainText('https://example.test/aip/rpc')
  await deployment.evaluate(node=>node.scrollIntoView({block:'center'}))
  await capture(page,'deployment-fields')
  await acs.getByRole('tab', {name:/技能定义/}).click()
  await expect(acs.getByRole('textbox', {name:'技能名称 *'})).toBeDisabled()
  await expect(acs.getByRole('button', {name:'添加技能'})).toBeVisible()
  await capture(page,'acs-skills')
  await acs.getByRole('tab', {name:'提供者'}).click()
  await expect(acs.getByRole('textbox', {name:'学校 / 组织'})).toHaveValue('示例学校')
  await capture(page,'acs-provider')
  inherited = true
  await page.reload()
  await open()
  await expect(acs.getByText('正式派生身份')).toBeVisible()
  await expect(acs.locator('strong').filter({hasText:'核对资料'})).toBeVisible()
  await expect(acs.getByRole('tab')).toHaveCount(0)
  await expect(acs.getByRole('link', {name:'下载 ACS'})).toBeVisible()
  await capture(page,'acs-inherited')
})

test('file previews render CSV and JSON while keeping source view available', async ({ page }) => {
  const data = [
    { id:'csv-1', filename:'示例.csv', relative_path:'示例.csv', caption:'', visibility:'private', size:80 },
    { id:'json-1', filename:'示例.json', relative_path:'示例.json', caption:'', visibility:'private', size:80 },
  ]
  await page.route('**/api/me/attachments', route => route.fulfill({ json:data }))
  await page.route('**/api/me/attachments/csv-1', route => route.fulfill({ contentType:'text/csv', body:'姓名,备注\r\n测试用户,"逗号, 和 ""引号"""\r\n' }))
  await page.route('**/api/me/attachments/json-1', route => route.fulfill({ json:{ name:'测试用户',provider:{ organization:'示例学校' } } }))
  await login(page)
  await page.goto('/files')
  await page.getByRole('button', {name:'示例.csv'}).click()
  await page.locator('.material-inspector').getByRole('button', {name:'预览'}).click()
  const csv = page.getByRole('dialog', {name:'示例.csv'})
  await expect(csv.getByRole('columnheader', {name:'姓名'})).toBeVisible()
  await expect(csv.getByRole('cell', {name:'逗号, 和 "引号"'})).toBeVisible()
  await capture(page,'preview-csv')
  await csv.getByRole('button', {name:'查看源码'}).click()
  await expect(csv.locator('pre')).toContainText('姓名,备注')
  await page.keyboard.press('Escape')
  await expect(csv).toBeHidden()
  await page.getByRole('button', {name:'示例.json'}).click()
  await page.locator('.material-inspector').getByRole('button', {name:'预览'}).click()
  const json = page.getByRole('dialog', {name:'示例.json'})
  await expect(json.locator('.document-fields').first()).toContainText('示例学校')
  await capture(page,'preview-json')
})

test('create task opens an editable form with an empty composer', async ({ page }) => {
  let draftBody: Record<string, unknown> | undefined
  let confirmation: Record<string, unknown> | undefined
  let cancelled = false
  await page.route('**/api/agent-sessions?**', route => route.fulfill({ json: { items:[{session_id:'task-session',peer_name:'任务分身'}],total:1,offset:0 } }))
  await page.route('**/api/agent-sessions/task-session', route => route.fulfill({ json: { peer_name:'任务分身',resource_conversation_id:'',messages:[],tasks:[],has_more:false } }))
  await page.route('**/api/agent-sessions/task-session/tasks?**', route => route.fulfill({ json: { items:[],total:0,offset:0 } }))
  await page.route('**/api/agent-sessions/task-session/task-draft', route => {
    draftBody = route.request().postDataJSON()
    return route.fulfill({ json: { id:'draft-1', conversation_id:'draft-workspace', target_query:'任务分身', target_aic:'peer-demo', objective:'',shared_content:'',delivery_mode:'negotiation' } })
  })
  await page.route('**/api/agent-intents/draft-1/targets?**', route => route.fulfill({ json: { items:[{aic:'peer-demo',name:'任务分身'}],total:1,offset:0 } }))
  await page.route('**/api/agent-intents/draft-1/confirm', route => {
    confirmation = route.request().postDataJSON()
    return route.fulfill({ json: { id:'task-created', session_id:'task-session' } })
  })
  await page.route('**/api/agent-dialogues/task-created', route => route.fulfill({
    json: { peer_name:'任务分身',resource_conversation_id:'',messages:[],tasks:[],has_more:false,
      task:{id:'task-created',objective:'整理三条会议行动项',status:'running',rounds_done:0,round_limit:10} },
  }))
  await page.route('**/api/agent-intents/draft-1/cancel', route => { cancelled = true; return route.fulfill({ json: {} }) })
  await page.route('**/api/conversations/draft-workspace/files', route => route.fulfill({ json: [] }))
  await page.route('**/api/conversations/draft-workspace/tools', route => route.fulfill({ json: { items:[] } }))
  await page.route('**/api/me/skills',route=>route.fulfill({json:[{id:'ready',name:'撰写技能',enabled:true},{id:'disabled',name:'待启用技能',enabled:false}]}))
  await login(page)
  await page.goto('/dialogues')
  await page.getByRole('button', { name:'与 任务分身 的对话' }).click()
  const dialogue = page.getByRole('dialog', { name:'分身间会话' })
  await expect(dialogue.getByRole('textbox', { name:'分身会话消息' })).toHaveValue('')
  await dialogue.getByRole('button', { name:'任务侧栏' }).click()
  await dialogue.getByRole('button', { name:/创建任务/ }).click()
  const form = page.getByRole('dialog', { name:'发起分身任务' })
  const objective = form.getByRole('textbox', { name:'任务或消息内容' })
  await expect(objective).toBeEditable()
  await expect(objective).toHaveValue('')
  expect(draftBody).toMatchObject({ message:'', request_id:expect.any(String) })
  const tools = form.getByRole('group',{name:'聊天工具栏'})
  await expect(tools.locator('summary > span:not([aria-hidden]):not(.app-icon)')).toHaveText(['技能','文件','工具'])
  await tools.locator('summary').filter({hasText:'技能'}).click()
  await expect(tools.getByText('撰写技能',{exact:true})).toBeVisible()
  await expect(tools.getByText('待启用技能')).toBeVisible()
  await tools.locator('summary').filter({hasText:'文件'}).click()
  await expect(tools.getByText('上传本地文件')).toBeVisible()
  await expect(tools.getByRole('button',{name:'从资料库添加'})).toBeVisible()
  await capture(page,'peer-create-task-files')
  await checkLibraryPicker(page,'draft-workspace',true)
  await tools.locator('summary').filter({hasText:'文件'}).click()
  await expect(tools.getByText('会议纪要.pdf')).toBeVisible()
  await expect(tools.getByRole('button',{name:'移除附件'})).toBeVisible()

  await objective.fill('标准知识图谱\n\n```mermaid\ngraph TD\nA --> B\n```')
  await form.locator('.actions').first().getByRole('button',{name:'预览'}).click()
  await expect(form.locator('.mermaid-card').getByRole('status')).toContainText('绘图完成',{timeout:10000})
  await form.getByRole('button',{name:'源码编辑'}).click()
  await objective.fill('整理三条会议行动项')
  await capture(page, 'peer-create-task')
  expect(confirmation).toBeUndefined()
  await page.keyboard.press('Escape')
  await expect(form).toBeHidden()
  await expect(dialogue).toBeVisible()
  expect(cancelled).toBe(true)
  expect(confirmation).toBeUndefined()
  await dialogue.getByRole('button', { name:/创建任务/ }).click()
  await expect(form.getByRole('button', {name:'确认发送'})).toBeDisabled()
  await objective.fill('整理三条会议行动项')
  await form.getByRole('button', {name:'确认发送'}).click()
  await expect(form).toBeHidden()
  await expect(dialogue.getByText(/整理三条会议行动项/)).toBeVisible()
  expect(confirmation).toMatchObject({target_aic:'peer-demo',objective:'整理三条会议行动项'})

})

test('Mermaid renders a completed fenced diagram in a chat bubble', async ({page}) => {
  await page.route(url=>url.pathname==='/api/chat',route=>route.fulfill({ json: [{
    id:304, role:'assistant', created_at:'2026-11-18T03:00:00Z',
    content:'下面是流程图。\n\n```mermaid\nflowchart TD\n  ' + Array.from({length:22}, (_, i) => 'N'+i).join(' --> ') + '\n```\n\n请查看。',
  }] }))
  await login(page)
  await page.goto('/chat')
  const diagram=page.locator('.bubble .mermaid-card')
  await expect(diagram).toBeVisible()
  await expect(diagram.getByRole('status')).toContainText('绘图完成',{timeout:8000})
  await expect(diagram.frameLocator('iframe').locator('svg')).toBeVisible()
  const frame = diagram.frameLocator('iframe')
  const viewport = diagram.locator('.diagram-viewport')
  await expect.poll(() => viewport.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true)
  expect(await frame.locator('html').evaluate(el => el.scrollHeight <= el.clientHeight + 1)).toBe(true)
  await expect(diagram.getByText('打开图表预览',{exact:true})).toHaveCount(0)
  const retry = diagram.getByRole('button', {name:'重试绘图'})
  await expect(retry.locator('svg')).toBeVisible()
  const retryBox = (await retry.boundingBox())!
  const zoomBox = (await diagram.getByRole('group',{name:'预览缩放'}).boundingBox())!
  expect(retryBox.x + retryBox.width).toBeLessThan(zoomBox.x)
  expect(Math.abs(retryBox.y - zoomBox.y)).toBeLessThan(3)
  await retry.click()
  await expect(diagram.getByRole('status')).toContainText('绘图完成')
  const more = diagram.locator('summary[aria-label="更多绘图操作"]')
  await expect(diagram.getByRole('button',{name:'转存资料'})).toBeHidden()
  await more.click()
  await expect(diagram.getByRole('button',{name:'转存资料'})).toBeVisible()
  await expect(diagram.getByRole('button',{name:'下载代码（.mmd）'})).toBeVisible()
  await expect(diagram.getByRole('button',{name:'下载 SVG 图片'})).toBeVisible()
  await expect(diagram.getByRole('button',{name:'下载 PNG 图片'})).toBeVisible()
  await expect(diagram.getByRole('button',{name:'下载 PNG 图片'})).toHaveCSS('border-top-width','0px')
  await more.click()
  await capture(page,'chat-mermaid')
  await diagram.frameLocator('iframe').locator('svg').click()
  const preview = page.getByRole('dialog', {name:'Mermaid 图表.png'})
  await expect(preview.getByRole('img')).toBeVisible()
  const zoom = preview.getByRole('group', {name:'预览缩放'})
  await zoom.getByRole('button',{name:'100%',exact:true}).click()
  const width = () => preview.getByRole('img').evaluate(img => img.getBoundingClientRect().width)
  const original = await width()
  await zoom.getByRole('button',{name:'放大预览'}).click()
  await expect.poll(width).toBeCloseTo(original * 1.25,0)
  const animated = await preview.getByRole('button', {name:'关闭预览'}).evaluate(async button => {
    const overlay = button.closest('.el-overlay')!
    if (!(button instanceof HTMLButtonElement)) return false
    button.click()
    await new Promise(requestAnimationFrame)
    return overlay.isConnected && overlay.getAnimations({subtree:true}).length > 0
  })
  expect(animated).toBe(true)
  await expect(preview).toBeHidden()
})

test('personal composer keeps session settings separate and shortcuts only affect the picker', async ({page}) => {
  const catalog=[{id:'active',name:'已加载技能',description:'当前会话使用中',enabled:true},{id:'other',name:'另一项技能',enabled:true}]
  let toggled:unknown, skillMutation=false
  await page.route('**/api/conversations?**', route=>route.fulfill({json:{items:[{id:'ui-chat-1',title:'测试会话',message_count:1}],default_id:'ui-chat-1'}}))
  await page.route('**/api/me/skills', route=>route.fulfill({json:catalog}))
  await page.route('**/api/conversations/ui-chat-1/skills**', route=>{
    if(route.request().method()==='PATCH')skillMutation=true
    return route.fulfill({json:[{...catalog[0],installed_enabled:true}]})
  })
  await page.route('**/api/conversations/ui-chat-1/style-learning', route=>route.fulfill({json:{enabled:false}}))
  await page.route('**/api/conversations/ui-chat-1/files', route=>route.fulfill({json:[]}))
  await page.route('**/api/conversations/ui-chat-1/tools', route=>{
    if(route.request().method()==='PATCH')toggled=route.request().postDataJSON()
    return route.fulfill({json:{items:[{id:'sample_tool',name:'示例工具',description:'示例说明',enabled:!!toggled}]}})
  })
  await page.route(url=>url.pathname==='/api/chat',route=>route.fulfill({json:[{id:1,role:'assistant',responder_name:'示例分身',content:'一行回复。'}]}))
  await login(page)
  await page.goto('/chat')
  const bar=page.getByRole('group',{name:'聊天工具栏'})
  await expect(bar.locator('summary > span:not([aria-hidden]):not(.app-icon)')).toHaveText(['技能','文件','工具'])
  const card=page.locator('#personal-chat-card')
  const box=await card.boundingBox(), select=await card.locator('.chat-session-select').boundingBox()
  expect(select!.width).toBeLessThan(box!.width/2)
  expect(await card.locator('.bubble').evaluate(node=>Number.parseFloat(getComputedStyle(node).paddingTop))).toBeLessThan(10)
  await capture(page,'chat-compact')
  await card.getByLabel('会话操作',{exact:true}).click()
  await card.locator('summary').filter({hasText:'表达风格学习'}).click()
  await expect(card.getByRole('checkbox',{name:'自动学习本人表达'})).toBeVisible()
  await capture(page,'chat-style-learning')
  const skills=bar.locator('summary').filter({hasText:'技能'})
  await skills.click()
  await expect(bar.getByText('已加载技能',{exact:true})).toBeVisible()
  await expect(bar.getByText('另一项技能',{exact:true})).toHaveCount(0)
  expect(await skills.evaluate(async element => {
    const details = element.closest('details')!
    const frame = () => new Promise<void>(done => requestAnimationFrame(() => done()))
    const opacity = () => Number(getComputedStyle(details,'::details-content').opacity)
    while (opacity() < 1) await frame()
    async function toggle() {
      (element as HTMLElement).click()
      for (let n=0; n<30; n++) { await frame(); if (opacity()>0 && opacity()<1) return true }
      return false
    }
    const closing = await toggle()
    while (opacity() > 0) await frame()
    return closing && await toggle()
  })).toBe(true)
  await bar.locator('summary').filter({hasText:'文件'}).click()
  await expect(bar.getByText('上传本地文件')).toBeVisible()
  await expect(bar.getByRole('button',{name:'从资料库引用'})).toBeVisible()
  await expect(bar.getByText('已加载技能',{exact:true})).toBeHidden()
  await capture(page,'chat-toolbar-files')
  await checkLibraryPicker(page,'ui-chat-1')
  await bar.locator('summary').filter({hasText:'工具'}).click()
  await bar.getByRole('checkbox',{name:/示例工具/}).check()
  await expect.poll(()=>toggled).toEqual({tools:{sample_tool:true}})
  const input=page.getByRole('textbox',{name:'聊天消息'})
  await input.fill('$')
  const picker=page.getByRole('listbox',{name:'快捷选择技能'})
  await expect(picker.getByRole('option',{name:'另一项技能'})).toBeVisible()
  await capture(page,'chat-skill-picker')
  await picker.getByRole('button',{name:'从快捷选择移除 另一项技能'}).click()
  await expect(picker.getByRole('option',{name:'另一项技能'})).toHaveCount(0)
  expect(skillMutation).toBe(false)
  await input.press('Enter')
  await expect(input).toHaveValue('$"已加载技能" ')
})

test('Soul materials and build history use tight tables and shared pagination', async ({page}) => {
  const created_at='2026-11-18T02:00:00Z'
  const assets=Array.from({length:11},(_,i)=>({id:'soul-'+i,filename:i?'素材-'+i+'.txt':'长文件名'.repeat(12)+'.jpg',size:1024,caption:'',created_at}))
  await page.route('**/api/me/knowledge',route=>route.fulfill({json:{
    assets,active_build_id:null,question_categories:[],model:{model:'示例模型'},
    builds:[{id:'build-1',status:'approved',origin:'manual',created_at,stage:'审核通过',progress:100,document:{voice:'**简洁清楚**'},interview:[],asset_ids:[]}],
  }}))
  await page.route('**/api/me/knowledge/folders',route=>route.fulfill({json:{items:['示例目录'],captions:{}}}))
  await login(page)
  await page.goto('/knowledge')
  const table=page.locator('.soul-asset-table')
  await expect(table.locator('tbody tr')).toHaveCount(10)
  await expect(table.getByRole('columnheader',{name:'用于构建'})).toBeVisible()
  const pager=page.getByRole('navigation',{name:'知识库素材分页'})
  await pager.getByRole('button',{name:'下一页'}).click()
  await expect(table.locator('tbody tr')).toHaveCount(2)
  const record=page.locator('.soul-build-record')
  await expect(record.getByText('审核通过',{exact:true})).toBeVisible()
  expect((await record.boundingBox())!.height).toBeLessThan(100)
  await record.locator('summary').click()
  await expect(record.locator('strong').filter({hasText:'简洁清楚'})).toBeVisible()
  await table.scrollIntoViewIfNeeded()
  await capture(page,'soul-materials')
  await record.scrollIntoViewIfNeeded()
  await capture(page,'soul-history')
  await page.setViewportSize({width:390,height:844})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await capture(page,'soul-mobile')
})

test('admin and model records use compact search and jump controls and omit certificate bodies', async ({page}) => {
  const agent={user_id:'agent-fixture',username:'示例账号',profile:{avatar_name:'示例正式分身',bio:'长资料换行。\n'.repeat(100)},aic:'demo.test.AIC',
    identity_status:'active',certificate:{id:'fixture',mode:'acps-direct',expires_at:'2026-12-30T00:00:00Z',public_pem:'CERT_BODY_EXAMPLE'},
    acs_document:{version:'1.0.0',entityMeta:{environment:'1.0.0'}},protocol:{endpoint:'https://example.test/aip/rpc'}}
  await page.route('**/api/admin/agents/operations?**',route=>route.fulfill({json:{
    mode:'acps-direct',personal_registration_mode:'acps-direct',entity_registration_enabled:true,
    items:[{...agent,profile:{avatar_name:agent.profile.avatar_name},identity_job:null}],total:1,offset:0,limit:10,
    entity_port_pool:{host:'192.0.2.10',start:10000,end:10020,registered:1},
  }}))
  await page.route('**/api/admin/agents/agent-fixture/detail',route=>route.fulfill({json:agent}))
  let checked=false
  await page.route('**/api/admin/agents/agent-fixture/health',route=>{checked=true;return route.fulfill({json:{message:'示例健康检查通过'}})})
  await page.route('**/api/admin/accounts?**',route=>{
    const params=new URL(route.request().url()).searchParams, offset=Number(params.get('offset')), search=params.get('search')||''
    return route.fulfill({json:{items:[{id:'a-'+offset,name:search?'查询匹配账号':'示例账户 '+(offset/10+1),username:'fixture',person_type:'student',account_status:'active',role:'user'}],offset,total:search?1:21}})
  })
  await page.route('**/api/records/model_calls?**',route=>route.fulfill({json:{
    items:[{id:1,model:'测试模型',provider:'示例平台',purpose:'聊天',created_at:'2026-11-18T01:00:00Z',usage:{input_tokens:2,output_tokens:3,total_tokens:5}}],total:1,offset:0,
  }}))
  await login(page,true)
  await page.goto('/admin/users')
  const filter = page.getByRole('combobox', {name:'账户角色'})
  await filter.click()
  const options = page.getByRole('listbox', {name:'账户角色'})
  await expect(options).toBeVisible()
  await filter.press('End')
  await filter.press('Enter')
  await expect(options).toBeHidden()
  await expect(filter).toHaveValue('admin')
  await filter.click()
  await filter.press('Escape')
  await expect(options).toBeHidden()
  await expect(filter).toBeFocused()
  const accounts=page.getByRole('navigation',{name:'账户分页'})
  await expect(accounts.getByRole('button',{name:'跳转',exact:true})).toHaveCount(0)
  await expect(accounts.getByRole('searchbox')).toBeHidden()
  await expect(accounts.getByRole('button',{name:'搜索账户'})).toHaveAttribute('aria-expanded','false')
  await expect(page.getByText('示例账户 1')).toBeVisible()
  await accounts.getByRole('spinbutton').fill('3')
  await accounts.getByRole('spinbutton').press('Enter')
  await expect(page.getByText('示例账户 3')).toBeVisible()
  await expect(accounts.getByRole('button',{name:'下一页'})).toBeDisabled()
  const searchButton = accounts.getByRole('button',{name:'搜索账户'})
  const positions = () => accounts.getByRole('button').evaluateAll(buttons => buttons.map(b => {
    const nav = b.closest('nav')!.getBoundingClientRect(), rect = b.getBoundingClientRect()
    return [Math.round(rect.x - nav.x), Math.round(rect.y - nav.y)]
  }))
  const beforeSearch = await positions()
  await searchButton.click()
  await expect(accounts.getByRole('searchbox')).toBeFocused()
  expect(await positions()).toEqual(beforeSearch)
  await accounts.getByRole('searchbox').fill('匹配')
  await accounts.getByRole('searchbox').press('Enter')
  await expect(page.getByText('查询匹配账号')).toBeVisible()
  await expect(accounts).toContainText('第 1 / 1 页')
  await accounts.getByRole('searchbox').press('Escape')
  await expect(searchButton).toBeFocused()
  expect(await positions()).toEqual(beforeSearch)
  await page.route('**/api/admin/accounts/*/profile', route => route.fulfill({json:{
    account:{identity_username:'teacher@test.invalid',person_type:'teacher'},approved_at:'2026-11-18T00:00:00Z',
    profile:{name:'审核示例',person_type:'teacher',employee_id:'T-12',specialty:'示例方向',bio:'教学研究与课程建设。\n'.repeat(30),education:[{name:'示例大学',degree:'博士'}],visibility:{specialty:'private'}},
  }}))
  await page.getByRole('button',{name:'查看资料'}).first().click()
  const profile = page.getByRole('dialog',{name:'用户资料 · 审核示例'})
  await expect(profile).toContainText('已审核通过的个人资料')
  await expect(profile).toContainText('T-12')
  await expect(profile).toContainText('示例方向')
  await expect(profile).not.toContainText('identity_username')
  const overlay = profile.locator('xpath=ancestor::div[contains(concat(" ",normalize-space(@class)," ")," el-overlay ")]')
  await expect.poll(() => overlay.evaluate(el => {
    const r = el.getBoundingClientRect()
    return {left:r.left,top:r.top,rightGap:Math.max(0,document.documentElement.clientWidth-r.right),
      bottomGap:Math.max(0,innerHeight-r.bottom),filter:getComputedStyle(el).backdropFilter,parent:el.parentElement?.tagName}
  })).toMatchObject({left:0,top:0,rightGap:0,bottomGap:0,filter:expect.stringContaining('blur'),parent:'BODY'})
  const body = profile.getByRole('region',{name:'用户资料正文'})
  const heading = profile.getByRole('heading',{name:'用户资料 · 审核示例'})
  await overlay.evaluate(node => Promise.allSettled(node.getAnimations({subtree:true}).map(animation => animation.finished)).then(() => {}))
  const top = (await heading.boundingBox())!.y
  await body.evaluate(node => { node.scrollTop = node.scrollHeight })
  expect(await body.evaluate(node => node.scrollTop)).toBeGreaterThan(0)
  expect((await heading.boundingBox())!.y).toBeCloseTo(top,0)
  await expect(body).toContainText('示例大学')
  await expect(body).not.toContainText('{"name"')
  await body.evaluate(node => { node.scrollTop = 0 })
  await capture(page,'admin-account-profile')
  const max = profile.getByRole('button', {name:'全屏', exact:true})
  const close = profile.getByRole('button', {name:'关闭', exact:true})
  await expect(max).toBeVisible()
  await max.click()
  await page.mouse.move(0, 0)
  const restore = profile.getByRole('button', {name:'还原小窗'})
  await expect(restore).toHaveCSS('border-top-width','0px')
  await expect(restore).toHaveCSS('box-shadow','none')
  await expect(restore).toHaveCSS('background-color','rgba(0, 0, 0, 0)')
  await expect(close).toHaveCSS('height', await restore.evaluate(el => getComputedStyle(el).height))
  await expect(close).toHaveCSS('width', await restore.evaluate(el => getComputedStyle(el).width))
  await restore.click()
  await close.click()
  await expect(profile).toBeHidden()
  await page.goto('/admin/agents')
  const row=page.getByRole('row').filter({hasText:'示例正式分身'})
  await expect(row.getByText('已部署')).toBeVisible()
  await row.getByRole('button',{name:'Health 检查'}).click()
  await expect(row.getByRole('cell').nth(3)).toContainText('示例健康检查通过')
  await expect(row.getByRole('cell').last()).not.toContainText('示例健康检查通过')
  expect(checked).toBe(true)
  await capture(page,'admin-agent-actions')
  await row.getByRole('button',{name:'详情'}).click()
  const detail=page.getByRole('dialog',{name:'示例正式分身'})
  await expect(detail).toContainText('https://example.test/aip/rpc')
  await expect(detail).toContainText('证书到期')
  await expect(detail).not.toContainText('CERT_BODY_EXAMPLE')
  const agentBody = detail.getByRole('region', {name:'分身详情正文'})
  const maximize = detail.getByRole('button', {name:'全屏', exact:true})
  const dismiss = detail.getByRole('button', {name:'关闭', exact:true})
  await expect(agentBody).toContainText('长资料换行。')
  await detail.evaluate(el => Promise.allSettled(el.getAnimations({subtree:true}).map(a => a.finished)))
  const controls = await dismiss.boundingBox()
  await agentBody.evaluate(el => { el.scrollTop = el.scrollHeight })
  expect(await agentBody.evaluate(el => el.scrollTop)).toBeGreaterThan(0)
  expect((await dismiss.boundingBox())!.y).toBeCloseTo(controls!.y,0)
  await maximize.click()
  await detail.getByRole('button', {name:'还原小窗'}).click()
  await capture(page,'admin-agent-detail')
  await dismiss.click()
  await expect(detail).toBeHidden()
  await page.goto('/models')
  const modelPager=page.getByRole('navigation',{name:'模型调用分页'})
  await expect(modelPager.getByRole('searchbox')).toBeHidden()
  const modelRow=page.getByRole('row').filter({hasText:'测试模型'})
  await expect(modelRow).toContainText('5')
  await modelRow.scrollIntoViewIfNeeded()
  await capture(page,'model-usage-table')
  await modelRow.getByRole('button',{name:'详情'}).click()
  await expect(page.getByRole('dialog',{name:'模型调用'})).toContainText('输入 Token')
})

test('peer task swaps content without clearing the sidebar and renders references and feedback', async ({page})=>{
  const sessionId='test-peer-task'
  const task={id:'peer-d1',objective:'整理交流事项',status:'ended',delivery_mode:'negotiation',rounds_done:3,round_limit:10,
    returned_summary:'**结论：** 已同步结果。',end_reason:'用户已确认。',created_at:'2026-11-18T00:00:00Z',
    files:[{id:'csv-copy',filename:'对方报告.csv',message_id:1,origin:'attachment',download_url:'/api/conversations/recipient-copy/files/csv-copy'}]}
  let promotion: unknown
  await page.route('**/api/conversations/recipient-copy/files/csv-copy',route=>route.fulfill({body:'a,b\n1,2',contentType:'text/csv'}))
  await page.route('**/api/conversations/recipient-copy/files/csv-copy/promote',route=>{
    promotion=route.request().postDataJSON()
    return route.fulfill({json:{attachment_id:'saved-copy'}})
  })
  let releaseTask:()=>void=()=>{}
  const gate=new Promise<void>(resume=>{releaseTask=resume})
  await page.route('**/api/agent-sessions?**',route=>route.fulfill({json:{items:[{session_id:sessionId,peer_name:'示例同事',messages:[]}],total:1,offset:0}}))
  await page.route(url=>url.pathname==='/api/agent-sessions/test-peer-task',route=>route.fulfill({json:{
    peer_name:'示例同事',resource_conversation_id:'',has_more:false,messages:[{turn:1,dialogue_id:'main',speaker_name:'示例同事',speaker_role:'agent',content:'主会话一直可见。',is_own:false}],
  }}))
  await page.route('**/api/agent-sessions/test-peer-task/tasks?**',route=>route.fulfill({json:{items:[task],total:1,offset:0}}))
  await page.route(url=>url.pathname==='/api/agent-dialogues/peer-d1',async route=>{
    await gate
    return route.fulfill({json:{peer_name:'示例同事',resource_conversation_id:'',has_more:false,task,current_transport_mode:'acps',messages:[{
      turn:1,dialogue_id:task.id,speaker_name:'示例同事',speaker_role:'agent',state:'awaiting_input',
      content:'已整理交流事项。\n\n```mermaid\nflowchart LR\nA --> B\n```\n\n> 引用来源：\n> 测试资料.txt · RAG  \n\n[等待用户] 请确认下一步。',
    }]}})
  })
  await login(page)
  await page.goto('/dialogues')
  await page.getByRole('button',{name:'与 示例同事 的对话'}).click()
  const dialog=page.getByRole('dialog',{name:'分身间会话'})
  await expect(dialog.getByText('主会话一直可见。')).toBeVisible()
  await dialog.getByRole('button',{name:'任务侧栏'}).click()
  await dialog.getByRole('button',{name:/整理交流事项/}).click()
  await expect(dialog.locator('.peer-chat-pane')).toHaveAttribute('aria-busy','true')
  await expect(dialog.getByText('主会话一直可见。')).toBeVisible()
  await expect(dialog.getByRole('button',{name:/整理交流事项/})).toBeVisible()
  releaseTask()
  await expect(dialog.locator('.peer-chat-pane')).toHaveAttribute('aria-busy','false')
  await expect(dialog.getByRole('heading',{name:'任务 · 示例同事'})).toBeVisible()
  await expect(dialog.locator('.dialogue-state')).toContainText('3/10 轮 · ACPS')
  await expect(dialog.getByRole('status',{name:'平台提示'})).toContainText('请确认下一步。')
  await expect(dialog.getByRole('button',{name:'还原小窗'})).toBeVisible()
  const sources = dialog.getByRole('button',{name:'2 份资料'})
  await sources.click()
  await expect(dialog.getByRole('dialog',{name:'引用来源'})).toContainText('测试资料.txt')
  const attachment = dialog.getByRole('button',{name:/对方报告.csv/})
  await attachment.click({button:'right'})
  const menu = dialog.getByRole('menu',{name:'附件操作'})
  await expect(menu).toBeVisible()
  const downloaded = page.waitForEvent('download')
  await menu.getByRole('menuitem',{name:'下载'}).click()
  expect((await downloaded).suggestedFilename()).toBe('对方报告.csv')
  await attachment.focus()
  await attachment.press('Shift+F10')
  await menu.getByRole('menuitem',{name:'转存资料'}).click()
  await page.getByRole('dialog',{name:'转存资料'}).getByRole('button',{name:'确定'}).click()
  await expect.poll(()=>promotion).toMatchObject({visibility:'private'})
  await capture(page,'peer-task-references')
  await dialog.locator('summary').filter({hasText:'任务总结已返回主分身记忆'}).click()
  await expect(dialog.locator('.peer-result strong')).toHaveText('结论：')
  await expect(dialog.locator('.mermaid-card').getByRole('status')).toContainText('绘图完成',{timeout:10000})
  await capture(page,'peer-task-feedback')
})

test('document popup uses the same search and numeric pager as record lists', async ({page})=>{
  const file={id:'long-document',filename:'多页示例.txt',relative_path:'多页示例.txt',visibility:'private',size:30000,caption:''}
  await page.route('**/api/me/attachments',route=>route.fulfill({json:[file]}))
  await page.route('**/api/me/attachments/long-document',route=>route.fulfill({
    contentType:'text/plain',body:'第一段。\n'.repeat(2000)+'第二段。\n'.repeat(2000)+'最后页的关键词。',
  }))
  await login(page)
  await page.goto('/files')
  await page.getByRole('button',{name:'多页示例.txt'}).click()
  await page.locator('.material-inspector').getByRole('button',{name:'预览'}).click()
  const dialog=page.getByRole('dialog',{name:'多页示例.txt'})
  const pager=dialog.getByRole('navigation',{name:'文档分页'})
  await expect(pager.getByRole('button',{name:'搜索文档'})).toHaveAttribute('aria-expanded','false')
  const jump=pager.getByRole('spinbutton')
  await expect(jump).toHaveAttribute('max','2')
  await jump.fill('2')
  await jump.press('Enter')
  await expect(pager.getByRole('button',{name:'下一页'})).toBeDisabled()
  await expect(dialog.locator('.preview-document')).toContainText('最后页的关键词。')
  const box=await pager.boundingBox()
  expect(box!.y+box!.height).toBeLessThanOrEqual(page.viewportSize()!.height)
  await pager.getByRole('button',{name:'搜索文档'}).click()
  await pager.getByRole('searchbox').fill('关键词')
  await pager.getByRole('searchbox').press('Enter')
  await expect.poll(() => page.evaluate(() => CSS.highlights.get('preview-match')?.size || 0)).toBeGreaterThan(0)
  await capture(page,'preview-compact-pagination')
})

test('login displays the running app version and question filters stay compact', async ({page}) => {
  await page.goto('/')
  const config = await (await page.request.get('/api/auth/config')).json()
  const {info} = await (await page.request.get('/api/openapi.json')).json()
  expect(config.version).toBe(info.version)
  await expect(page.locator('.pill')).toContainText(info.version)
  await page.route('**/api/admin/soul-question-bank?**', route => route.fulfill({json:{
    categories:[{id:'goals',name:'目标',available:1,total:1,active:true}],offset:0,total:1,
    items:[{id:'fixture',prompt:'请描述近期的一次目标选择。',category_id:'goals',category_name:'目标',active:true,source:{license:'示例'}}],
  }}))
  await login(page,true)
  await page.goto('/admin/question_bank')
  const bank = page.locator('section.card').filter({has:page.getByRole('heading',{name:/分类题库/})})
  await expect(bank.getByRole('columnheader',{name:'题目',exact:true})).toBeVisible()
  const category = bank.getByRole('combobox',{name:'类别',exact:true})
  const status = bank.getByRole('combobox',{name:'题目状态'})
  expect((await category.boundingBox())!.y).toBe((await status.boundingBox())!.y)
  await expect(category).toHaveValue('')
  await capture(page,'question-filters')
  await bank.getByRole('button',{name:'编辑',exact:true}).first().click()
  await expect(page.getByRole('dialog',{name:'编辑题目'}).getByLabel('题目',{exact:true})).toBeVisible()
  await capture(page,'question-editor')
})

test('packaged PDF worker renders pages and searches their text', async ({page, context}) => {
  const source = await context.newPage()
  await source.setContent('<article style="padding:36px"><h1>文件预览示例</h1><p>这份文档有两页，可用底部分页控件切换。</p><h2>阅读与缩放</h2><p>放大后可滚动查看细节，适应宽度会随窗口大小变化。</p></article><p style="break-before:page">Second sheet needle</p>')
  const bytes = await source.pdf()
  await source.close()
  const file = {id:'pdf-test',filename:'example.pdf',relative_path:'example.pdf',visibility:'private',size:bytes.length,caption:''}
  await page.route('**/api/me/attachments', route => route.fulfill({json:[file]}))
  await page.route('**/api/me/attachments/pdf-test', route => route.fulfill({contentType:'application/pdf',body:bytes}))
  await login(page)
  await page.goto('/files')
  await page.getByRole('button',{name:file.filename}).click()
  await page.locator('.material-inspector').getByRole('button',{name:'预览'}).click()
  const dialog=page.getByRole('dialog',{name:file.filename})
  const pager=dialog.getByRole('navigation',{name:'文档分页'})
  await expect(pager.getByRole('spinbutton')).toHaveAttribute('max','2')
  await expect.poll(() => dialog.locator('canvas').evaluate(canvas => {
    if (!(canvas instanceof HTMLCanvasElement) || !canvas.width || !canvas.height) return false
    const pixels = canvas.getContext('2d')?.getImageData(0,0,canvas.width,canvas.height).data
    return pixels?.some((v,i) => i%4===0 && v<128 && pixels[i+3]===255)
  })).toBe(true)
  const zoom = dialog.getByRole('group', {name:'预览缩放'})
  const input = zoom.getByRole('spinbutton', {name:'预览缩放比例'})
  await expect(zoom.getByText('%', {exact:true})).toBeVisible()
  const body = dialog.locator('.preview-document')
  const position = () => body.evaluate(node => {
    const page = node.querySelector('canvas')!.getBoundingClientRect()
    const view = node.getBoundingClientRect()
    return {
      x: (view.left + node.clientLeft + node.clientWidth / 2 - page.left) / page.width,
      y: (view.top + node.clientTop + node.clientHeight / 2 - page.top) / page.height,
      width: page.width, overflow: node.scrollWidth - node.clientWidth,
    }
  })
  const fit = zoom.getByRole('button', {name:'适应宽度'})
  await expect(fit).toHaveAttribute('aria-pressed','true')
  expect((await position()).overflow).toBeLessThanOrEqual(1)
  const before = await position()
  const fitPercent = Number(await input.inputValue())
  await input.fill(String(Math.round(fitPercent * 2)))
  await input.press('Enter')
  await expect.poll(async () => (await position()).width / before.width).toBeCloseTo(2,1)
  const after = await position()
  expect(after.x).toBeCloseTo(before.x,2)
  expect(after.y).toBeCloseTo(before.y,2)
  await zoom.getByRole('button',{name:'100%',exact:true}).click()
  await expect(input).toHaveValue('100')
  await zoom.getByRole('button',{name:'放大预览'}).click()
  await expect(input).toHaveValue('125')
  await zoom.getByRole('button',{name:'缩小预览'}).click()
  await expect(input).toHaveValue('100')
  await fit.click()
  await dialog.getByRole('button',{name:'全屏',exact:true}).click()
  await expect.poll(async () => Number(await input.inputValue())).toBeGreaterThan(fitPercent)
  await dialog.getByRole('button',{name:'还原小窗'}).click()
  await expect.poll(async () => Number(await input.inputValue())).toBeCloseTo(fitPercent,0)
  await page.mouse.move(0,0)
  await body.evaluate(node => { node.scrollTop = 0 })
  await capture(page,'pdf-toolbar')
  await pager.getByRole('button',{name:'搜索文档'}).click()
  await pager.getByRole('searchbox').fill('needle')
  await pager.getByRole('searchbox').press('Enter')
  await expect(pager.getByRole('spinbutton')).toHaveValue('2')
  await expect(dialog.getByLabel('搜索匹配').first()).toBeVisible()
})

test('工作台键盘入口与表单字体跟随主题', async ({ page }) => {
  await login(page)
  await page.goto('/profile')
  const skip = page.getByRole('button', {name:'跳至主要内容'})
  await expect(page.getByRole('main')).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(skip).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('main')).toBeFocused()
  const family = await page.locator('body').evaluate(el => getComputedStyle(el).fontFamily)
  expect(family).toContain('Segoe UI')
  await expect(page.locator('main textarea').first()).toHaveCSS('font-family', family)
})

test('sidebars animate both directions and honor reduced motion', async ({page}) => {
  await page.route('**/api/agent-sessions/slide/tasks?**',r=>r.fulfill({json:{items:[],total:0,offset:0}}))
  await page.route('**/api/agent-sessions/slide',r=>r.fulfill({json:{peer_name:'示例',messages:[],tasks:[],has_more:false}}))
  await login(page)
  await page.goto('/dialogues?session=slide')
  const window=page.getByRole('dialog',{name:'分身间会话'})
  const key=window.getByRole('button',{name:'任务侧栏'}), task=window.locator('#peer-task-navigation')
  await window.getByRole('button',{name:'全屏',exact:true}).click()
  await expect.poll(()=>window.evaluate(n=>n.getAnimations().length)).toBe(0)
  const chat=window.locator('.peer-chat-pane'), closed=await chat.boundingBox()
  const slides=(panel:typeof task)=>panel.evaluate(n=>n.getAnimations().some(a=>a instanceof CSSTransition && a.transitionProperty==='transform'))
  await key.click()
  expect(await slides(task)).toBe(true)
  await expect.poll(async()=>(await chat.boundingBox())!.x).toBeGreaterThan(closed!.x+100)
  await key.click()
  expect(await slides(task)).toBe(true)
  await expect(task).toBeHidden()
  await expect.poll(async()=>Math.round((await chat.boundingBox())!.x)).toBe(Math.round(closed!.x))
  await page.setViewportSize({width:390,height:844})
  await page.goto('/overview')
  const nav=page.locator('aside.sidebar'), toggle=page.getByRole('button',{name:/展开导航|收起导航/})
  await toggle.click()
  expect(await slides(nav)).toBe(true)
  await expect(nav.locator('[aria-current="page"]')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(toggle).toBeFocused()
  expect(await slides(nav)).toBe(true)
  await expect(nav).toBeHidden()
  await page.emulateMedia({reducedMotion:'reduce'})
  await toggle.click()
  await expect(nav).toBeVisible()
  expect(await slides(nav)).toBe(false)
  await toggle.click()
  await expect(nav).toBeHidden()
})

test('申请记录工具栏换行且仅选择已审核行', async ({ page }) => {
  await login(page)
  const rows = ['pending', 'approved', 'rejected'].map((status, index) => ({
    id: String(index), status, snapshot: {}, submitted_at: '2026-11-18', last_modified_at: '2026-11-18',
  }))
  let empty = true
  await page.route('**/api/records/history?**', route => route.fulfill({
    json: { items: empty ? [] : rows, total: empty ? 0 : rows.length, offset: 0, limit: 10 },
  }))
  await page.goto('/history')
  const toggle = page.getByRole('checkbox', { name: '选择本页' })
  await expect(toggle).toBeDisabled()
  await expect(page.getByText('还没有申请记录', { exact: false })).toBeVisible()
  empty = false
  await page.reload()
  const checks = page.locator('tbody input[type=checkbox]')
  await expect(checks).toHaveCount(2)
  await checks.first().check()
  await expect(toggle).toHaveJSProperty('indeterminate', true)
  await toggle.check()
  await expect(page.getByText('已选 2 条')).toBeVisible()
  await page.getByRole('button', { name: '删除所选' }).click()
  await page.getByRole('button', { name: /Cancel|取消/ }).click()
  await expect(checks).toHaveCount(2)
  await page.setViewportSize({ width: 390, height: 844 })
  const toolbar = page.locator('.notification-batch')
  await expect(toolbar).toHaveCSS('display', 'flex')
  await expect(toolbar).toHaveCSS('flex-wrap', 'wrap')
  expect(await toolbar.evaluate(el => el.scrollWidth <= el.clientWidth)).toBeTruthy()
})
