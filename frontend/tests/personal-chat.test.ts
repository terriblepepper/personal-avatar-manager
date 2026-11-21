import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ChatFilesManager from '../src/components/chat/ChatFilesManager.vue'
import ComposerToolbar from '../src/components/chat/ComposerToolbar.vue'
import ChatView from '../src/views/ChatView.vue'

const {request, notifyError, observe} = vi.hoisted(() => ({request:vi.fn(), notifyError:vi.fn(), observe:vi.fn()}))
const route = reactive({query:{conversation:'A'}})
vi.mock('vue-router', () => ({useRoute:()=>route, useRouter:()=>({replace:vi.fn()})}))
vi.mock('../src/api/client', () => ({request,notifyError}))
vi.mock('../src/api/generations', () => ({observeGeneration:observe,submitAndObserve:vi.fn()}))

beforeEach(() => { request.mockReset(); notifyError.mockReset(); observe.mockReset() })

it('keeps paged messages after generation and discards late history when switching conversations', async () => {
  let finish!:()=>void, page!:()=>void
  const rows = (start:number) => Array.from({length:20},(_,i)=>({id:start+i,role:'assistant',content:String(start+i)}))
  let latest = 21
  const pages = () => request.mock.calls.filter(([path])=>path.includes('before='))
  request.mockImplementation(async (path:string) => {
    if (path.startsWith('/conversations?')) return {items:[{id:'A'},{id:'B'}],default_id:'A'}
    if (path.startsWith('/chat/pending?')) return path.endsWith('A') ? {request_id:'job',done:false} : null
    if (path.startsWith('/chat?')) {
      const query = new URLSearchParams(path.split('?')[1])
      if (query.has('before')) return new Promise(resolve=>{page=()=>resolve(rows(Number(query.get('before'))-20))})
      return rows(query.get('conversation_id')==='A'? latest:201)
    }
    return path==='/me/skills' ? [] : {}
  })
  observe.mockImplementation(async()=>new Promise(resolve=>{finish=()=>resolve({request_id:'job',done:true})}))
  const view = mount(ChatView,{global:{stubs:{ChatFilesManager:true,ConversationTools:true,ChatSettings:true,
    ChatMarkdown:true,MarkdownContent:true,ComposerInput:true,FilePreview:true}}})
  try {
    await flushPromises()
    const log = view.get('.chatlog')
    const ids = () => log.findAll('[data-chat-message]').map(row=>Number(row.attributes('data-chat-message')))
    expect(ids()).toEqual(rows(21).map(r=>r.id))
    await log.trigger('wheel',{deltaY:-12}); await log.trigger('keydown',{key:'PageUp'})
    expect(pages()).toHaveLength(1)
    page(); await flushPromises()
    latest=23; finish(); await flushPromises()
    expect(ids()).toEqual(Array.from({length:42},(_,i)=>i+1))
    await log.get('[data-chat-message]').trigger('keydown',{key:'Home'})
    expect(pages()).toHaveLength(2)
    route.query.conversation='B'; await flushPromises()
    expect(pages()[1]?.[3].aborted).toBe(true)
    page(); await flushPromises()
    expect(ids()).toEqual(rows(201).map(r=>r.id))
    expect(notifyError).not.toHaveBeenCalled()
  } finally { view.unmount(); route.query.conversation='A' }
})

it('dismisses one menu level with Escape and closes the flyout after an action or outside click', async () => {
  const view = mount(ComposerToolbar, {
    attachTo: document.body,
    props: { label: '会话操作' },
    slots: { default: '<details class="composer-tool chat-command-menu"><summary>更多</summary><div class="chat-command-panel"><details class="chat-menu-section"><summary class="chat-menu-item">表达风格学习</summary><button>立即学习</button></details><button class="chat-menu-item">重命名</button></div></details>' }
  })
  try {
    const [menu, section] = view.findAll<HTMLDetailsElement>('details')
    const [trigger, child] = view.findAll('summary')
    expect(view.attributes('aria-label')).toBe('会话操作')
    await trigger!.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(child!.element)
    await child!.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(view.get('button.chat-menu-item').element)
    section!.element.open = true
    await view.get('button').trigger('keydown', { key: 'Escape' })
    expect(section!.element.open).toBe(false)
    expect(menu!.element.open).toBe(true)
    expect(document.activeElement).toBe(child!.element)
    await child!.trigger('keydown', { key: 'Escape' })
    expect(menu!.element.open).toBe(false)
    expect(document.activeElement).toBe(trigger!.element)
    menu!.element.open = true
    await view.get('button.chat-menu-item').trigger('click')
    expect(menu!.element.open).toBe(false)
    menu!.element.open = section!.element.open = true
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    expect(menu!.element.open || section!.element.open).toBe(false)
    menu!.element.open = true
    document.body.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    expect(menu!.element.open).toBe(false)
  } finally { view.unmount() }
})

const item=(id:string)=>({id,filename:id+'.txt',content_type:'text/plain',size:1,referenced:true,description:'',download_url:'/api/conversations/a/files/'+id})
describe('conversation file selection',()=>{
  it('clears stale selection on conversation change and ignores late responses',async()=>{
    let resolveOld:(files:ReturnType<typeof item>[])=>void=()=>{}
    request.mockImplementation((path:string)=>path==='/conversations/a/files'?new Promise(resolve=>{resolveOld=resolve}):Promise.resolve([item('new')]))
    const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}})
    await wrapper.setProps({conversationId:'b'});await flushPromises();resolveOld([item('old')]);await flushPromises()
    expect(wrapper.emitted('selected')?.at(-1)).toEqual([['new']]);expect(wrapper.text()).not.toContain('old.txt')
    wrapper.unmount()
  })
  it('rejects uploads that exceed five referenced files',async()=>{
    request.mockResolvedValue(Array.from({length:5},(_,i)=>item(String(i))))
    const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}});await flushPromises()
    const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['a'],'new.txt')]})
    await input.trigger('change');await flushPromises()
    expect(request.mock.calls.filter(call=>call[1]==='POST')).toHaveLength(0);expect(wrapper.text()).toContain('最多同时引用五份')
    wrapper.unmount()
  })
})

it('blocks sending while uploading and releases the state on conversation switch',async()=>{
  request.mockImplementation((path:string,method?:string)=>{
    if(method==='POST')return new Promise(()=>{})
    return Promise.resolve([])
  })
  const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}});await flushPromises()
  const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['x'],'file.txt')]})
  await input.trigger('change');await flushPromises();expect(wrapper.emitted('uploading')?.at(-1)).toEqual([true])
  await wrapper.setProps({conversationId:'b'});await flushPromises();expect(wrapper.emitted('uploading')?.at(-1)).toEqual([false]);expect(wrapper.emitted('selected')?.at(-1)).toEqual([[]])
  wrapper.unmount()
})

it('uploads multipart data and emits the returned persistent references',async()=>{
  request.mockImplementation(async (_path:string,method?:string)=>method==='POST'?{items:[item('uploaded')]}:[])
  const wrapper=mount(ChatFilesManager,{props:{conversationId:'a',busy:false}});await flushPromises()
  const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['x'],'file.txt')]})
  await input.trigger('change');await flushPromises()
  const call=request.mock.calls.find(c=>c[1]==='POST');expect(call?.[0]).toBe('/conversations/a/files');expect(call?.[2]).toBeInstanceOf(FormData)
  expect(wrapper.emitted('selected')?.at(-1)).toEqual([['uploaded']]);expect(wrapper.emitted('uploading')?.at(-1)).toEqual([false]);wrapper.unmount()
})
