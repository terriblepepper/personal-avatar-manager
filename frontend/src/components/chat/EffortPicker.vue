<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request, notifyError } from '../../api/client'
import AppIcon from '../AppIcon.vue'
import { effortLabels, type Effort } from '../../utils/reasoningEffort'

interface Settings { model: string; selected: Effort | null; default: Effort | null; levels: Effort[] }
const props = defineProps<{ conversationId: string; busy: boolean }>()
const emit = defineEmits<{ saving: [pending: boolean] }>()
const settings = ref<Settings>(), saving = ref(false), failure = ref(false)
const url = computed(() => '/conversations/' + encodeURIComponent(props.conversationId) + '/reasoning-effort')
const preview = ref<number>()
const requested = ref<Effort | null>()
const stops = computed<(Effort | null)[]>(() => {
  const value = settings.value
  if (!value?.levels.length) return []
  return value.default && value.levels.includes(value.default) && value.levels.length > 1
    ? value.levels : [null, ...value.levels]
})
const position = computed(() => {
  const selection = requested.value === undefined ? settings.value?.selected : requested.value
  return preview.value ?? Math.max(0, stops.value.indexOf(selection ?? settings.value?.default ?? null))
})
const shownLevel = computed(() => {
  const value = stops.value[position.value]
  return value ? effortLabels[value] : '默认'
})
const progress = computed(() => position.value / Math.max(1, stops.value.length - 1))
const modelLabel = computed(() => settings.value?.model || (failure.value ? '读取失败' : settings.value ? '未配置模型' : '读取中…'))
const levelLabel = computed(() => {
  const level = settings.value?.selected ?? settings.value?.default
  return level ? effortLabels[level] : '默认'
})
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
  preview.value = requested.value = undefined
  saving.value = failure.value = false
  if (id) void load(active.signal)
}, { immediate: true })

async function select(level: Effort | null) {
  preview.value = undefined
  if (props.busy || !settings.value) return
  requested.value = level
  if (saving.value) return
  const signal = controller.signal
  saving.value = true
  emit('saving', true)
  try {
    // Keep one write in flight while the user moves through several keyboard stops.
    while (requested.value !== undefined && requested.value !== settings.value?.selected) {
      const next: Effort | null = requested.value
      const value: Settings = await request<Settings>(url.value, 'PATCH', { reasoning_effort: next }, signal)
      if (signal.aborted) return
      settings.value = value
      if (next === requested.value) break
    }
  } catch (error) { if (!signal.aborted) notifyError(error) }
  finally {
    if (!signal.aborted) {
      requested.value = undefined
      saving.value = false
      emit('saving', false)
    }
  }
}
</script>

<template>
  <details v-if="conversationId" class="composer-menu composer-tool effort-picker">
    <summary :title="modelLabel + ' · 调整推理强度'" :aria-label="modelLabel + '，推理强度：' + levelLabel">
      <span class="composer-model-name">{{ modelLabel }}</span><span v-if="settings?.model" class="composer-effort-level">{{ levelLabel }}</span>
    </summary>
    <div class="composer-popover effort-card" aria-label="推理强度">
      <button v-if="failure" type="button" class="chat-menu-item" @click="load(controller.signal)">读取失败，重试</button>
      <span v-else-if="!settings" class="sub" role="status">正在读取…</span>
      <template v-else>
        <div class="effort-heading">
          <AppIcon class="effort-bolt" name="bolt" :size="16"/>
          <strong>{{ shownLevel }}</strong>
          <button v-if="settings.selected !== null || requested" type="button" class="effort-reset" aria-label="恢复模型默认" title="恢复模型默认" :disabled="busy" @click="select(null)"><AppIcon name="rebuild" :size="15"/></button>
          <RouterLink class="effort-model" to="/models" :title="'查看模型配置 · ' + modelLabel">
            <span>{{ modelLabel }}</span><AppIcon name="chevron" :size="12"/>
          </RouterLink>
        </div>
        <div v-if="stops.length" class="effort-slider" :style="{ '--progress': progress }">
          <div class="effort-track" aria-hidden="true">
            <div class="effort-ticks"><i v-for="(level, index) in stops" :key="level ?? 'default'" :class="{ filled: index <= position }"/></div>
          </div>
          <input type="range" aria-label="推理强度" :aria-valuetext="shownLevel" min="0" :max="stops.length - 1" step="1"
            :value="position" :disabled="busy" @input="preview = ($event.target as HTMLInputElement).valueAsNumber"
            @pointercancel="preview = undefined" @change="select(stops[($event.target as HTMLInputElement).valueAsNumber])">
        </div>
        <small v-else class="sub">当前模型仅提供默认强度</small>
      </template>
    </div>
  </details>
</template>

<style scoped>
.effort-heading { position: relative; display: grid; justify-items: center; gap: .15rem; padding-inline: 1.75rem; }
.effort-heading strong { color: var(--accent); font-size: 1rem; }
.effort-bolt, .effort-reset { position: absolute; top: 0; color: var(--text-secondary); }
.effort-bolt { inset-inline-start: 0; }
.effort-reset { inset-inline-end: -.25rem; padding: .2rem; background: transparent; border: 0; border-radius: 50%; cursor: pointer; }
.effort-reset:hover { background: var(--ui-hover); }
.effort-model { display: inline-flex; align-items: center; gap: .15rem; max-width: 100%; color: var(--text-secondary); text-decoration: none; }
.effort-model span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.effort-model:hover { color: var(--text); }
.effort-slider { --thumb: 2.125rem; position: relative; height: 2.75rem; margin-top: .55rem; }
.effort-track { position: absolute; inset: 50% 0 auto; height: 1.65rem; transform: translateY(-50%); border-radius: 999px; overflow: hidden; background: var(--surface-sunken); }
.effort-track::before { content: ''; position: absolute; inset: 0 auto 0 0; width: calc(var(--thumb) / 2 + (100% - var(--thumb)) * var(--progress)); background: color-mix(in srgb, var(--accent), #629bff 55%); }
.effort-ticks { position: absolute; inset: 0 calc(var(--thumb) / 2 - 2px); display: flex; align-items: center; justify-content: space-between; }
.effort-ticks i { width: 4px; height: 4px; border-radius: 50%; background: var(--text-secondary); opacity: .45; }
.effort-ticks i.filled { background: var(--on-accent); }
.effort-slider input { appearance: none; position: relative; display: block; width: 100%; height: 100%; margin: 0; padding: 0; border: 0; border-radius: 999px; background: transparent; cursor: pointer; }
.effort-slider input::-webkit-slider-thumb { appearance: none; box-sizing: border-box; width: var(--thumb); height: var(--thumb); border: 2px solid var(--accent); border-radius: 50%; background: var(--surface); }
.effort-slider input::-moz-range-thumb { box-sizing: border-box; width: var(--thumb); height: var(--thumb); border: 2px solid var(--accent); border-radius: 50%; background: var(--surface); }
.effort-slider input:focus-visible { outline: none; }
.effort-slider input:focus-visible::-webkit-slider-thumb { box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 30%, transparent); }
.effort-slider input:focus-visible::-moz-range-thumb { box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 30%, transparent); }
.effort-slider input:disabled { cursor: default; opacity: .65; }
@media (forced-colors: active) {
  .effort-slider input { appearance: auto; }
  .effort-slider input:focus-visible { outline: 2px solid Highlight; }
  .effort-slider input::-webkit-slider-thumb { appearance: auto; }
  .effort-track { display: none; }
}
</style>
