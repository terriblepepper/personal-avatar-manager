import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ComposerToolbar from '../src/components/chat/ComposerToolbar.vue'

it('dismisses one menu level with Escape and closes the flyout after an action or outside click', async () => {
  const view = mount(ComposerToolbar, {
    attachTo: document.body,
    props: { label: '会话操作' },
    slots: { default: '<details class="composer-tool chat-command-menu"><summary>更多</summary><div class="chat-command-panel"><details class="chat-menu-section"><summary class="chat-menu-item">表达风格学习</summary><button>立即学习</button></details><button class="chat-menu-item">重命名</button></div></details>' }
  })
  try {
    const [menu, section] = view.findAll<HTMLDetailsElement>('details')
    const [trigger, child] = view.findAll('summary')
    expect(view.attributes('aria-label')).toBe('会话操作')
    await trigger!.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(child!.element)
    await child!.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(view.get('button.chat-menu-item').element)
    section!.element.open = true
    await view.get('button').trigger('keydown', { key: 'Escape' })
    expect(section!.element.open).toBe(false)
    expect(menu!.element.open).toBe(true)
    expect(document.activeElement).toBe(child!.element)
    await child!.trigger('keydown', { key: 'Escape' })
    expect(menu!.element.open).toBe(false)
    expect(document.activeElement).toBe(trigger!.element)
    menu!.element.open = true
    await view.get('button.chat-menu-item').trigger('click')
    expect(menu!.element.open).toBe(false)
    menu!.element.open = section!.element.open = true
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    expect(menu!.element.open || section!.element.open).toBe(false)
    menu!.element.open = true
    document.body.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    expect(menu!.element.open).toBe(false)
  } finally { view.unmount() }
})
