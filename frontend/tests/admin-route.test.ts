import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia } from 'pinia'
import { expect, it, vi } from 'vitest'
import AdminView from '../src/views/AdminView.vue'
import ConsoleLayout from '../src/layouts/ConsoleLayout.vue'
import { useSessionStore } from '../src/stores/session'
import type { User } from '../src/types'

const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', async original => ({
  ...await original<typeof import('../src/api/client')>(), request, notifyError: vi.fn(),
}))

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
