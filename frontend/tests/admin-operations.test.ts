import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { routerKey } from 'vue-router'
import ProfileDialog from '../src/components/admin/ProfileDialog.vue'
import AdminOverview from '../src/views/AdminOverview.vue'
import RecordPagination from '../src/components/RecordPagination.vue'
import OperationsPanel from '../src/components/admin/OperationsPanel.vue'
import ReviewsPanel from '../src/components/admin/ReviewsPanel.vue'
import SettingsPanel from '../src/components/admin/SettingsPanel.vue'
import MigrationPanel from '../src/components/admin/MigrationPanel.vue'

const {request,notifyError}=vi.hoisted(()=>({request:vi.fn(),notifyError:vi.fn()}))
vi.mock('../src/api/client',()=>({request,notifyError}))
vi.mock('../src/stores/session',()=>({useSessionStore:()=>({user:{is_root:true}})}))
afterEach(()=>{vi.restoreAllMocks();request.mockReset();notifyError.mockReset()})

it('reports each health result once, including a stale list and an unsuccessful probe',async()=>{
  const agent={user_id:'u',username:'alice',aic:'real',identity_status:'active',certificate:{id:'cert'},profile:{}}
  const results=[{message:'探测成功'},{ok:false,message:'探测未通过'},new Error('无法连接')]
  request.mockImplementation(async(path:string)=>{
    if(path.endsWith('/health')){const next=results.shift();if(next instanceof Error)throw next;return next}
    return {entity_registration_enabled:true,items:[agent],total:1,offset:0}
  })
  const wrapper=mount(OperationsPanel,{props:{page:'agents'}});await flushPromises()
  const row=wrapper.get('tbody tr'), health=row.findAll('button').find(button=>button.text()==='Health 检查')!
  for(const message of ['探测成功','探测未通过']){
    await health.trigger('click');await flushPromises()
    expect(row.findAll('td')[3]!.text()).toContain(message)
    expect(row.findAll('td')[4]!.text()).not.toContain(message)
  }
  expect(row.find('[role=alert]').text()).toContain('探测未通过')
  await health.trigger('click');await flushPromises()
  expect(notifyError).toHaveBeenCalledWith(expect.objectContaining({message:'无法连接'}))
  wrapper.unmount()
})

it('confirms each task list selection and only offers eligible rows',async()=>{
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false)
  const rows=(statuses:string[])=>statuses.map(status=>({id:status,status,username:status,updated_at:'2026-11-18',action:'renew'}))
  request.mockImplementation(async(_path:string,method?:string)=>method==='POST'?{}:{
    items:rows(['done','failed','waiting']),offset:0,total:3})
  const wrapper=mount(OperationsPanel,{props:{page:'jobs'}});await flushPromises()
  for(const [index,collection,ids] of [[0,'identity_jobs',['done','failed']],[1,'jobs',['done']]] as const){
    const card=wrapper.findAll('section.card')[index]!
    expect(card.findAll('input[type=checkbox]')).toHaveLength(ids.length+1)
    await card.get('input[type=checkbox]').setValue(true)
    const remove=card.findAll('button').find(button=>button.text()==='删除所选')!
    await remove.trigger('click');await flushPromises()
    expect(request.mock.calls.some(([p])=>p==='/records/'+collection+'/delete')).toBe(false)
    confirm.mockReturnValueOnce(true)
    await remove.trigger('click');await flushPromises()
    expect(request).toHaveBeenCalledWith('/records/'+collection+'/delete','POST',{ids:[...ids]})
  }
  wrapper.unmount()
})

it('shows runtime communication and refreshes resource usage without overwriting the edited policy',async()=>{
  const state={policy:{max_jobs:20},enforcement:{mode:'soft'},effective_cpu_cores:2,effective_memory_bytes:1073741824,active_jobs:1,waiting:0}
  request.mockImplementation(async(path:string)=>path==='/admin/settings'?{settings:{},environment:{registration_adapter:'DemoAdapter',registration_mode:'demo',communication:[{name:'系统 mTLS AIP',value:null}],framework_versions:{'acps-sdk':'2.2'},manual_path:'manual.md'}}:path.includes('system/registration')?{enabled:false,phase:'active',aic:'AIC-1'}:state)
  const wrapper=mount(SettingsPanel);await flushPromises()
  expect(wrapper.text()).toContain('系统 mTLS AIP：未启用')
  expect(wrapper.text()).toContain('身份与证书就绪')
  const jobs=wrapper.findAll('label').find(label=>label.text().includes('全局活跃工作上限'))!.get('input')
  await jobs.setValue('12');await jobs.trigger('change')
  await wrapper.findAll('button').find(button=>button.text()==='刷新状态')!.trigger('click');await flushPromises()
  expect(jobs.element.value).toBe('12')
  wrapper.unmount()
})

it('shows migrated subjects and names the account kept or removed in a preview',async()=>{
  request.mockImplementation(async(path:string)=>path.endsWith('/preview')?{preview_token:'token',issuer:'issuer',subject:'new-id',target:{account:{username:'retained'},files:9},empty_account:{account:{username:'placeholder'}},oidc_enabled:true}:{items:[{id:'u',username:'retained',migration_status:'unlinked',previous_identity:{subject:'former-id'}}],total:1,offset:0,issuer:'issuer'})
  const wrapper=mount(MigrationPanel);await flushPromises()
  expect(wrapper.text()).toContain('former-id')
  await wrapper.findAll('button').find(button=>button.text()==='绑定小镇 ID')!.trigger('click')
  await wrapper.findAll('input').find(input=>input.attributes('maxlength')==='255')!.setValue('new-id')
  await wrapper.findAll('button').find(button=>button.text()==='预览迁移')!.trigger('click');await flushPromises()
  expect(wrapper.text()).toContain('9 个文件')
  expect(wrapper.text()).toContain('placeholder')
  expect(wrapper.find('pre').exists()).toBe(false)
  wrapper.unmount()
})

it('deletes one processed review independently of the current selection',async()=>{
  vi.spyOn(window,'confirm').mockReturnValue(true)
  request.mockImplementation(async(path:string)=>path.includes('?')?{items:['a','b'].map(id=>({id,kind:'profile',username:id,category:'update',status:'approved'})),total:2,offset:0}:{})
  const wrapper=mount(ReviewsPanel);await flushPromises()
  await wrapper.findAll('tbody tr')[0]!.get('input[type=checkbox]').setValue(true)
  const row=wrapper.findAll('tbody tr')[1]!
  expect(row.text()).toContain('资料更新')
  await row.findAll('button').find(button=>button.text()==='删除记录')!.trigger('click');await flushPromises()
  expect(request).toHaveBeenCalledWith('/admin/review-queue/delete','POST',{items:[{kind:'profile',id:'b'}]})
  wrapper.unmount()
})

it('shows audience-specific visibility and ACS capabilities while omitting certificate bodies',async()=>{
  const agent={user_id:'u',username:'alice',aic:'real',identity_status:'active',certificate:{id:'cert'},
    profile:{person_type:'teacher',employee_id:'T-42',visibility:{employee_id:'private'}},
    acs_document:{name:'示例能力',skills:[{name:'研究问答'}],certificate:{public_pem:'CERT_BODY_EXAMPLE'}}}
  let finish!: (value:unknown)=>void
  request.mockImplementation(async(path:string)=>path.endsWith('/detail')?new Promise(resolve=>{finish=resolve}):path==='/profile-fields'?
    [{key:'employee_id',label:'教师工号',audience:'teacher',visibility:'public'},{key:'student_id',label:'学生学号',audience:'student',visibility:'private'}]:
    {entity_registration_enabled:true,items:[{...agent,profile:{avatar_name:'摘要'},acs_document:{version:'1.0'}}],total:1,offset:0})
  const wrapper=mount(OperationsPanel,{props:{page:'agents'},attachTo:document.body});await flushPromises()
  await wrapper.findAll('button').find(button=>button.text()==='详情')!.trigger('click');await flushPromises()
  const dialog=document.body.querySelector('[role=dialog]')!
  expect(dialog.textContent).toContain('正在读取分身详情')
  expect(request).toHaveBeenCalledWith('/admin/agents/u/detail')
  const late=finish
  wrapper.getComponent(ProfileDialog).vm.$emit('close')
  await wrapper.findAll('button').find(button=>button.text()==='详情')!.trigger('click');await flushPromises()
  late(agent);await flushPromises()
  expect(dialog.textContent).toContain('正在读取分身详情')
  finish(agent);await flushPromises()
  expect(dialog.textContent).toContain('教师工号')
  expect(dialog.textContent).toContain('私密')
  expect(dialog.textContent).not.toContain('学生学号')
  expect(dialog.textContent).toContain('研究问答')
  expect(dialog.textContent).not.toContain('CERT_BODY_EXAMPLE')
  wrapper.unmount()
})

it('refreshes only the overview counts and ignores a superseded response',async()=>{
  let first!: (value:unknown)=>void
  request.mockImplementation((path:string)=>{
    if(path.startsWith('/records/submissions?'))return {items:[],offset:0,total:0}
    if(path==='/admin/overview'){
      if(!first)return new Promise(resolve=>{first=resolve})
      return {counts:{users:72}}
    }
    throw new Error(path)
  })
  const wrapper=mount(AdminOverview,{global:{provide:{[routerKey as symbol]:{push:vi.fn()}},stubs:{MessageRecent:true}}})
  try{
    await wrapper.get('button').trigger('click');await flushPromises()
    expect(wrapper.get('.stats').text()).toContain('72')
    first({counts:{users:34}});await flushPromises()
    expect(wrapper.get('.stats').text()).not.toContain('34')
    expect(notifyError).not.toHaveBeenCalled()
  }finally{wrapper.unmount()}
})

it('renders the current operations page and its row-specific waiting job when responses arrive out of order',async()=>{
  let first!: (value:unknown)=>void
  const agent={user_id:'u',username:'newer',profile:{},aic:'real',identity_status:'active',identity_job:{id:'job-u',action:'renew',status:'waiting'}}
  request.mockImplementation((path:string)=>{
    if(!path.startsWith('/admin/agents/operations?'))throw new Error(path)
    const offset=Number(new URLSearchParams(path.split('?')[1]).get('offset'))
    if(offset===0)return new Promise(resolve=>{first=resolve})
    return {entity_registration_enabled:true,items:[agent],offset,total:11}
  })
  const wrapper=mount(OperationsPanel,{props:{page:'agents'}})
  try {
    wrapper.getComponent(RecordPagination).vm.$emit('update:offset',10);await flushPromises()
    const row=wrapper.get('tbody tr')
    expect(row.text()).toContain('newer')
    expect(row.text()).toContain('等待恢复')
    expect(row.findAll('button').find(b=>b.text()==='立即检查')).toBeTruthy()
    first({entity_registration_enabled:false,items:[],offset:0,total:0});await flushPromises()
    expect(wrapper.get('tbody tr').text()).toContain('等待恢复')
    expect(notifyError).not.toHaveBeenCalled()
  }finally{wrapper.unmount()}
})
