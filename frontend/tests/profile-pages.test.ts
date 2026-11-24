import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import SkillsView from '../src/views/SkillsView.vue'
import ModelForm from '../src/components/models/ModelForm.vue'
const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', () => ({ request, notifyError: vi.fn(), setSessionExpiryHandler:vi.fn(), setCsrfToken:vi.fn() }))
vi.mock('element-plus', async importOriginal => {
  const actual = await importOriginal<typeof import('element-plus')>()
  return { ...actual, ElMessage: { success: vi.fn(), error: vi.fn() } }
})
beforeEach(() => { request.mockReset(); setActivePinia(createPinia()) })
async function mountFiles() {
  const { default: FilesView } = await import('../src/views/FilesView.vue')
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/files', component: FilesView }] })
  await router.push('/files')
  return mount(FilesView, { global: { plugins: [router] } })
}

describe('migrated settings contracts', () => {
  it('sends the saved model version and never echoes the stored key', async () => {
    request.mockImplementation(async (path: string, method?: string) => {
      if (path === '/model-providers') return [{ id:'custom',name:'Custom',base_url:'',api_format:'openai',models:[] }]
      if (path === '/me/model-settings' && !method) return { model_config:{provider:'custom',base_url:'https://example.com',model:'test',key_configured:true},updated_at:'version-1' }
      if (path === '/me/model-settings' && method === 'PUT') return {model_config:{key_configured:true},updated_at:'version-2'}
      return {models:[],notice:''}
    })
    const wrapper=mount(ModelForm); await flushPromises()
    expect(wrapper.get<HTMLInputElement>('input[type=password]').element.value).toBe('')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    const call=request.mock.calls.find(c=>c[0]==='/me/model-settings' && c[1]==='PUT')
    expect(call?.[2]).toMatchObject({updated_at:'version-1',model_config_input:{api_key:null}})
  })

  it('shows installed skills in read-only mode', async () => {
    request.mockResolvedValueOnce([{id:'id-1',name:'test',description:'desc',updated_at:'now',manifest:[]}])
    const wrapper=mount(SkillsView); await flushPromises()
    expect(wrapper.text()).toContain('暂停使用')
    expect(wrapper.get('.skill-item').text()).toContain('test')
    expect(wrapper.find('.skill-item button').exists()).toBe(false)
    expect(request).toHaveBeenCalledExactlyOnceWith('/me/skills')
  })
})

describe('profile and file versioning', () => {
  it('submits the original profile revision with an edited draft', async () => {
    const { default: ProfileView } = await import('../src/views/ProfileView.vue')
    const { useSessionStore } = await import('../src/stores/session')
    const session=useSessionStore();session.user={id:'u',username:'alice',role:'user',person_type:'student',account_status:'active'}
    const data={profile:{draft:{name:'Alice',person_type:'student',visibility:{name:'public'}},last_modified_at:'rev-1',status:'draft',acs_input:null,acs_document:{},acs_online:false,acs_direct:false,acs_editing:false}}
    request.mockImplementation(async (path:string, method?:string) => {
      if(path==='/profile-fields')return [{key:'name',label:'姓名',type:'text',audience:'both',visibility:'public',fixed_public:true}]
      if(path==='/me' || (path==='/me/profile' && method==='PUT'))return data
      if(path==='/auth/me')return {user:session.user,csrf:'csrf'}
      return {}
    })
    const wrapper=mount(ProfileView);await flushPromises()
    await wrapper.get('input').setValue('New Alice')
    const save=wrapper.findAll('button').find(button=>button.text()==='保存草稿');expect(save).toBeTruthy()
    await save!.trigger('click');await flushPromises()
    expect(request).toHaveBeenCalledWith('/me/profile','PUT',expect.objectContaining({last_modified_at:'rev-1',profile:expect.objectContaining({name:'New Alice'})}))
  })
})

it('uses the current collection version for folder creation', async () => {
  request.mockImplementation(async (path:string,method?:string) => {
    if(path==='/me/attachments')return []
    if(path==='/me/material-folders' && !method)return {items:[],captions:{},visibility:{},updated_at:'files-rev'}
    if(path==='/me')return {files_modified_at:'files-rev'}
    if(path==='/me/file-index')return {configured:true,items:[],summary:{profile:{total:0,completed:0,remaining:0}}}
    return {}
  })
  const wrapper=await mountFiles();await flushPromises()
  const button=wrapper.findAll('button').find(x=>x.text()==='新建文件夹');expect(button).toBeTruthy()
  await button!.trigger('click');await flushPromises()
  const input=document.querySelector<HTMLInputElement>('.el-message-box input');expect(input).toBeTruthy();input!.value='new';input!.dispatchEvent(new Event('input',{bubbles:true}))
  const confirm=[...document.querySelectorAll<HTMLButtonElement>('.el-message-box button')].find(x=>x.textContent?.includes('OK'));expect(confirm).toBeTruthy();confirm!.click();await flushPromises()
  expect(request).toHaveBeenCalledWith('/me/material-folders','POST',{path:'new',last_modified_at:'files-rev'})
  wrapper.unmount()
})

it('reports a cancelled upload and still completes its batch', async () => {
  const { useSessionStore } = await import('../src/stores/session')
  const session=useSessionStore();session.csrf='csrf-token'
  request.mockImplementation(async (path:string) => {
    if(path==='/me/attachments')return []
    if(path==='/me/material-folders')return {items:[],captions:{},visibility:{}}
    if(path==='/me')return {files_modified_at:''}
    if(path==='/me/file-index')return {configured:true,items:[],summary:{profile:{total:0,completed:0,remaining:0}}}
    return {}
  })
  const fetcher=vi.fn((_url:string, options:RequestInit)=>new Promise((_resolve,reject)=>options.signal?.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError')))))
  vi.stubGlobal('fetch',fetcher)
  const wrapper=await mountFiles();await flushPromises()
  const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['hello'],'a.txt')]})
  await input.trigger('change');await flushPromises()
  expect(fetcher).toHaveBeenCalledOnce();expect(new Headers(fetcher.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-token')
  const cancel=wrapper.findAll('button').find(x=>x.text()==='取消上传');expect(cancel).toBeTruthy();await cancel!.trigger('click');await flushPromises()
  expect(wrapper.text()).toContain('已上传 0 个文件')
  expect(request.mock.calls.some(c=>typeof c[0]==='string'&&c[0].startsWith('/me/upload-batches/')&&c[2]?.selected===1)).toBe(true)
  wrapper.unmount();vi.unstubAllGlobals()
})

it('clears the session after a successful password change', async () => {
  const { default: AccountView } = await import('../src/views/AccountView.vue')
  const { useSessionStore } = await import('../src/stores/session')
  const session=useSessionStore();session.user={id:'u',username:'alice',role:'user',person_type:'student',account_status:'active'}
  request.mockResolvedValue({message:'ok'})
  const wrapper=mount(AccountView);await flushPromises()
  const passwordSection = wrapper.findAll('section').find(section => section.element.querySelector('h2')?.textContent === '修改密码')
  const passwordForm = passwordSection!.get('form')
  const [current, updated, confirmation] = passwordForm.findAll('input[type=password]')
  await current!.setValue('oldpassword')
  await updated!.setValue('newpassword')
  await confirmation!.setValue('newpassword')
  await passwordForm.trigger('submit')
  await flushPromises()
  expect(request).toHaveBeenCalledWith('/auth/change-password','POST',{current_password:'oldpassword',new_password:'newpassword'})
  expect(session.user).toBeNull()
})

it('previews ACS against the current profile revision', async () => {
  const { default: ProfileView } = await import('../src/views/ProfileView.vue')
  const { useSessionStore } = await import('../src/stores/session')
  const session=useSessionStore();session.user={id:'u',username:'alice',role:'user',person_type:'student',account_status:'active'}
  vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:vi.fn().mockReturnValue('blob:test'),revokeObjectURL:vi.fn()}))
  const input={name:'Alice',description:'desc',version:'1.0.0',icon_url:'',documentation_url:'',location:'',default_input_modes:['text/plain'],default_output_modes:['text/plain'],skills:[],provider:{country_code:'CN',organization:'',department:'',name:'',url:'',license:'',email:'',domain_registrations:[]}}
  request.mockImplementation(async (path:string) => {
    if(path==='/profile-fields')return [{key:'name',label:'姓名',type:'text',audience:'both',visibility:'public',fixed_public:true}]
    if(path==='/me')return {profile:{draft:{name:'Alice',person_type:'student',visibility:{name:'public'}},last_modified_at:'rev-1',status:'draft',acs_input:input,acs_document:{},acs_online:false,acs_direct:true,acs_editing:false}}
    if(path==='/me/acs/preview')return {document:{name:'Alice',description:'desc'},validation:{valid:true,errors:[]}}
    return {}
  })
  const wrapper=mount(ProfileView);await flushPromises()
  const acs=wrapper.findAll('button').find(x=>x.text()==='ACS 配置');expect(acs).toBeTruthy();await acs!.trigger('click');await flushPromises()
  expect(request).toHaveBeenCalledWith('/me/acs/preview','POST',expect.objectContaining({last_modified_at:'rev-1',name_visibility:'public',acs_input:expect.objectContaining({name:'Alice'})}))
  expect(wrapper.text()).toContain('默认输入格式');expect(wrapper.find('a[download="avatar-acs.json"]').exists()).toBe(true)
  wrapper.unmount();vi.unstubAllGlobals()
})

it('does not replace a completed upload with an older inventory response', async () => {
  let finishOld:(files:unknown[])=>void=()=>{}, inventories=0
  const file={id:'new',filename:'new.txt',relative_path:'new.txt',content_type:'text/plain',size:1,caption:'',visibility:'private',updated_at:'v2'}
  request.mockImplementation(async (path:string) => {
    if(path==='/me/attachments'){inventories++;return inventories===1?new Promise(resolve=>{finishOld=resolve}):[file]}
    if(path==='/me/material-folders')return {items:[],captions:{},visibility:{}}
    if(path==='/me')return {files_modified_at:'v2'}
    if(path==='/me/file-index')return {configured:true,items:[],summary:{profile:{total:0,completed:0,remaining:0}}}
    return {}
  })
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('{}',{status:201})))
  const wrapper=await mountFiles();await flushPromises()
  const input=wrapper.get<HTMLInputElement>('input[type=file]');Object.defineProperty(input.element,'files',{configurable:true,value:[new File(['a'],'new.txt')]})
  await input.trigger('change');await flushPromises();finishOld([]);await flushPromises()
  expect(wrapper.text()).toContain('new.txt')
  wrapper.unmount();vi.unstubAllGlobals()
})

it('uploads every directory batch and plain files from a mixed browser drop', async () => {
  request.mockImplementation(async (path: string, method?: string, body?: { path: string }) => {
    if (path === '/me/attachments') return []
    if (path === '/me/material-folders') return { items: method ? [body?.path] : [], captions: {}, visibility: {}, last_modified_at: 'rev' }
    if (path === '/me') return { files_modified_at: 'rev' }
    if (path === '/me/file-index') return { configured: true, items: [], summary: {} }
    return {}
  })
  const fetcher = vi.fn(async (_path: string, _options: RequestInit) => new Response('{}'))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = await mountFiles()
  try {
    await flushPromises()
    const leaf = (name: string) => ({ isFile: true, name, file: (done: (file: File) => void) => done(new File(['text'], name)) })
    function folder(name: string, batches: unknown[][]) {
      return { isDirectory: true, name, createReader: () => ({ readEntries: (done: (entries: unknown[]) => void) => done(batches.shift() || []) }) }
    }
    const root = folder('root', [[leaf('first.md')], [folder('sub', [[leaf('second.pdf')]]), folder('empty', [])]])
    const plain = new File(['text'], 'top.txt')
    await wrapper.get('.material-workspace').trigger('drop', { dataTransfer: { items: [
      { kind: 'file', webkitGetAsEntry: () => root, getAsFile: () => null },
      { kind: 'file', getAsFile: () => plain },
    ], files: [plain] } })
    await flushPromises()
    expect(fetcher.mock.calls.map(([, options]) => decodeURIComponent(new Headers(options.headers).get('X-Attachment-Relative-Path')!)))
      .toEqual(['root/first.md', 'root/sub/second.pdf', 'top.txt'])
    expect(request).toHaveBeenCalledWith('/me/material-folders', 'POST', expect.objectContaining({ path: 'root/empty' }))
  } finally { wrapper.unmount(); vi.unstubAllGlobals() }
})

it('keeps a caption draft tied to its original revision when another editor changes visibility', async () => {
  const file = {id:'f',filename:'a.txt',relative_path:'a.txt',caption:'old',visibility:'public',updated_at:'v1'}
  let changed = false
  request.mockImplementation(async (path:string, method?:string) => {
    if (path === '/me/attachments') return [changed ? {...file, visibility:'private', updated_at:'v2'} : {...file}]
    if (path === '/me/material-folders') return {items:[], captions:{}, visibility:{}, updated_at:'collection'}
    if (path === '/me/file-index') return {configured:true,items:[],summary:{profile:{total:0,completed:0,remaining:0}}}
    if (method === 'PATCH') throw new Error('文件设置已变更，请刷新后再保存')
    return {}
  })
  const wrapper = await mountFiles()
  try {
    await flushPromises()
    await wrapper.get('.material-entry').trigger('click')
    await wrapper.get('.material-inspector textarea').setValue('edited')
    changed = true
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}')))
    const input = wrapper.get<HTMLInputElement>('input[type=file]')
    Object.defineProperty(input.element, 'files', {value:[new File(['x'], 'other.txt')]})
    await input.trigger('change'); await flushPromises()
    expect(wrapper.get<HTMLSelectElement>('.material-inspector select').element.value).toBe('private')
    await wrapper.get('.material-inspector button').trigger('click'); await flushPromises()
    expect(request).toHaveBeenCalledWith('/me/attachments/f','PATCH',{caption:'edited',updated_at:'v1'})
    expect(wrapper.get<HTMLTextAreaElement>('.material-inspector textarea').element.value).toBe('edited')
    await wrapper.findAll('.material-inspector button').find(button => button.text() === '放弃草稿并刷新')!.trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('.material-inspector textarea').element.value).toBe('old')
    expect(wrapper.get<HTMLSelectElement>('.material-inspector select').element.value).toBe('private')
  } finally { wrapper.unmount(); vi.unstubAllGlobals() }
})

it('polls only permitted profile indexes and keeps partial and blocked states idle', async () => {
  const { useSessionStore } = await import('../src/stores/session')
  const session = useSessionStore()
  session.user = {id:'u',username:'user',account_status:'active',person_type:'student',role:'user',access_scope:'profile'}
  request.mockImplementation(async (path:string) => {
    if (path.startsWith('/me/file-index')) return {configured:true,items:[],summary:{profile:{total:2,completed:0,remaining:2,partial:1,blocked:1,running:0,queued:0}}}
    return path === '/me/attachments' ? [] : {items:[],captions:{},visibility:{}}
  })
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout','setInterval','clearInterval']})
  const view = await mountFiles()
  const indexCalls = () => request.mock.calls.filter(([path]) => path.startsWith('/me/file-index'))
  try {
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30000)
    expect(indexCalls()).toHaveLength(0)
    expect(view.text()).not.toContain('同步索引')
    session.user = {...session.user!,access_scope:'full'}
    await flushPromises()
    expect(indexCalls()).toHaveLength(1)
    expect(view.get('[role=status]').text()).toMatch(/部分索引.*1/)
    expect(view.get('[role=status]').text()).toMatch(/阻塞.*1/)
    await vi.advanceTimersByTimeAsync(29999)
    expect(indexCalls()).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(indexCalls()).toHaveLength(2)
  } finally { view.unmount(); vi.useRealTimers() }
})
