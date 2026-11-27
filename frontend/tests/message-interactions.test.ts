import { ElMessageBox } from 'element-plus'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import ChatView from '../src/views/ChatView.vue'
import DialoguesView from '../src/views/DialoguesView.vue'
import MessagesView from '../src/views/MessagesView.vue'
import { createNotificationInbox, notificationInboxKey } from '../src/composables/useNotificationInbox'
import NotificationBell from '../src/components/NotificationBell.vue'
import ChatIntent from '../src/components/collaboration/ChatIntent.vue'

vi.mock('../src/stores/session', () => ({useSessionStore: () => ({user:{id:'viewer',role:'user'}})}))
const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', () => ({request, notifyError: vi.fn()}))
vi.mock('vue-router', async importOriginal => ({ ...await importOriginal<typeof import('vue-router')>(), useRoute: () => ({query:{session:'s'}}), useRouter: () => ({replace:vi.fn(),push:vi.fn()}) }))
enableAutoUnmount(afterEach)
afterEach(() => {
  document.body.replaceChildren()
  window.getSelection()?.removeAllRanges()
  Reflect.deleteProperty(navigator,'clipboard')
  request.mockReset()
})

const body = '第一段 **强调**。\n\n第二段。\n\n```mermaid\ngraph TD; A-->B\n```\n\n> 引用来源：\n> 私有来源 · RAG'
const menuitem = () => document.querySelector<HTMLButtonElement>('[role=menuitem]')
async function chatRequest(path:string) {
  if (path.startsWith('/conversations?')) return {items:[{id:'c',title:'本人会话'}],default_id:'c',embedding_configured:false}
  if (path.startsWith('/chat?')) return [{id:1,role:'assistant',content:body,responder_name:'助手',tool_events:[{id:'tool',name:'敏感工具',input:'调用参数'}]}]
  return null
}

it('copies the rendered personal reply and preserves native selection and nested controls', async () => {
  request.mockImplementation(chatRequest)
  const writeText=vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText}})
  const view=mount(ChatView,{attachTo:document.body,global:{stubs:{ChatSettings:true,ChatFilesManager:true,ConversationTools:true,ElSelect:true,RouterLink:{props:['to'],template:'<a :href="to"><slot/></a>'}}}})
  await flushPromises()
  const bubble=view.get<HTMLElement>('[data-chat-message]'), paragraph=bubble.get('p')
  const range=document.createRange()
  range.selectNodeContents(paragraph.element)
  window.getSelection()!.addRange(range)
  await paragraph.trigger('contextmenu')
  expect(menuitem()).toBeNull()
  window.getSelection()!.removeAllRanges()
  for (const selector of ['.source-trigger','.tool-activity summary']) {
    await bubble.get(selector).trigger('contextmenu')
    expect(menuitem()).toBeNull()
  }
  await paragraph.trigger('contextmenu')
  await flushPromises()
  expect(menuitem()?.textContent).toContain('复制文字')
  expect(document.activeElement).toBe(menuitem())
  menuitem()!.click()
  await flushPromises()
  expect(writeText).toHaveBeenCalledWith(expect.stringMatching(/^第一段 强调。\n+第二段。\n+graph TD; A-->B$/))
  expect(document.activeElement).toBe(bubble.element)
  expect(view.get('a[href="/models"]').text()).toBe('前往配置')
})

it('opens peer copy from the keyboard, restores focus, and falls back to a temporary textarea', async () => {
  request.mockImplementation((path:string) => {
    if(path==='/agent-sessions/s') return {task_page:{items:[],offset:0,total:0},peer_name:'对方',resource_conversation_id:'c',embedding_status:{configured:true,peerConfigured:false},messages:[{turn:1,speaker_role:'user',speaker_name:'我',content:'补充正文'}]}
    if(path==='/me/skills')return []
    return {items:[],max_rounds:5,offset:0,total:0}
  })
  let copied=''
  Object.defineProperty(document,'execCommand',{configurable:true,value:vi.fn((cmd:string)=>{
    expect(cmd).toBe('copy')
    copied=(document.activeElement as HTMLTextAreaElement).value
    return true
  })})
  try {
    const view=mount(DialoguesView,{attachTo:document.body,global:{stubs:{Teleport:true,ChatFilesManager:true}}})
    await flushPromises()
    const bubble=view.get<HTMLElement>('.peer-user-note')
    bubble.element.focus()
    await bubble.trigger('keydown',{key:'F10',shiftKey:true})
    await flushPromises()
    expect(menuitem()?.textContent).toContain('复制文字')
    menuitem()!.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))
    await flushPromises()
    expect(menuitem()).toBeNull()
    expect(document.activeElement).toBe(bubble.element)
    await bubble.trigger('keydown',{key:'ContextMenu'})
    await flushPromises()
    menuitem()!.click()
    await flushPromises()
    expect(copied).toBe('补充正文')
    expect(document.activeElement).toBe(bubble.element)
    expect(view.get('.embedding-notice').text()).toContain('对方尚未配置嵌入模型')
  } finally { Reflect.deleteProperty(document,'execCommand') }
})

it('marks one notification and confirms deletion from the compact feed', async () => {
  const item={id:7,title:'回复',content:'已处理',created_at:'',read_at:null}
  request.mockImplementation((path:string)=>path.startsWith('/notifications?')?{items:[item],total:1,offset:0}:{})
  const view=mount(MessagesView,{props:{management:false},attachTo:document.body})
  await flushPromises()
  const row=view.get<HTMLElement>('.notification-row')
  await row.get('button').trigger('contextmenu')
  await flushPromises()
  const action=(label:string) => [...document.querySelectorAll<HTMLButtonElement>('[role=menuitem]')].find(el=>el.textContent?.includes(label))
  expect(action('查看消息')).toBeTruthy()
  action('标记已读')!.click()
  await flushPromises()
  expect(request).toHaveBeenCalledWith('/notifications/batch','POST',{ids:[7],action:'read'},expect.any(AbortSignal))
  const confirm=vi.spyOn(ElMessageBox,'confirm').mockRejectedValue('cancel')
  await row.trigger('keydown',{key:'F10',shiftKey:true})
  await flushPromises()
  action('删除消息')!.click()
  await flushPromises()
  expect(confirm).toHaveBeenCalled()
  expect(request.mock.calls.filter(call=>call[2]?.action==='delete')).toHaveLength(0)
})

it('opens the task named by the confirmed intent', async () => {
  request.mockResolvedValue({session_id:'peer-session'})
  const card=mount(ChatIntent,{props:{intent:{id:'i',status:'confirmed',task_id:'task-7'}}})
  await card.get('button').trigger('click')
  await flushPromises()
  expect(card.emitted('opened')).toEqual([['peer-session','task-7']])
})

it('keeps the editable draft open while the conversation refreshes its intent status', async () => {
  let status='pending'
  request.mockImplementation(async(path:string) => {
    if (path.startsWith('/chat?')) return [{id:5,role:'assistant',content:'',intent:{id:'i',status}}]
    if (path.endsWith('/choose')) {
      status='editing'
      return {id:'i',conversation_id:'c',objective:'任务目标'}
    }
    if (path.startsWith('/conversations?')) return {...await chatRequest(path), embedding_configured:true}
    return chatRequest(path)
  })
  const view=mount(ChatView,{global:{stubs:{ChatSettings:true,ChatFilesManager:true,ConversationTools:true,ElSelect:true,IntentDialog:{props:['intent'],template:'<div role="dialog">{{intent.objective}}</div>'}}}})
  await flushPromises()
  await view.findAll('button').find(button=>button.text()==='确认并填写')!.trigger('click')
  await flushPromises()
  expect(view.get('[role=dialog]').text()).toBe('任务目标')
  expect(request.mock.calls.some(call=>call[0].endsWith('/confirm'))).toBe(false)
})

it('keeps selections across pages and refreshes the unread badge after a batch',async()=>{
  let count=21
  request.mockImplementation(async(path:string,method:string) => {
    if(path==='/notifications/unread-count')return {unread:count}
    if(method==='POST'){count=10;return {}}
    const offset=Number(new URLSearchParams(path.split('?')[1]).get('offset'))
    return {items:Array.from({length:10},(_,i)=>({id:offset+i+1,title:'消息'+(offset+i+1),content:'内容'})),total:21,offset,unread:count}
  })
  const global = {provide:{[notificationInboxKey]:createNotificationInbox()}}
  const view=mount(MessagesView,{global})
  const bell=mount(NotificationBell,{global})
  await flushPromises()
  await view.get('input[aria-label="选择：消息1"]').setValue(true)
  await view.findAll('button').find(button=>button.text()==='下一页')!.trigger('click')
  await flushPromises()
  await view.get('.privacy input').setValue(true)
  await view.findAll('button').find(button=>button.text()==='批量已读')!.trigger('click')
  await flushPromises()
  expect(request).toHaveBeenCalledWith('/notifications/batch','POST',{ids:[1,11,12,13,14,15,16,17,18,19,20],action:'read'},expect.any(AbortSignal))
  expect(bell.get('#message-unread-badge').text()).toBe('10')
})

it('keeps the request identity after uncertainty and never restores a confirmed message as a draft', async () => {
  const generations = await import('../src/api/generations')
  const submit = vi.spyOn(generations, 'submitAndObserve')
    .mockRejectedValueOnce(new Error('等待服务器确认'))
    .mockResolvedValue({reply:'已保存的回答', state:'succeeded', done:true})
  let historyUnavailable = false
  request.mockImplementation(async (path:string) => {
    if (path.startsWith('/chat?') && historyUnavailable) throw new Error('记录暂不可用')
    return chatRequest(path)
  })
  const view = mount(ChatView, {global:{stubs:{ChatSettings:true,ChatFilesManager:true,ConversationTools:true,RouterLink:true}}})
  await flushPromises()
  const input = view.get<HTMLTextAreaElement>('.personal-compose textarea')
  const form = view.get('.personal-compose')
  await input.setValue('同一个问题')
  await form.trigger('submit'); await flushPromises()
  expect(input.element.value).toBe('同一个问题')
  historyUnavailable = true
  await form.trigger('submit'); await flushPromises()
  expect(submit.mock.calls[1][0].request_id).toBe(submit.mock.calls[0][0].request_id)
  expect(input.element.value).toBe('')
  expect(view.get('[role="alert"]').text()).toContain('记录暂不可用')
  expect(view.text()).toContain('已保存的回答')
})

it('polls an idle badge once per 30 seconds and uses the open inbox response', async () => {
  vi.useFakeTimers({toFake:['setInterval','clearInterval','setTimeout','clearTimeout']})
  try {
    let unread = 3, slow = false
    let finish!: (result:{unread:number}) => void
    request.mockImplementation(async (path:string) => {
      if (path === '/notifications/unread-count') return slow ? new Promise(resolve => { finish = resolve }) : {unread}
      return {items:[],total:0,offset:0,unread}
    })
    const bell = mount(NotificationBell, {global:{provide:{[notificationInboxKey]:createNotificationInbox()}}})
    await flushPromises()
    const counts = () => request.mock.calls.filter(([path]) => path === '/notifications/unread-count').length
    expect(counts()).toBe(1)
    await vi.advanceTimersByTimeAsync(29999)
    expect(counts()).toBe(1)
    slow = true
    await vi.advanceTimersByTimeAsync(1)
    expect(counts()).toBe(2)
    unread = 5
    await bell.get('#message-hub-ball').trigger('click')
    await flushPromises()
    finish({unread:99})
    await flushPromises()
    expect(bell.get('#message-unread-badge').text()).toBe('5')
    unread = 7
    await vi.advanceTimersByTimeAsync(30000)
    expect(bell.get('#message-unread-badge').text()).toBe('7')
    expect(counts()).toBe(2)
    slow = false; unread = 8
    await bell.get('#message-hub-ball').trigger('click')
    await flushPromises()
    expect(counts()).toBe(3)
    expect(bell.get('#message-unread-badge').text()).toBe('8')
    bell.unmount()
  } finally { vi.useRealTimers() }
})
