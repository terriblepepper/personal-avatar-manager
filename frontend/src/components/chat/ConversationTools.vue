<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { request, notifyError } from '../../api/client'

interface Tool { id: string; name: string; description?: string; enabled: boolean; locked?: boolean }
const props = defineProps<{ conversationId: string; busy: boolean }>()
const emit = defineEmits<{saving:[pending:boolean]}>()
const tools = ref<Tool[]>([]), saving = ref(false), loading = ref(false), failure = ref('')
const enabled = computed(() => tools.value.filter(tool => tool.enabled).length)
const endpoint = computed(() => '/conversations/' + encodeURIComponent(props.conversationId) + '/tools')
let controller = new AbortController()

async function load() {
  controller.abort()
  controller = new AbortController()
  const signal = controller.signal
  tools.value = []
  failure.value = ''
  saving.value = false
  loading.value = !!props.conversationId
  if (!props.conversationId) return
  try {
    const result = await request<{items:Tool[]}>(endpoint.value,'GET',undefined,signal)
    if (!signal.aborted) tools.value = result.items
  }
  catch (error) { if (!signal.aborted) failure.value = error instanceof Error ? error.message : '读取会话工具失败' }
  finally { if (!signal.aborted) loading.value = false }
}

async function change(tool: Tool, event: Event) {
  const input = event.target
  if (!(input instanceof HTMLInputElement)) return
  const checked = input.checked
  if (props.busy || saving.value || tool.locked) { input.checked = tool.enabled; return }
  const signal = controller.signal, previous = tools.value
  saving.value = true
  const enableSandbox = tool.id === 'animation' && checked
  tools.value = previous.map(item => item.id === tool.id || (enableSandbox && item.id === 'python')
    ? { ...item, enabled: checked } : item)
  try {
    const result = await request<{items:Tool[]}>(endpoint.value,'PATCH',{tools:{[tool.id]:checked}},signal)
    if (!signal.aborted) tools.value = result.items
  } catch (error) { if (!signal.aborted) { tools.value=previous; notifyError(error) } }
  finally { if (!signal.aborted) saving.value = false }
}
watch(saving, pending => emit('saving', pending), {flush:'sync'})
watch(() => props.conversationId, load, { immediate: true })
onUnmounted(() => { controller.abort(); emit('saving', false) })
</script>

<template>
  <details class="composer-menu composer-tool">
    <summary title="管理本会话工具"><AppIcon name="settings" :size="14"/><span>工具</span><small>{{ enabled }}</small></summary>
    <div class="composer-popover">
      <h3>会话工具</h3>
      <p v-if="saving" class="sub" role="status">正在保存工具设置…</p>
      <p v-if="failure" class="error-text" role="alert">{{ failure }} <button type="button" class="button small" @click="load">重试</button></p>
      <p v-else-if="loading" class="sub" role="status">正在读取…</p>
      <p v-else-if="!tools.length" class="sub">暂无可用工具。</p>
      <label v-for="tool in tools" :key="tool.id" class="conversation-tool">
        <input type="checkbox" :checked="tool.enabled" :disabled="busy || saving || tool.locked" @change="change(tool,$event)">
        <span><strong>{{ tool.name }}</strong><small v-if="tool.locked">始终开启</small><small v-if="tool.description">{{ tool.description }}</small></span>
      </label>
    </div>
  </details>
</template>
