import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import MarkdownContent from '../src/components/MarkdownContent.vue'

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
