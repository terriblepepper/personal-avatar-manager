import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import FilePreview from '../src/components/FilePreview.vue'
import MarkdownContent from '../src/components/MarkdownContent.vue'
import MermaidDiagram from '../src/components/MermaidDiagram.vue'
import ThemePicker from '../src/components/ThemePicker.vue'
import { fileSizeMB } from '../src/utils/format'

const pdfPage = vi.hoisted(() => vi.fn())
vi.mock('/static/vendor/pdfjs/legacy/build/pdf.mjs', () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({ promise: Promise.resolve({ numPages: 3, getPage: pdfPage, destroy: async () => {} }), destroy: async () => {} }),
}))
const global = { stubs: { ElDialog: { template: '<div><slot/></div>' }, ElInputNumber: true, MarkdownContent: { props: ['source'], template: '<article>{{ source }}</article>' } } }
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function preview(filename: string, url: string, content: string) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(content)))
  const wrapper = mount(FilePreview, { props: { file: { id: 'file', filename }, url }, global })
  await flushPromises()
  return wrapper
}

it('opens the authorized manual by chapter and navigates by accessible contents and pager', async () => {
  const markdown = '# 手册\n\n引言\n\n## 起步\n\n```\n## 示例代码\n```\n\n## 常见问题\n\n答案'
  const wrapper = await preview('用户操作手册.md', '/api/help/用户操作手册.md', markdown)
  const contents = wrapper.get('[aria-label="章节目录"]')
  const buttons = contents.findAll('button')
  expect(buttons.map(button => button.text())).toEqual(['起步', '常见问题'])
  expect(contents.get('details').element.open).toBe(true)
  expect(wrapper.get('article').text()).toContain('引言')
  await buttons[1]!.trigger('click')
  expect(buttons[1]!.attributes('aria-current')).toBe('page')
  expect(wrapper.get('article').text()).toContain('答案')
  expect(wrapper.get('article').text()).not.toContain('引言')
  const pager = wrapper.get('nav[aria-label="章节分页"]')
  expect(pager.get('[aria-label="跳转章节"]').element).toHaveProperty('value', '2')
  await pager.findAll('button').find(button => button.text() === '上一章')!.trigger('click')
  expect(buttons[0]!.attributes('aria-current')).toBe('page')
  expect(wrapper.get('a[download]').text()).toBe('下载手册')
  wrapper.unmount()
})

it.each(['txt', 'pdf'])('cycles matching %s pages forwards and backwards, including wraparound', async ext => {
  pdfPage.mockImplementation(async (number: number) => ({
    getViewport: () => ({ width: 100, height: 100 }),
    render: () => ({ promise: Promise.resolve(), cancel() {} }),
    getTextContent: async () => ({ items: number === 2 ? [{ str: 'other' }] : [{ str: 'a' }, { str: 'BC' }] }),
  }))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  const content = 'aBC'.padEnd(12000, '.') + 'other'.padEnd(12000, '.') + 'aBC'
  const wrapper = await preview('示例.' + ext, '/api/me/attachments/file', content)
  await vi.waitFor(() => expect(wrapper.get('input[aria-label="跳转页数"]').attributes('max')).toBe('3'))
  await wrapper.get('button[aria-label="搜索文档"]').trigger('click')
  const search = wrapper.get('input[type=search]')
  await search.setValue('ＡＢＣ')
  async function enter(shiftKey = false) {
    await search.trigger('keydown', { key: 'Enter', shiftKey })
    await flushPromises()
  }
  await enter()
  expect(wrapper.get('input[aria-label="跳转页数"]').element).toHaveProperty('value', '1')
  await enter(true)
  expect(wrapper.get('input[aria-label="跳转页数"]').element).toHaveProperty('value', '3')
  await enter()
  expect(wrapper.get('input[aria-label="跳转页数"]').element).toHaveProperty('value', '1')
  await search.setValue('missing')
  await enter()
  expect(wrapper.text()).toContain('未找到匹配内容')
  wrapper.unmount()
})

it('formats decimal megabytes and keeps nonempty small files distinct from empty ones', () => {
  expect([0, 1, 9999, 10000, 1250000].map(fileSizeMB)).toEqual(['0 MB', '<0.01 MB', '<0.01 MB', '0.01 MB', '1.25 MB'])
})

it('keeps code and tables while removing active HTML and external tracking images', () => {
  const view = mount(MarkdownContent, { props: { source: '# 消息\n\n```python\nprint(1)\n```\n\n<script>alert(1)</script><img src="https://tracker.invalid/x" onerror="alert(1)"><a href="javascript:alert(1)">危险链接</a>\n\n| A | B |\n| - | - |\n| 1 | 2 |' } })
  expect(view.find('h1').text()).toBe('消息')
  expect(view.find('pre code').text()).toBe('print(1)')
  expect(view.find('table').exists()).toBe(true)
  expect(view.find('script').exists()).toBe(false)
  expect(view.find('img').exists()).toBe(false)
  expect(view.find('[onerror]').exists()).toBe(false)
  expect(view.find('a[href^="javascript:"]').exists()).toBe(false)
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
