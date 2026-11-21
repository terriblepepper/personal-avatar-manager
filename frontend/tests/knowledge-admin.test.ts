import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MemoryEditor from '../src/components/knowledge/MemoryEditor.vue'
import MigrationPanel from '../src/components/admin/MigrationPanel.vue'
import type { Build } from '../src/components/knowledge/types'
const request=vi.hoisted(()=>vi.fn())
vi.mock('../src/api/client',()=>({request,notifyError:vi.fn()}))
const build:Build={id:'b1',status:'ready',origin:'user',prompt:'',updated_at:'build-v1',created_at:'',stage:'',progress:100,phase:'',document:{voice:'a',personality:'',expression:'',appearance:'',boundaries:'',examples:'',uncertainties:''},question_plan:[],bank_answers:{},interview:[],asset_ids:[],interview_rounds:3}
beforeEach(() => { request.mockReset() })
describe('knowledge and administration safety',()=>{
  it('sends only changed memory with both original revisions',async()=>{
    request.mockImplementation(async (path:string)=>path?.endsWith('/memory-tree')?{revision:'tree-v1',nodes:[{id:'a'.repeat(64),group:'day',title:'old',summary:'',content:'content'}]}:{})
    const wrapper=mount(MemoryEditor,{props:{build}})
    wrapper.get<HTMLDetailsElement>('details').element.open = true
    await wrapper.get('details').trigger('toggle');await flushPromises()
    await wrapper.findAll('input').find(input=>input.element.value==='old')!.setValue('new')
    await wrapper.findAll('button').find(button=>button.text().includes('保存记忆修订'))!.trigger('click');await flushPromises()
    expect(request).toHaveBeenCalledWith('/me/knowledge/builds/b1/refine','POST',expect.objectContaining({revision:'build-v1',memory_revision:'tree-v1',memory_edits:[expect.objectContaining({title:'new',deleted:false})]}))
  })
  it('requires a preview and explicit identity confirmation before commit',async()=>{
    request.mockImplementation(async(path:string)=>path?.startsWith('/admin/identity-migration?')?{items:[{id:'u',username:'alice',name:'Alice',is_root:false,migration_status:'unlinked'}],total:1,offset:0,issuer:'issuer',oidc_enabled:false}:path?.endsWith('/preview')?{preview_token:'token',issuer:'issuer',subject:'subject',target:{},oidc_enabled:false}:{message:'ok'})
    const wrapper=mount(MigrationPanel);await flushPromises()
    await wrapper.findAll('button').find(button=>button.text()==='配置迁移')!.trigger('click')
    await wrapper.findAll('input').find(input=>input.attributes('maxlength')==='255')!.setValue('subject')
    await wrapper.findAll('button').find(button=>button.text()==='预览迁移')!.trigger('click');await flushPromises()
    await wrapper.findAll('button').find(button=>button.text()==='确认迁移')!.trigger('click');await flushPromises()
    expect(request.mock.calls.some(call=>call[0]==='/admin/identity-migration/commit')).toBe(false)
    await wrapper.find('textarea').setValue('已核验本人')
    await wrapper.find('input[type=checkbox]').setValue(true)
    await wrapper.findAll('button').find(button=>button.text()==='确认迁移')!.trigger('click');await flushPromises()
    expect(request).toHaveBeenCalledWith('/admin/identity-migration/commit','POST',{preview_token:'token',identity_verified:true,reason:'已核验本人'})
  })
})

it('serializes questionnaire drafts and uses the server revision for the next save',async()=>{
  const {default:QuestionnaireDialog}=await import('../src/components/knowledge/QuestionnaireDialog.vue')
  vi.useFakeTimers()
  let resolveFirst:(value:{revision:string})=>void=()=>{}
  request.mockImplementation((path:string)=>path?.includes('/questionnaire')?new Promise(resolve=>{resolveFirst=resolve}):Promise.resolve({}))
  const item:Build={...build,status:'waiting_user',question_plan:[{id:'q1',category:'c',category_name:'C',category_index:1,question:'Question'}]}
  const wrapper=mount(QuestionnaireDialog,{props:{build:item},attachTo:document.body})
  await wrapper.vm.$nextTick()
  const radios=document.body.querySelectorAll<HTMLInputElement>('input[type=radio]')
  radios[0]!.click();await wrapper.vm.$nextTick();await vi.advanceTimersByTimeAsync(800)
  expect(request.mock.calls.filter(call=>call[0]?.includes('/questionnaire'))).toHaveLength(1)
  radios[1]!.click();await wrapper.vm.$nextTick();await vi.advanceTimersByTimeAsync(800)
  expect(request.mock.calls.filter(call=>call[0]?.includes('/questionnaire'))).toHaveLength(1)
  resolveFirst({revision:'v2'});await Promise.resolve();await Promise.resolve();await wrapper.vm.$nextTick()
  const calls=request.mock.calls.filter(call=>call[0]?.includes('/questionnaire'))
  expect(calls).toHaveLength(2);expect(calls[1]?.[2]).toMatchObject({revision:'v2',answers:[{id:'q1',choice:'B'}]})
  resolveFirst({revision:'v3'});await Promise.resolve();wrapper.unmount();vi.useRealTimers()
})

it('does not include unrelated referenced files in an intent',async()=>{
  const {default:ChatFilesManager}=await import('../src/components/chat/ChatFilesManager.vue')
  request.mockImplementation(async(path:string)=>path?.endsWith('/files')?[{id:'one',filename:'one.txt',referenced:true,description:'',download_url:''},{id:'two',filename:'two.txt',referenced:true,description:'',download_url:''}]:{items:[]})
  const wrapper=mount(ChatFilesManager,{props:{conversationId:'c1',temporary:true,selectedIds:['one']}});await flushPromises()
  expect(wrapper.emitted('selected')?.at(-1)).toEqual([['one']])
  expect(request.mock.calls.some(call=>call[1]==='PATCH')).toBe(false)
})

it('requires explicit selection before opening an agent communication draft',async()=>{
  const {default:ChatIntent}=await import('../src/components/collaboration/ChatIntent.vue')
  request.mockImplementation(async(path:string)=>path==='/me/skills'||path?.endsWith('/files')?[]:path?.endsWith('/choose')?{id:'i',conversation_id:'c',target_query:'',target_aic:'',objective:'hello',shared_content:'',delivery_mode:'message'}:path?.includes('/targets')?{items:[],total:0,offset:0}:{items:[]})
  const wrapper=mount(ChatIntent,{props:{intent:{id:'i',status:'pending'}}});await flushPromises()
  expect(request.mock.calls.some(call=>call[0]?.endsWith('/confirm'))).toBe(false)
  await wrapper.findAll('button').find(button=>button.text()==='确认并填写')!.trigger('click');await flushPromises()
  expect(request).toHaveBeenCalledWith('/agent-intents/i/choose','POST',{delivery_mode:'message'})
  expect(request.mock.calls.some(call=>call[0]?.endsWith('/confirm'))).toBe(false)
})

it('keeps review selections across pages and sends one atomic batch',async()=>{
  const {default:ReviewsPanel}=await import('../src/components/admin/ReviewsPanel.vue')
  const row=(id:string)=>({id,kind:'profile',name:id,username:id,avatar_name:'',category:'create',status:'pending',created_at:''})
  request.mockImplementation(async(path:string,method?:string)=>path?.startsWith('/admin/review-queue?')?{items:[row(path.includes('offset=10')?'b':'a')],total:11,offset:path.includes('offset=10')?10:0}:method==='POST'?{}:{})
  const wrapper=mount(ReviewsPanel,{attachTo:document.body});await flushPromises()
  await wrapper.findAll('input[type=checkbox]')[1]!.setValue(true)
  await wrapper.findAll('button').find(button=>button.text()==='下一页')!.trigger('click');await flushPromises()
  await wrapper.findAll('input[type=checkbox]')[1]!.setValue(true)
  await wrapper.findAll('button').find(button=>button.text()==='批量通过')!.trigger('click');await flushPromises()
  const confirmButton=Array.from(document.body.querySelectorAll('button')).find(button=>button.textContent?.trim()==='确认')!
  confirmButton.click();await flushPromises()
  expect(request).toHaveBeenCalledWith('/admin/review-queue/batch','POST',{items:[{kind:'profile',id:'a'},{kind:'profile',id:'b'}],approve:true,reason:''})
  wrapper.unmount()
})

it('shows soul review evidence and an authorized source preview entry',async()=>{
  const {default:ReviewsPanel}=await import('../src/components/admin/ReviewsPanel.vue')
  request.mockImplementation(async(path:string)=>path?.includes('?')?{items:[{id:'b',kind:'soul',name:'Alice',username:'alice',avatar_name:'A',category:'soul',status:'pending',created_at:''}],total:1,offset:0}:path==='/admin/review-queue/soul/b'?{kind:'soul',prompt:'素材提示',document:{voice:'温和'},snapshot:{name:'Alice'},notes:[{coverage:'文本'}],interview:[{question:'问',answer:'答'}],memory_tree:{edits:[]},assets:[{id:'f',filename:'source.txt'}]}:{})
  const wrapper=mount(ReviewsPanel,{attachTo:document.body});await flushPromises()
  await wrapper.findAll('button').find(button=>button.text()==='查看 / 审核')!.trigger('click');await flushPromises()
  expect(document.body.textContent).toContain('素材提示');expect(document.body.textContent).toContain('风格问答记录');expect(document.body.textContent).toContain('查看原料 · source.txt')
  expect(document.body.textContent).toContain('温和');wrapper.unmount()
})

it('keeps the memory tree and saved revision scoped to the open build', async () => {
  const node = (title:string) => ({revision:title, nodes:[{id:'node',group:'day',title,summary:'',content:''}]})
  let finishOld!: (value:ReturnType<typeof node>) => void
  request.mockImplementation(async (path:string) => {
    if (path.includes('/b1/')) return new Promise(resolve => { finishOld = resolve })
    return node('新构建')
  })
  const wrapper = mount(MemoryEditor, {props:{build}})
  try {
    const details = wrapper.get<HTMLDetailsElement>('details')
    details.element.open = true
    await details.trigger('toggle')
    await wrapper.setProps({build:{...build,id:'b2'}})
    await flushPromises()
    finishOld(node('旧构建'))
    await flushPromises()
    expect(wrapper.get('.soul-memory-node summary').text()).toContain('新构建')
    const title = wrapper.get('.soul-memory-node input')
    await title.setValue('我的草稿')
    await wrapper.setProps({build:{...build,id:'b2',updated_at:'build-v2'}})
    expect((title.element as HTMLInputElement).value).toBe('我的草稿')
    const save = wrapper.findAll('button').find(button => button.text() === '保存记忆修订')!
    expect(save.attributes('disabled')).toBeDefined()
    expect(request.mock.calls.some(([path]) => path.endsWith('/refine'))).toBe(false)
    await wrapper.findAll('button').find(button => button.text() === '重新载入记忆树')!.trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLInputElement>('.soul-memory-node input').element.value).toBe('新构建')
  } finally { wrapper.unmount() }
})

it('polls a settled Soul every 30s and pending learning or folder work every 3s', async () => {
  const {default:KnowledgeView} = await import('../src/views/KnowledgeView.vue')
  const empty = {assets:[], builds:[], active_build_id:null, model:{model:'test'}}
  let next = {...empty,learning:{status:'approved'},folders:{pending:false}}
  request.mockImplementation(async (path:string) => path === '/me/knowledge' ? next : {items:[]})
  const reads = () => request.mock.calls.filter(([path]) => path === '/me/knowledge').length
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval']})
  const wrapper = mount(KnowledgeView,{global:{stubs:{AssetLibrary:true,QuestionnaireDialog:true,AppDialog:true}}})
  try {
    await flushPromises()
    await vi.advanceTimersByTimeAsync(29_999)
    expect(reads()).toBe(1)
    next = {...next,learning:{status:'running'}}
    await vi.advanceTimersByTimeAsync(1)
    expect(reads()).toBe(2)
    next = {...next,learning:{status:'approved'},folders:{pending:true}}
    await vi.advanceTimersByTimeAsync(3000)
    next = {...next,folders:{pending:false}}
    await vi.advanceTimersByTimeAsync(3000)
    expect(reads()).toBe(4)
    await vi.advanceTimersByTimeAsync(29999)
    expect(reads()).toBe(4)
  } finally { wrapper.unmount(); vi.useRealTimers() }
})

it('refreshes Soul after a write even when an older read finishes last', async () => {
  const {default:KnowledgeView} = await import('../src/views/KnowledgeView.vue')
  const result = {assets:[],builds:[build],active_build_id:null,model:{model:'test'}}
  let reads = 0, finishOld!: (data:typeof result) => void
  request.mockImplementation(async (path:string) => {
    if (path !== '/me/knowledge') return {items:[]}
    if (++reads === 2) return new Promise(resolve => { finishOld = resolve })
    return reads === 1 ? result : {...result,builds:[{...build,status:'canceled'}]}
  })
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval']})
  const wrapper = mount(KnowledgeView,{global:{stubs:{AssetLibrary:true,QuestionnaireDialog:true,AppDialog:true}}})
  try {
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30000)
    await wrapper.findAll('button').find(button => button.text() === '取消构建')!.trigger('click')
    await flushPromises()
    finishOld(result)
    await flushPromises()
    expect(reads).toBe(3)
    expect(wrapper.get('.soul-build-status').text()).toContain('已取消')
  } finally { wrapper.unmount(); vi.useRealTimers() }
})

it('keeps a follow-up answer attached to the question revision where it was drafted', async () => {
  const {default:KnowledgeView} = await import('../src/views/KnowledgeView.vue')
  let revision = 'first'
  request.mockImplementation(async (path:string) => path === '/me/knowledge'
    ? {assets:[],builds:[{...build,status:'waiting_user',phase:'followup',updated_at:revision}],model:{model:'test'}}
    : {items:[]})
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval']})
  const wrapper = mount(KnowledgeView,{global:{stubs:{AssetLibrary:true,QuestionnaireDialog:true,AppDialog:true}}})
  try {
    await flushPromises()
    const answer = wrapper.get<HTMLTextAreaElement>('.soul-build textarea')
    await answer.setValue('第一个问题的回答')
    revision = 'second'
    await vi.advanceTimersByTimeAsync(30000)
    expect(answer.element.value).toBe('第一个问题的回答')
    const send = wrapper.findAll('.soul-build button').find(button => button.text() === '回答并继续')!
    expect(send.attributes('disabled')).toBeDefined()
    await wrapper.findAll('.soul-build button').find(button => button.text() === '重新作答')!.trigger('click')
    await answer.setValue('第二个问题的回答')
    await send.trigger('click'); await flushPromises()
    expect(request).toHaveBeenCalledWith('/me/knowledge/builds/b1/answer','POST',{answer:'第二个问题的回答',revision:'second'})
    expect(answer.element.value).toBe('')
  } finally { wrapper.unmount(); vi.useRealTimers() }
})
