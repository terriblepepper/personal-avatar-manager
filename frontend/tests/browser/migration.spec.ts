import { expect, test, type Page } from '@playwright/test'

async function login(page:Page, username:string, password:string, path='/') {
  await page.goto(path)
  await page.locator('input[name=username]').fill(username)
  await page.locator('input[name=password]').fill(password)
  await page.getByRole('button',{name:'登录工作台 →'}).click()
  await expect(page.locator('.shell')).toBeVisible()
}
async function loginUser(page:Page){await login(page,'browser-user','browser-user-password')}

test('profile saves and submits, uploaded files appear and preview', async ({page}) => {
  await loginUser(page)
  await page.goto('/profile')
  await expect(page.getByRole('heading',{name:'个人资料',exact:true})).toBeVisible()
  const save=page.getByRole('button',{name:'保存草稿'})
  await expect(save).toBeEnabled({timeout:30_000})
  await page.locator('label').filter({hasText:'个人简介'}).locator('textarea').fill('浏览器测试资料简介')
  const saved=page.waitForResponse(response=>response.url().endsWith('/api/me/profile') && response.request().method()==='PUT')
  await save.click();expect((await saved).status()).toBe(200)
  const submitted=page.waitForResponse(response=>response.url().endsWith('/api/me/submit') && response.request().method()==='POST')
  await page.getByRole('button',{name:'保存并提交审核'}).click();expect((await submitted).status()).toBe(200)
  await page.goto('/files')
  const completed = page.waitForResponse(response =>
    /^\/api\/me\/upload-batches\/[^/]+\/complete$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST')
  await page.locator('input[type=file]').first().setInputFiles({name:'browser-note.txt',mimeType:'text/plain',buffer:Buffer.from('文件预览浏览器内容')})
  expect((await completed).status()).toBe(200)
  await expect(page.getByRole('status').filter({hasText:'上传完成'})).toBeVisible({timeout:30_000})
  const inventory=await page.request.get('/api/me/attachments');expect(inventory.status()).toBe(200)
  expect(await inventory.json()).toEqual(expect.arrayContaining([expect.objectContaining({filename:'browser-note.txt'})]))
  const item=page.locator('.material-entry').filter({hasText:'browser-note.txt'});await expect(item).toBeVisible({timeout:10_000});await item.click()
  await page.getByRole('button',{name:'预览',exact:true}).click()
  await expect(page.getByText('文件预览浏览器内容')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', {name:'关闭预览'}).click()
})

test('model configuration, skill installation, deep links and mobile viewport', async ({page}) => {
  await loginUser(page);await page.goto('/models')
  await expect(page.getByRole('button',{name:'保存模型配置'})).toBeVisible()
  await expect(page.getByRole('textbox',{name:'模型 ID'}).first()).toHaveValue('test-model')
  const modelSaved=page.waitForResponse(response=>response.url().endsWith('/api/me/model-settings') && response.request().method()==='PUT')
  await page.getByRole('button',{name:'保存模型配置'}).click();expect((await modelSaved).status()).toBe(200)
  const saved = page.locator('.model-save-actions').first()
  await expect(saved.getByRole('status')).toContainText('上次保存于')
  const saveButton = await saved.getByRole('button').boundingBox(), time = await saved.getByRole('status').boundingBox()
  expect(time!.x - (saveButton!.x + saveButton!.width)).toBeGreaterThan(6)
  await page.goto('/skills')
  await page.locator('input[type=file]').setInputFiles({name:'SKILL.md',mimeType:'text/markdown',buffer:Buffer.from('---\nname: browser-skill\ndescription: 浏览器技能\n---\n请整理测试内容。')})
  await page.getByRole('button',{name:'安装技能',exact:true}).click()
  await expect(page.getByText('$browser-skill')).toBeVisible({timeout:20_000})
  const card = page.locator('.skill-item').filter({hasText:'$browser-skill'})
  const checkbox = await card.getByRole('checkbox').boundingBox(), title = await card.locator('strong').first().boundingBox()
  expect(Math.abs(checkbox!.y + checkbox!.height/2 - title!.y - title!.height/2)).toBeLessThan(4)
  for(const [path,heading] of [['/account','账户设置'],['/history','申请记录'],['/knowledge','Soul'],['/dialogues','我的分身'],['/messages','最近动态']] as const){
    await page.goto(path);await expect(page.getByRole('heading',{name:new RegExp(heading)}).first()).toBeVisible();await page.reload();await expect(page.locator('.shell')).toBeVisible()
  }
  await page.setViewportSize({width:390,height:844});await page.goto('/files');await expect(page.getByRole('heading',{name:'资料文件'})).toBeVisible()
})

test('streamed chat resumes after reload without duplication or script execution',async({page})=>{
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));await loginUser(page);await page.goto('/chat')
  const input=page.getByRole('textbox',{name:'聊天消息'});await expect(input).toBeEnabled({timeout:30_000})
  await input.fill('浏览器流测试');await page.getByRole('button',{name:'发送',exact:true}).click()
  await expect(page.locator('.pending-answer')).toContainText('浏览器流式',{timeout:30_000})
  await page.reload();await expect(page.getByText('浏览器流式回答完成')).toHaveCount(1,{timeout:30_000})
  await page.reload();await expect(page.getByText('浏览器流式回答完成')).toHaveCount(1)
  expect(await page.evaluate(()=>Object.prototype.hasOwnProperty.call(window,'__xss_test'))).toBe(false);expect(errors).toEqual([])
})

test('administrator can access and reload the console',async({page})=>{
  await login(page,'admin','browser-admin-password','/admin')
  await expect(page.getByRole('heading',{name:/管理/}).first()).toBeVisible();await page.reload();await expect(page.locator('.shell')).toBeVisible()
})
