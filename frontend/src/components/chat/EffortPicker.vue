<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request, notifyError } from '../../api/client'
import { effortLabels, type Effort } from '../../utils/reasoningEffort'

interface Settings { selected: Effort | null; default: Effort | null; levels: Effort[] }
const props = defineProps<{ conversationId: string; busy: boolean }>()
const emit = defineEmits<{ saving: [pending: boolean] }>()
const settings = ref<Settings>(), saving = ref(false), failure = ref(false)
const url = computed(() => '/conversations/' + encodeURIComponent(props.conversationId) + '/reasoning-effort')
const defaultLabel = computed(() => settings.value?.default ? '默认（' + effortLabels[settings.value.default] + '）' : '默认')
let controller: AbortController

async function load(signal: AbortSignal) {
  failure.value = false
  try {
    const value = await request<Settings>(url.value, 'GET', undefined, signal)
    if (!Array.isArray(value?.levels)) throw new Error('推理强度设置响应无效')
    if (!signal.aborted) settings.value = value
  } catch { if (!signal.aborted) failure.value = true }
}
watch(() => props.conversationId, (id, _, cleanup) => {
  controller = new AbortController()
  const active = controller
  cleanup(() => { active.abort(); emit('saving', false) })
  settings.value = undefined
  saving.value = failure.value = false
  if (id) void load(active.signal)
}, { immediate: true })

async function select(level: Effort | null) {
  if (props.busy || saving.value || !settings.value || level === settings.value.selected) return
  const signal = controller.signal
  saving.value = true
  emit('saving', true)
  try {
    const value = await request<Settings>(url.value, 'PATCH', { reasoning_effort: level }, signal)
    if (!signal.aborted) settings.value = value
  } catch (error) { if (!signal.aborted) notifyError(error) }
  finally { if (!signal.aborted) { saving.value = false; emit('saving', false) } }
}
</script>

<template>
  <details v-if="conversationId" class="composer-menu composer-tool">
    <summary title="调整本会话的推理强度">推理强度 <small>{{ settings?.selected ? effortLabels[settings.selected] : defaultLabel }}</small></summary>
    <div class="composer-popover chat-command-panel" aria-label="推理强度">
      <button v-if="failure" type="button" class="chat-menu-item" @click="load(controller.signal)">读取失败，重试</button>
      <span v-else-if="!settings" class="sub" role="status">正在读取…</span>
      <template v-else>
        <button type="button" class="chat-menu-item" :aria-pressed="settings.selected === null" :disabled="busy || saving" @click="select(null)">{{ defaultLabel }} {{ settings.selected === null ? '✓' : '' }}</button>
        <button v-for="level in settings.levels" :key="level" type="button" class="chat-menu-item" :aria-pressed="settings.selected === level" :disabled="busy || saving" @click="select(level)">{{ effortLabels[level] }} {{ settings.selected === level ? '✓' : '' }}</button>
        <small v-if="!settings.levels.length" class="sub">当前模型仅提供默认强度</small>
      </template>
    </div>
  </details>
</template>
