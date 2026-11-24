import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import DialoguesView from '../src/views/DialoguesView.vue'
const {request,notifyError}=vi.hoisted(()=>({request:vi.fn(),notifyError:vi.fn()}))
vi.mock('../src/api/client',()=>({request,notifyError}))
import { reactive } from 'vue'
const route = reactive({query:{session:'session-1'}})
vi.mock('vue-router',()=>({useRoute:()=>route,useRouter:()=>({replace:vi.fn()})}))
afterEach(()=>vi.useRealTimers())
it('retries a failed message with the same request ID',async()=>{
  let sends=0
  request.mockImplementation(async(path:string,method:string)=>{
    if(path==='/me/skills')return []
    if(path==='/agent-dialogue-settings')return {max_rounds:10}
    if(path?.startsWith('/agent-sessions?'))return {items:[],offset:0,total:0}
    if(path?.startsWith('/agent-sessions/session-1/tasks'))return {items:[],offset:0,total:0}
    if(path==='/agent-sessions/session-1')return {peer_name:'peer',resource_conversation_id:'resource',messages:[],has_more:false}
    if(path==='/agent-sessions/session-1/messages'&&method==='POST'){if(++sends===1)throw Error('network');return {}}
    return {items:[]}
  })
  const wrapper=mount(DialoguesView,{attachTo:document.body,global:{stubs:{ChatFilesManager:true,MarkdownContent:true}}});await flushPromises()
  const panel=new DOMWrapper(document.querySelector<HTMLElement>('#agent-dialogue-window')!)
  await panel.get('textarea').setValue('hello')
  const send=panel.findAll('button').find(button=>button.text()==='发送')!
  await send.trigger('click');await flushPromises()
  await send.trigger('click');await flushPromises()
  const writes=request.mock.calls.filter(call=>call[0]==='/agent-sessions/session-1/messages')
  expect(writes).toHaveLength(2);expect(writes[0]?.[2].request_id).toBeTruthy();expect(writes[1]?.[2].request_id).toBe(writes[0]?.[2].request_id)
  wrapper.unmount()
})
it('renders task boundaries, message attachments and public tool details',async()=>{
  request.mockImplementation(async(path:string)=>{
    if(path==='/me/skills')return []
    if(path==='/agent-dialogue-settings')return {max_rounds:10}
    if(path?.startsWith('/agent-sessions?'))return {items:[],offset:0,total:0}
    if(path?.startsWith('/agent-sessions/session-1/tasks'))return {items:[],offset:0,total:0}
    if(path==='/agent-sessions/session-1')return {peer_name:'peer',resource_conversation_id:'resource',has_more:false,messages:[{turn:1,dialogue_id:'d',speaker_name:'peer',speaker_role:'agent',content:'hello',tool_activity:[{id:'tool',name:'Python',status:'completed',output:'42'}]},
      {turn:2,dialogue_id:'d',speaker_role:'user',speaker_name:'Alice',is_own:true,content:'first'},
      {turn:3,dialogue_id:'d',speaker_role:'user',speaker_name:'Bob',is_own:false,content:'second'}],message_tasks:[{id:'d',delivery_mode:'message',status:'ended',created_at:'start',ended_at:'end',files:[{id:'f',message_id:1,filename:'result.txt',download_url:'/api/conversations/resource/files/f'}]}]}
    return {items:[]}
  })
  const wrapper=mount(DialoguesView,{global:{stubs:{Teleport:true,ChatFilesManager:true,MarkdownContent:true}}});await flushPromises()
  expect(wrapper.text()).toContain('请求开始');expect(wrapper.text()).toContain('请求结束')
  expect(wrapper.get('.peer-user-note.peer-own small').text()).toBe('我补充要求')
  expect(wrapper.get('.peer-user-note.peer-other small').text()).toBe('Bob补充要求')
  expect(wrapper.text()).toContain('Python');expect(wrapper.text()).toContain('42')
  expect(wrapper.get('.source-trigger').text()).toContain('1 份文件')
  expect(wrapper.get('.source-chip').text()).toContain('result.txt')
  wrapper.unmount()
})

it('catches up past one page, backs off at rest and drops history after a visibility change', async () => {
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval']})
  let last = 1, revision = 'first', state = 'running', forbidden = false
  const msg = (turn:number) => ({turn,dialogue_id:'d',speaker_name:'peer',speaker_role:'agent',content:'line-'+turn,is_own:false})
  const rows = (from:number, to:number) => Array.from({length:Math.max(0,to-from+1)},(_,i)=>msg(from+i))
  const path = '/agent-sessions/session-1'
  request.mockReset().mockImplementation(async (url:string) => {
    if (url === '/me/skills') return []
    if (url === '/agent-dialogue-settings') return {max_rounds:10}
    if (url.startsWith(path) && !url.includes('/tasks')) {
      if (forbidden) throw Object.assign(new Error('no longer visible'),{status:404})
      const after = new URL(url,'http://test').searchParams.get('after')
      const floor = revision === 'first' ? 1 : 27
      const page = after ? rows(Math.max(floor,Number(after)+1),Math.min(last,Number(after)+10)) : rows(Math.max(floor,last-9),last)
      return {history_revision:revision,task_page:{items:[],total:0,offset:0},peer_name:'peer',
        resource_conversation_id:'',chat:{id:'d',status:state},messages:page,has_more:page.length===10}
    }
    return {items:[],total:0,offset:0}
  })
  const view = mount(DialoguesView,{global:{stubs:{Teleport:true,FilePreview:true,ChatFilesManager:true,ConversationTools:true,ChatMarkdown:{name:'ChatMarkdown',props:['source'],template:'<p>{{source}}</p>'}}}})
  try {
    await flushPromises()
    expect(view.findAll('.peer-message')).toHaveLength(1)
    view.getComponent({name:'ChatMarkdown'}).vm.$emit('preview', {id:'f',filename:'report.txt',download_url:'/files/f'})
    await flushPromises()
    expect(view.findComponent({name:'FilePreview'}).exists()).toBe(true)
    window.dispatchEvent(new Event('online')); await flushPromises()
    expect(view.findComponent({name:'FilePreview'}).exists()).toBe(true)
    last = 26; state = 'ended'
    await vi.advanceTimersByTimeAsync(1500); await flushPromises()
    expect(view.findAll('.peer-message p').map(p=>p.text())).toEqual(rows(1,26).map(m=>m.content))
    expect(view.get('.dialogue-state').text()).toContain('任务已结束')
    request.mockClear()
    await vi.advanceTimersByTimeAsync(29999)
    expect(request.mock.calls.filter(([url])=>url.startsWith(path))).toHaveLength(0)
    last = 27; revision = 'cleared'
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(view.findAll('.peer-message p').map(p=>p.text())).toEqual(['line-27'])
    expect(view.findComponent({name:'FilePreview'}).exists()).toBe(false)
    forbidden = true
    window.dispatchEvent(new Event('online')); await flushPromises()
    expect(view.find('#agent-dialogue-window').exists()).toBe(false)
  } finally { view.unmount() }
})

it.each(['success','error'])('isolates late session reads and writes after navigation (%s)', async outcome => {
  const path = '/agent-sessions/'
  const snapshot = (peer:string) => ({peer_name:peer,resource_conversation_id:'',messages:[],
    task_page:{items:[],offset:0,total:0},has_more:false})
  let finishRead!:()=>void, finishWrite!:()=>void, finishSecond!:()=>void
  let followup = false
  const late = new Error('previous session')
  request.mockReset().mockImplementation(async (url:string) => {
    if (url === path+'session-1' && followup) return new Promise((resolve,reject) => {
      finishRead = () => outcome === 'success' ? resolve(snapshot('stale peer')) : reject(Object.assign(late,{status:404}))
    })
    if (url === path+'session-1') return snapshot('peer A')
    if (url === path+'session-2') return snapshot('peer B')
    if (url === path+'session-1/messages') return new Promise((resolve,reject) => {
      finishWrite = () => outcome === 'success' ? resolve({}) : reject(late)
    })
    if (url === path+'session-2/messages') return new Promise(resolve => { finishSecond = () => resolve({}) })
    if (url === '/me/skills') return []
    if (url === '/agent-dialogue-settings') return {max_rounds:10}
    return {items:[],total:0,offset:0}
  })
  notifyError.mockClear()
  const view = mount(DialoguesView,{global:{stubs:{Teleport:true,ChatFilesManager:true,ConversationTools:true}}})
  const send = () => view.findAll('button').find(button=>button.text()==='发送')!
  try {
    await flushPromises()
    followup = true
    window.dispatchEvent(new Event('online')); await flushPromises()
    await view.get('textarea').setValue('A content'); await send().trigger('click')
    route.query.session = 'session-2'; await flushPromises()
    expect(view.get('#agent-dialogue-window').text()).toContain('peer B')
    expect(view.get('textarea').element.value).toBe('')
    await view.get('textarea').setValue('B content'); await send().trigger('click')
    finishRead(); finishWrite(); await flushPromises()
    expect(view.get('#agent-dialogue-window').text()).toContain('peer B')
    expect(view.get('textarea').element.value).toBe('B content')
    expect(notifyError).not.toHaveBeenCalled()
    await send().trigger('click')
    expect(request.mock.calls.filter(([url])=>url===path+'session-2/messages')).toHaveLength(1)
    finishSecond(); await flushPromises()
    expect(view.get('textarea').element.value).toBe('')
  } finally { view.unmount(); route.query.session='session-1' }
})

it('loads older turns on upward input, preserves the reading position and ignores late pages after navigation', async () => {
  let last = 50, otherLast = 120
  let resolveOlder!: (response: unknown) => void
  const snapshot = (first: number, end: number) => ({
    peer_name:'peer', resource_conversation_id:'', history_revision:'rev', has_more:true,
    task_page:{items:[],total:0,offset:0}, messages:Array.from({length:end-first+1}, (_,i) => ({
      turn:first+i, dialogue_id:'d', speaker_name:'peer', speaker_role:'agent', content:String(first+i),
    })),
  })
  request.mockReset().mockImplementation(async (path: string) => {
    if (path === '/me/skills') return []
    if (path === '/agent-dialogue-settings') return {max_rounds:10}
    if (path.includes('?before=')) return new Promise(resolve => { resolveOlder = resolve })
    if (path === '/agent-sessions/session-1') return snapshot(last-9,last)
    if (path === '/agent-sessions/session-2') return snapshot(otherLast-9,otherLast)
    return {items:[],offset:0,total:0}
  })
  const view = mount(DialoguesView, {attachTo:document.body, global:{stubs:{ ChatFilesManager:true, ConversationTools:true,
    ChatMarkdown:{props:['source'],template:'<p>{{source}}</p>'}}}})
  try {
    const log = new DOMWrapper(document.querySelector<HTMLElement>('#agent-dialogue-window .dialogue-log')!)
    const turns = () => [...log.element.querySelectorAll<HTMLElement>('.peer-message')]
    let top = 0
    Object.defineProperties(log.element, {
      clientHeight:{value:60}, scrollHeight:{get:() => turns().length * 20},
      scrollTop:{get:() => top,set:value => {top = Math.max(0,Math.min(value,log.element.scrollHeight-60))}},
    })
    await flushPromises()
    expect(top).toBe(log.element.scrollHeight-log.element.clientHeight)
    const original = turns()[0]!
    const anchorTop = () => turns().indexOf(original)*20-top
    vi.spyOn(original, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0,anchorTop(),100,20))
    const pages = () => request.mock.calls.filter(([path]) => path.includes('?before='))
    log.element.scrollTop = 0
    await log.trigger('wheel',{deltaY:-20})
    await log.trigger('keydown',{key:'PageUp'})
    expect(pages().map(([path])=>path)).toEqual(['/agent-sessions/session-1?before=41'])
    expect(log.text()).not.toContain('加载更早消息')
    expect(log.attributes('tabindex')).toBe('0')
    last = 51
    window.dispatchEvent(new Event('online')); await flushPromises()
    expect(top).toBe(0)
    resolveOlder(snapshot(31,40)); await flushPromises()
    expect(turns().map(row => Number(row.querySelector('p')?.textContent))).toEqual(
      Array.from({length:21},(_,i)=>i+31))
    expect(anchorTop()).toBe(0)

    log.element.scrollTop = 0
    await log.trigger('touchstart',{touches:[{clientY:60}]})
    await log.trigger('touchmove',{touches:[{clientY:15}]})
    expect(pages()).toHaveLength(1)
    await log.trigger('touchstart',{touches:[{clientY:15}]})
    await log.trigger('touchmove',{touches:[{clientY:60}]})
    expect(pages()).toHaveLength(2)
    route.query.session = 'session-2'; await flushPromises()
    expect(pages()[1]?.[3].aborted).toBe(true)
    resolveOlder(snapshot(21,30)); await flushPromises()
    expect(turns()[0]?.querySelector('p')?.textContent).toBe('111')

    log.element.scrollTop = 0
    await log.trigger('keydown',{key:'Home'})
    expect(pages()[2]?.[0]).toBe('/agent-sessions/session-2?before=111')
    resolveOlder({...snapshot(109,110), has_more:false}); await flushPromises()
    await log.trigger('wheel',{deltaY:-25})
    expect(pages()).toHaveLength(3)
    log.element.scrollTop = log.element.scrollHeight
    otherLast++
    window.dispatchEvent(new Event('online')); await flushPromises()
    expect(turns().at(-1)?.querySelector('p')?.textContent).toBe('121')
    expect(top).toBe(log.element.scrollHeight-log.element.clientHeight)
  } finally { view.unmount(); route.query.session = 'session-1' }
})

it.each([undefined, ''])('discards previous messages when history revision is %s', async revision => {
  let first = 1
  request.mockReset().mockImplementation(async (path: string) => {
    if (path === '/me/skills') return []
    if (path === '/agent-dialogue-settings') return {max_rounds: 10}
    if (path !== '/agent-sessions/session-1') return {items: [], total: 0, offset: 0}
    const messages = Array.from({length: 10}, (_, i) => ({
      turn: i + first, dialogue_id: 'chat', speaker_name: 'Peer', speaker_role: 'agent', content: String(i + first),
    }))
    return {peer_name: 'Peer', resource_conversation_id: '', messages, has_more: true,
      ...(revision === undefined ? {} : {history_revision: revision})}
  })
  const view = mount(DialoguesView, {global: {stubs: {Teleport: true, ChatFilesManager: true,
    ConversationTools: true, ChatMarkdown: {props: ['source'], template: '<p>{{source}}</p>'}}}})
  const turns = () => view.findAll('.peer-message p').map(node => Number(node.text()))
  try {
    await flushPromises()
    expect(turns()).toContain(1)
    first = 2
    window.dispatchEvent(new Event('online'))
    await flushPromises()
    expect(turns()).toEqual([2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
  } finally { view.unmount() }
})
