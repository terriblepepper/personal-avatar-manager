import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AccountsPanel from '../src/components/admin/AccountsPanel.vue'
import QuestionBank from '../src/components/admin/QuestionBank.vue'
import AdminView from '../src/views/AdminView.vue'
import ConsoleLayout from '../src/layouts/ConsoleLayout.vue'
import { useSessionStore } from '../src/stores/session'
import type { User } from '../src/types'

const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', async original => ({
  ...await original<typeof import('../src/api/client')>(), request, notifyError: vi.fn(),
}))

beforeEach(() => { request.mockReset() })

it('renders the overview when the optional admin page parameter is empty', async () => {
  request.mockResolvedValue({counts:{}, items:[], total:0})
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/admin/:page?', component: AdminView }] })
  await router.push('/admin'); await router.isReady()
  const view = mount(AdminView, { global: { plugins: [router] } })
  expect(view.find('h1').text()).toBe('分身管理概览')
  view.unmount()
})

it('refreshes the visible admin sidebar and drops its late badge after an account switch', async () => {
  const pinia = createPinia(), session = useSessionStore(pinia)
  const admin: User = {id:'a',username:'admin',profile_name:'管理员甲',role:'admin',person_type:'teacher',account_status:'active'}
  const account: User = {...admin,id:'b',username:'local-b',role:'user',profile_name:'',identity_username:'平台账户乙'}
  let active = admin, count = Promise.resolve({pending:4})
  request.mockReset().mockImplementation(async (path:string) => {
    if (path === '/auth/me') return {user:active,csrf:active.id}
    if (path === '/admin/pending-count') return count
    throw new Error('Unexpected sidebar request ' + path)
  })
  session.accept({user:active,csrf:active.id})
  const router = createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:ConsoleLayout}]})
  await router.push('/')
  vi.useFakeTimers({toFake:['setInterval','clearInterval']})
  const hidden = vi.spyOn(document,'hidden','get').mockReturnValue(false)
  const view = mount(ConsoleLayout, {global:{plugins:[pinia,router],stubs:{NotificationBell:true,ThemePicker:true,FilePreview:true}}})
  try {
    await flushPromises()
    expect(view.get('.sidebar-account strong').text()).toBe('管理员甲')
    await view.get('.sidebar-more').trigger('click')
    await view.findAll('[role=menuitem]').find(button => button.text() === '管理控制台')!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/admin')
    expect(view.get('[data-nav="history"] .badgecount').text()).toBe('4')

    request.mockClear()
    hidden.mockReturnValue(true)
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(24000)
    expect(request).not.toHaveBeenCalled()
    let finish!: (value:{pending:number}) => void
    count = new Promise(resolve => { finish = resolve })
    hidden.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(request.mock.calls.map(([path]) => path)).toEqual(['/auth/me','/admin/pending-count'])
    active = account
    session.accept({user:active,csrf:active.id})
    finish({pending:11})
    await flushPromises()
    expect(view.get('.sidebar-account strong').text()).toBe('平台账户乙')
    expect(view.find('.badgecount').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(8000)
    expect(request.mock.calls.filter(([path]) => path === '/admin/pending-count')).toHaveLength(1)
  } finally { view.unmount(); hidden.mockRestore(); vi.useRealTimers() }
})

describe('accounts and question bank', () => {
  const global = { stubs: { ElDialog: { props: ['modelValue', 'title'], template: '<section v-if="modelValue" role="dialog" :aria-label="title"><slot/></section>' } } }
  beforeEach(() => { setActivePinia(createPinia()) })

  it('uses registered account fields and the approved snapshot with field-specific privacy', async () => {
    const account = { id: 'teacher', username: 'local', identity_username: 'teacher@example.test', person_type: 'teacher', role: 'user', account_status: 'active' }
    const profile = { name: '审核示例', person_type: 'teacher', employee_id: 'T-12', specialty: '示例方向', visibility: { specialty: 'private' } }
    const fields = [
      { key: 'name', label: '姓名', audience: 'both', visibility: 'public', fixed_public: true },
      { key: 'specialty', label: '专业 / 研究方向', audience: 'both', visibility: 'public' },
      { key: 'location', label: '所在地', audience: 'both', visibility: 'private' },
      { key: 'student_id', label: '学号（选填）', audience: 'student', visibility: 'private' },
    ]
    let approved = true
    request.mockImplementation(async (path: string) => path === '/profile-fields' ? fields :
      path.endsWith('/profile') ? { account, profile: approved ? profile : {}, approved_at: approved ? '2026-11-18' : null } :
        { items: [account], total: 1, offset: 0 })
    let wrapper = mount(AccountsPanel, { global })
    async function open() {
      await flushPromises()
      await wrapper.findAll('button').find(button => button.text() === '查看资料')!.trigger('click')
      await flushPromises()
      return wrapper.get('[role="dialog"]')
    }
    const dialog = await open()
    expect(dialog.attributes('aria-label')).toBe('用户资料 · 审核示例')
    expect(dialog.text()).toContain('teacher@example.test')
    expect(dialog.text()).toContain('T-12')
    expect(dialog.text()).toContain('审核时间')
    expect(dialog.findAll('dt').map(dt => dt.text())).toEqual(expect.arrayContaining(['姓名 公开（固定）', '专业 / 研究方向 私密', '所在地 私密']))
    expect(dialog.text()).not.toContain('学号（选填）')
    expect(dialog.text()).toContain('示例方向')
    expect(dialog.text()).toContain('未填写')
    wrapper.unmount()
    approved = false
    wrapper = mount(AccountsPanel, { global })
    const empty = await open()
    expect(empty.text()).toContain('注册已通过，暂无已审核通过的个人资料。')
    expect(empty.text()).not.toContain('审核时间')
    wrapper.unmount()
  })

  it('shows category totals, an empty filtered page and import errors inside the dialog', async () => {
    const category = { id: 'c', name: '表达', total: 9, available: 6, active: true }
    request.mockResolvedValue({ categories: [category], items: [], total: 0, offset: 0 })
    const wrapper = mount(QuestionBank, { global })
    await flushPromises()
    expect(wrapper.text()).toContain('1 个类别 · 9 道题')
    expect(wrapper.text()).toContain('暂无匹配题目。')
    const [categories, status] = wrapper.findAll('select')
    expect(categories!.element.value).toBe('')
    expect(categories!.text()).toContain('表达 · 6 道')
    await status!.setValue('inactive')
    expect(request.mock.calls.at(-1)?.[0]).toContain('status=inactive')
    await wrapper.findAll('button').find(button => button.text() === '导入题库')!.trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    const input = dialog.get('input[type=file]')
    Object.defineProperty(input.element, 'files', { value: [{ size: 8 * 1024 * 1024 + 1 }] })
    await input.trigger('change')
    await dialog.get('form').trigger('submit')
    await flushPromises()
    expect(dialog.get('[role="alert"]').text()).toContain('题库文件最大 8 MiB')
    expect(request.mock.calls.some(call => call[1] === 'POST')).toBe(false)
    wrapper.unmount()
  })
})
