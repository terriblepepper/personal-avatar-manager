<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { themeIconSvg } from '../utils/icons'
type Theme = 'system' | 'light' | 'dark'
const labels: Record<Theme, string> = { system: '跟随系统', light: '浅色', dark: '深色' }
function valid(value: unknown): value is Theme { return ['system', 'light', 'dark'].includes(String(value)) }
const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null
const mode = ref<Theme>('system')
try { const saved = localStorage.getItem('avatar.theme'); if (valid(saved)) mode.value = saved } catch { /* 设备存储可选 */ }
const open = ref(false)
const trigger = ref<HTMLButtonElement>(), choices = ref<HTMLElement>()
async function showChoices() {
  open.value = true
  await nextTick()
  choices.value?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus()
}
function closeChoices() {
  open.value = false
  trigger.value?.focus()
}
function navigate(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeChoices()
    return
  }
  const items = Array.from(choices.value?.querySelectorAll('button') ?? [])
  const index = items.indexOf(event.target as HTMLButtonElement)
  if (index < 0 || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  event.stopPropagation()
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[next]?.focus()
}
function apply() {
  const theme = mode.value === 'system' ? (media?.matches ? 'dark' : 'light') : mode.value
  document.documentElement.dataset.theme = theme
  document.documentElement.dataset.themeMode = mode.value
  document.documentElement.style.colorScheme = theme
  document.documentElement.classList.toggle('dark', theme === 'dark')
}
function choose(value: Theme) { mode.value = value; closeChoices(); try { localStorage.setItem('avatar.theme', value) } catch { /* 存储被拒绝 */ } apply() }
function dismiss(event: MouseEvent) { if (!(event.target as HTMLElement).closest('.theme-settings')) open.value = false }
function storage(event: StorageEvent) { if (event.key === 'avatar.theme') mode.value = valid(event.newValue) ? event.newValue : 'system'; apply() }
onMounted(() => { media?.addEventListener('change', apply); window.addEventListener('storage', storage); document.addEventListener('click', dismiss); apply() })
onUnmounted(() => { media?.removeEventListener('change', apply); window.removeEventListener('storage', storage); document.removeEventListener('click', dismiss) })
</script>
<template>
  <div class="theme-settings">
    <button ref="trigger" type="button" class="theme-trigger" aria-haspopup="menu" :aria-expanded="open" @click="open ? closeChoices() : showChoices()" @keydown.down.stop.prevent="showChoices"><span v-html="themeIconSvg(mode)"/><span>主题</span><small>{{ labels[mode] }}</small></button>
    <Transition name="menu-pop"><div ref="choices" v-show="open" class="theme-menu" @keydown="navigate" role="menu" aria-label="界面主题">
      <button v-for="value in (['system','light','dark'] as const)" :key="value" type="button" role="menuitemradio" :aria-checked="value === mode" @click="choose(value)"><span v-html="themeIconSvg(value)"/><span>{{ labels[value] }}</span><span class="theme-check" aria-hidden="true">✓</span></button>
    </div></Transition>
  </div>
</template>
