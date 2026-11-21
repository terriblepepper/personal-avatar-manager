<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

withDefaults(defineProps<{ label?: string }>(), { label: '聊天工具栏' })
const bar = ref<HTMLElement>()
const menus = 'details.composer-tool, details.chat-menu-section'
function openMenus() {
  return Array.from(bar.value?.querySelectorAll<HTMLDetailsElement>(menus) || []).filter(menu => menu.open)
}
function close(menu: HTMLDetailsElement, restore = false) {
  for (const child of menu.querySelectorAll<HTMLDetailsElement>('details.chat-menu-section[open]')) child.open = false
  menu.open = false
  if (restore) menu.querySelector('summary')?.focus({ preventScroll: true })
}
function toggle(event: Event) {
  const menu = event.target
  if (!(menu instanceof HTMLDetailsElement) || !menu.matches(menus)) return
  if (!menu.open) { close(menu); return }
  for (const other of openMenus()) if (!menu.contains(other) && !other.contains(menu)) close(other)
}
function escape(event: KeyboardEvent) {
  const menu = event.target instanceof Element && event.target.closest<HTMLDetailsElement>('details.chat-menu-section[open], details.composer-tool[open]')
  if (!menu) return
  event.stopPropagation()
  event.preventDefault()
  close(menu, true)
}
function navigate(event: KeyboardEvent) {
  const target = event.target
  if (!(target instanceof HTMLElement) || !target.matches('summary, button.chat-menu-item')) return
  const menu = target.closest<HTMLDetailsElement>('details.composer-tool')
  const panel = menu?.querySelector(':scope > .chat-command-panel')
  if (!menu || !panel || !['ArrowDown', 'ArrowUp'].includes(event.key)) return
  event.preventDefault()
  menu.open = true
  const items = Array.from(panel.querySelectorAll<HTMLElement>('.chat-menu-item:not(:disabled)')).filter(item => {
    const closed = item.closest('details:not([open])')
    return !closed || item === closed.querySelector('summary')
  })
  const index = items.indexOf(target)
  const next = event.key === 'ArrowDown' ? index + 1 : index <= 0 ? items.length - 1 : index - 1
  items[next % items.length]?.focus({ preventScroll: true })
}
function activate(event: MouseEvent) {
  const action = event.target instanceof Element && event.target.closest('button.chat-menu-item')
  const menu = action && action.closest<HTMLDetailsElement>('details.composer-tool')
  if (menu) close(menu, true)
}
function outside(event: Event) {
  if (!(event.target instanceof Node)) return
  for (const menu of openMenus()) if (!menu.contains(event.target)) close(menu)
}
onMounted(() => {
  document.addEventListener('pointerdown', outside, true)
  document.addEventListener('focusin', outside, true)
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', outside, true)
  document.removeEventListener('focusin', outside, true)
})
</script>

<template>
  <div ref="bar" class="composer-toolbar" role="group" :aria-label="label" @toggle.capture="toggle" @keydown.esc="escape" @keydown="navigate" @click.capture="activate">
    <slot/>
  </div>
</template>
