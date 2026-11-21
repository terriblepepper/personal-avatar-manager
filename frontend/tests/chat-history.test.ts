import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { expect, it, vi } from 'vitest'
import ChatView from '../src/views/ChatView.vue'

const {request, notifyError, observe} = vi.hoisted(() => ({request:vi.fn(), notifyError:vi.fn(), observe:vi.fn()}))
const route = reactive({query:{conversation:'A'}})
vi.mock('vue-router', () => ({useRoute:()=>route, useRouter:()=>({replace:vi.fn()})}))
vi.mock('../src/api/client', () => ({request,notifyError}))
vi.mock('../src/api/generations', () => ({observeGeneration:observe,submitAndObserve:vi.fn()}))

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
