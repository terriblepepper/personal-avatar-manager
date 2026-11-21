import { expect, test, type Page } from '@playwright/test'

async function register(page: Page) {
  const username = 'guide-' + Date.now()
  const credentials = { username, password:'browser-guide-password' }
  expect((await page.request.post('/api/auth/register', { data:{ ...credentials, email:username + '@example.test', student_id:'20261119' } })).status()).toBe(201)
  expect((await page.request.post('/api/auth/login', { data:credentials })).ok()).toBe(true)
}

const steps = [
  ['我的工作台','/','.workspace-page .steps','填写资料'],
  ['个人资料','/profile','.profile-header-actions button','保存草稿'],
  ['我的分身','/avatar','main > .heading h1','我的分身'],
  ['分身概览','/avatar','.avatar-panel .empty','你还没有分身'],
  ['资料文件','/files','#attachments-editor button','上传文件'],
  ['Soul','/knowledge','#knowledge-workspace h3','个人形象概览'],
  ['与分身对话','/chat','#personal-chat-compose',''],
  ['模型配置','/models','.avatar-panel h2','聊天模型配置'],
  ['分身技能','/skills','.avatar-panel h2','安装技能包'],
  ['分身会话','/dialogues','.peer-session-list h2','分身间会话'],
  ['最近动态','/messages','main [role=tablist]','全部消息'],
  ['申请记录','/history','.pagehead h1','申请记录'],
  ['账户与帮助','/','#sidebar-extra-actions button','帮助文档'],
] as const

for (const viewport of [{width:390,height:740},{width:1440,height:900}]) {
  test('guided navigation, real targets and replay at ' + viewport.width + 'px', async ({ page }) => {
    await page.setViewportSize(viewport)
    await register(page)
    await page.goto('/files')
    const guide = page.locator('dialog.workspace-guide')
    await expect(guide.getByRole('heading')).toHaveText('欢迎来到分身平台')
    for (const pseudo of [null,'::backdrop']) {
      expect(await guide.evaluate((node, part) => getComputedStyle(node, part).backdropFilter, pseudo)).toBe('none')
    }
    const shots = process.env.UI_PARITY_SCREENSHOTS
    if (shots) await page.screenshot({path:shots + '/welcome-' + viewport.width + '.png',animations:'disabled'})
    await guide.getByRole('button', {name:'认识工作台'}).click()
    await expect(guide.getByRole('heading')).toHaveText('我的工作台')
    await page.reload()
    for (const [title,path,selector,text] of steps) {
      await expect(guide.getByRole('heading')).toHaveText(title)
      await expect.poll(() => new URL(page.url()).pathname).toBe(path)
      const target = page.locator(selector).filter({hasText:text}).first()
      await expect(target).toBeVisible()
      await expect.poll(async () => {
        const ring = await guide.locator('.guide-spotlight').boundingBox()
        const box = await target.boundingBox()
        const card = await guide.locator('.guide-card').boundingBox()
        if (!ring || !card || !box) return false
        return Math.abs(box.x - ring.x - 3) < 2 && Math.abs(box.y - ring.y - 3) < 2 &&
          Math.abs(box.width - ring.width + 6) < 2 && box.y >= 0 && box.y + box.height <= viewport.height + 1 &&
          card.x >= 0 && card.x + card.width <= viewport.width + 1 &&
          (card.x >= ring.x + ring.width || card.x + card.width <= ring.x || card.y >= ring.y + ring.height || card.y + card.height <= ring.y)
      }, {message:'高亮目标应完整可见且未被卡片覆盖：' + title}).toBe(true)
      if (title === '资料文件') {
        await guide.getByRole('button',{name:'上一步'}).click()
        await expect(guide.getByRole('heading')).toHaveText('分身概览')
        await expect(page).toHaveURL(/\/avatar$/)
        await guide.getByRole('button',{name:'下一步'}).click()
        await expect(guide.getByRole('heading')).toHaveText(title)
        if (shots) {
          await expect(guide.getByRole('button',{name:'下一步'})).toBeEnabled()
          await page.screenshot({path:shots + '/files-' + viewport.width + '.png',animations:'disabled'})
        }
      }
      await guide.getByRole('button',{name:title === '账户与帮助' ? '完成导览' : '下一步'}).click()
    }
    await expect(guide).toHaveCount(0)
    await page.reload()
    await expect(page.locator('.shell')).toBeVisible()
    await expect(guide).toHaveCount(0)
    if (viewport.width < 900) await page.getByRole('button',{name:'展开导航'}).click()
    const more = page.getByRole('button',{name:'账户与帮助'})
    await more.click()
    const toggle = page.getByRole('menuitemcheckbox',{name:'Debug 模式'})
    await toggle.click()
    await page.emulateMedia({reducedMotion:'reduce'})
    for (const [action,title] of [['重播首次介绍','欢迎来到分身平台'],['重播模块导览','我的工作台']]) {
      await page.getByRole('menuitem',{name:action}).click()
      await expect(guide.getByRole('heading')).toHaveText(title)
      await expect(guide.getByRole('button',{name:'跳过引导'})).toBeEnabled()
      await page.keyboard.press('Escape')
      await expect(guide).toHaveCount(0)
      await more.click()
    }
    await toggle.click()
    await expect(page.getByRole('menuitem',{name:'重播模块导览'})).toHaveCount(0)
    expect((await (await page.request.get('/api/auth/me')).json()).user.onboarding_stage).toBe('done')
  })
}

test('welcome and tour animate when skipped, forward and back move in opposite directions', async ({ page }) => {
  await register(page)
  await page.emulateMedia({reducedMotion:'no-preference'})
  await page.goto('/')
  const guide = page.locator('dialog.workspace-guide')
  async function animatedSkip() {
    const leaving = guide.evaluate(node => new Promise<boolean>(resolve => {
      const timeout = setTimeout(() => resolve(false), 3000)
      node.addEventListener('transitionrun', event => {
        if (event.target === node && (event as TransitionEvent).propertyName === 'opacity' && node.classList.contains('guide-dialog-leave-active')) {
          clearTimeout(timeout)
          resolve(true)
        }
      })
    }))
    await guide.getByRole('button',{name:'跳过引导'}).click()
    expect(await leaving).toBe(true)
    await expect(guide).toHaveCount(0)
  }
  await animatedSkip()
  await page.getByRole('button',{name:'账户与帮助'}).click()
  await page.getByRole('menuitemcheckbox',{name:'Debug 模式'}).click()
  await page.getByRole('menuitem',{name:'重播模块导览'}).click()
  for (const [label,direction,heading] of [['下一步',-1,'个人资料'],['上一步',1,'我的工作台']] as const) {
    const button = guide.getByRole('button',{name:label})
    await expect(button).toBeEnabled()
    const offset = await button.evaluate(element => {
      (element as HTMLButtonElement).click()
      const effects = element.closest('section')!.getAnimations().map(a => a.effect as KeyframeEffect)
      const exit = effects.flatMap(e => e.getKeyframes()).find(frame => frame.opacity === '0' && frame.translate)
      return parseFloat(String(exit?.translate))
    })
    expect(Math.sign(offset)).toBe(direction)
    await expect(guide.getByRole('heading')).toHaveText(heading)
  }
  await animatedSkip()
})

test('restricted guide retries unavailable profile controls and visits only allowed pages', async ({ page }) => {
  await register(page)
  await page.route('**/api/auth/me', async route => {
    const reply = await route.fetch()
    const session = await reply.json()
    session.user.access_scope = 'profile'
    await route.fulfill({response:reply, json:session})
  })
  await page.route('**/api/me', route => route.fulfill({status:503,json:{detail:'暂时不可用'}}))
  await page.goto('/profile')
  const guide = page.locator('dialog.workspace-guide')
  await guide.getByRole('button',{name:'认识工作台'}).click()
  await expect(guide.getByRole('heading')).toHaveText('模块暂未就绪',{timeout:10000})
  await expect(guide.locator('.guide-spotlight')).toHaveCount(0)
  await page.unroute('**/api/me')
  await guide.getByRole('button',{name:'重试'}).click()
  for (const [title,path] of [['个人资料','/profile'],['我的分身','/models'],['资料文件','/files'],['模型配置','/models'],['申请记录','/history'],['账户与帮助','/profile']]) {
    await expect(guide.getByRole('heading')).toHaveText(title)
    await expect.poll(() => new URL(page.url()).pathname).toBe(path)
    await expect(guide.locator('.guide-spotlight')).toBeVisible()
    await guide.getByRole('button',{name:title === '账户与帮助' ? '完成导览' : '下一步'}).click()
  }
  await expect(guide).toHaveCount(0)
})
