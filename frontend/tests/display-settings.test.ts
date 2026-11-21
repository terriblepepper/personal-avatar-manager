import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, expect, it, vi } from 'vitest'
import ThemePicker from '../src/components/ThemePicker.vue'
import SettingsPanel from '../src/components/admin/SettingsPanel.vue'
import MermaidDiagram from '../src/components/MermaidDiagram.vue'

const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', () => ({ request, setSessionExpiryHandler: vi.fn(), notifyError: vi.fn() }))
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('follows system and storage events and applies a choice when storage is unavailable', async () => {
  const changes = new EventTarget()
  const media = Object.assign(changes, { matches: true })
  vi.stubGlobal('matchMedia', () => media)
  vi.stubGlobal('localStorage', { getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } })
  const picker = mount(ThemePicker)
  try {
    expect(document.documentElement.dataset.theme).toBe('dark')
    media.matches = false; changes.dispatchEvent(new Event('change'))
    expect(document.documentElement.dataset.theme).toBe('light')
    await picker.findAll('[role=menuitemradio]')[2]!.trigger('click')
    expect(document.documentElement.dataset.themeMode).toBe('dark')
    window.dispatchEvent(new StorageEvent('storage', { key: 'avatar.theme', newValue: 'light' }))
    expect(document.documentElement.dataset.theme).toBe('light')
    vi.stubGlobal('localStorage', { getItem: () => 'dark', setItem: vi.fn() })
    const restored = mount(ThemePicker)
    expect(document.documentElement.dataset.theme).toBe('dark')
    restored.unmount()
  } finally { picker.unmount() }
})

it('reads runtime registration and resource status as escaped text', async () => {
  setActivePinia(createPinia())
  request.mockImplementation(async (path: string) => path === '/admin/settings' ? {
    settings: {}, environment: { registration_adapter: 'Actual-ACPS', manual_path: '/deployment/manual.md', communication: [{ name: 'Web', value: '<script>test</script>' }] },
  } : { policy: {}, effective_cpu_cores: 4, effective_memory_bytes: 8 * 1024 ** 3, active_jobs: 2, waiting: 3, pressure: true, enforcement: { mode: 'soft' } })
  const panel = mount(SettingsPanel)
  try {
    await flushPromises()
    for (const text of ['Actual-ACPS', '/deployment/manual.md', '<script>test</script>', '4 核', '8.0 GiB', '活跃 2', '等待 3', '资源压力', '软预算']) expect(panel.text()).toContain(text)
    expect(panel.find('script').exists()).toBe(false)
  } finally { panel.unmount() }
})

it('zooms the isolated diagram while preserving its layout height', async () => {
  const diagram = mount(MermaidDiagram, { props: { source: 'graph TD; A-->B' } })
  try {
    const input = diagram.get('input[aria-label="预览缩放比例"]')
    await input.setValue('200')
    await input.trigger('change')
    await flushPromises()
    const frame = diagram.get('iframe').element
    expect(frame.parentElement?.style.width).toBe('640px')
    expect(frame.parentElement?.style.height).toBe('360px')
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
  } finally { diagram.unmount() }
})
