<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { notifyError, request } from '../api/client'
import { prepareAnimation, type AnimationScene } from '../utils/animation'
import { readAnimationPromotion, submitAnimationPromotion } from '../utils/animationPromotion'
import { requestId } from '../utils/requestId'
import type { FilePromotion } from '../types'
import AppIcon from './AppIcon.vue'
import RecordPagination from './RecordPagination.vue'
import DiagramMenu from './DiagramMenu.vue'
import MaterialFileDialog from './MaterialFileDialog.vue'

const FilePreview = defineAsyncComponent(() => import('./FilePreview.vue'))
const props = defineProps<{ source: string; filename?: string; conversationId?: string; previewMode?: boolean }>()
const card = ref<HTMLElement>(), frame = ref<HTMLIFrameElement>()
const scene = shallowRef<AnimationScene>(), code = ref(false), error = ref(''), frameError = ref('')
const channel = ref(requestId()), ready = ref(false), playing = ref(false), elapsed = ref(0), index = ref(-1), speed = ref(1)
const codePage = ref(0), pageLength = 12000
const pageCount = computed(() => Math.ceil(props.source.length / pageLength))
const shownCode = computed(() => {
  function boundary(page: number) {
    const offset = page * pageLength, unit = props.source.charCodeAt(offset)
    return unit >= 0xdc00 && unit <= 0xdfff && offset > 0 ? offset - 1 : offset
  }
  return props.source.slice(boundary(codePage.value), boundary(codePage.value + 1))
})
const current = computed(() => scene.value?.steps[index.value])
const visible = ref(false), shown = ref(!document.hidden), reduced = ref(false)
const preview = ref<'window' | 'full' | null>(null), form = ref(false), folders = ref<string[]>([])
const saved = ref(false), saving = ref(false), opening = ref(false), saveError = ref('')
const active = computed(() => !!scene.value && visible.value && shown.value && !code.value && !preview.value && !frameError.value)
const name = computed(() => {
  const base = (scene.value?.title || '动画').replace(/[\\/:*?"<>|]/g, '_').replace(/^[ .]+|[ .]+$/g, '') || '动画'
  return (/^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(base) ? '_' : '') + base + '.avanim'
})
let transferSource = ''
let controller = new AbortController(), lookupTicket = 0, observer: IntersectionObserver | undefined
let media: MediaQueryList | undefined, timeout: ReturnType<typeof setTimeout> | undefined
let objectURL = '', revokeTimer: ReturnType<typeof setTimeout> | undefined
function releaseURL() {
  clearTimeout(revokeTimer)
  if (objectURL) URL.revokeObjectURL(objectURL)
  objectURL = ''
}
function download() {
  if (!scene.value) return
  releaseURL()
  objectURL = URL.createObjectURL(new Blob([props.source], {type:'application/vnd.acps.animation+json'}))
  const link = document.createElement('a')
  link.href = objectURL
  link.download = name.value
  link.click()
  revokeTimer = setTimeout(releaseURL, 1000)
}
function send(type: string, value?: number) {
  frame.value?.contentWindow?.postMessage({avanim:true, channel:channel.value, type, value}, '*')
}
function message(event: MessageEvent) {
  const data = event.data
  if (!active.value || !scene.value || event.source !== frame.value?.contentWindow || event.origin !== 'null' ||
      !data || data.avanim !== true || data.channel !== channel.value) return
  if (data.type === 'ready') {
    frame.value?.contentWindow?.postMessage({avanim:true, channel:channel.value, type:'init', scene:scene.value, fitContent:props.previewMode === true,
      reducedMotion:reduced.value, elapsed:elapsed.value, speed:speed.value}, '*')
  } else if ((data.type === 'state' || data.type === 'complete') && Number.isFinite(data.elapsed) &&
      Number.isInteger(data.step) && data.step >= -1 && data.step < scene.value.steps.length) {
    clearTimeout(timeout)
    ready.value = true
    elapsed.value = Math.max(0, Math.min(scene.value.durationMs, data.elapsed))
    index.value = data.step
    playing.value = data.playing === true
  }
}
function resetFrame() {
  ready.value = playing.value = false
  channel.value = requestId()
}
function readSource() {
  controller.abort()
  controller = new AbortController()
  lookupTicket++
  transferSource = ''
  codePage.value = 0
  scene.value = undefined
  error.value = frameError.value = saveError.value = ''
  code.value = form.value = saving.value = saved.value = opening.value = false
  preview.value = null
  folders.value = []
  speed.value = 1
  elapsed.value = 0
  index.value = -1
  resetFrame()
  releaseURL()
  try {
    const prepared = prepareAnimation(props.source)
    transferSource = prepared.source
    scene.value = prepared.scene
    if (reduced.value) elapsed.value = scene.value.durationMs
  } catch (e) { error.value = (e as Error).message; code.value = true }
}
async function lookup() {
  if (!props.conversationId || !scene.value || !visible.value || !shown.value || saving.value) return
  const ticket = ++lookupTicket, {signal} = controller
  try {
    const receipt = await readAnimationPromotion(props.conversationId, transferSource, signal)
    if (signal.aborted || ticket !== lookupTicket) return
    saved.value = !!receipt.attachment_id || receipt.already_exists
    saveError.value = ''
  } catch {
    if (!signal.aborted && ticket === lookupTicket) { saved.value = false; saveError.value = '转存状态未知，请重新检查' }
  }
}
async function openForm() {
  if (!scene.value || !props.conversationId || opening.value || saving.value || saved.value) return
  const {signal} = controller
  opening.value = true
  try {
    const result = await request<{items:string[]}>('/me/material-folders','GET',undefined,signal)
    if (!signal.aborted) { folders.value = result.items; form.value = true }
  } catch (e) { if (!signal.aborted) notifyError(e) }
  finally { if (!signal.aborted) opening.value = false }
}
async function promote(value: FilePromotion) {
  if (!scene.value || !props.conversationId || saving.value || saved.value) return
  const {signal} = controller
  lookupTicket++
  saving.value = true
  try {
    await submitAnimationPromotion(props.conversationId, transferSource, value, signal)
    if (signal.aborted) return
    saved.value = true
    saveError.value = ''
    form.value = false
    ElMessage.success('已保存到资料库')
  } catch (e) {
    if (!signal.aborted) { saved.value = false; saveError.value = (e as Error).message; throw e }
  } finally { if (!signal.aborted) saving.value = false }
}
function motion() {
  reduced.value = !!media?.matches
  if (reduced.value && scene.value) elapsed.value = scene.value.durationMs
  resetFrame()
}
function visibility() { shown.value = !document.hidden }
function retry() { readSource(); void lookup() }
watch([() => props.source, () => props.conversationId], readSource, {immediate:true, flush:'sync'})
watch([visible, shown, scene], () => { void lookup() })
watch([active, channel], ([enabled], _, cleanup) => {
  ready.value = playing.value = false
  if (!enabled) { releaseURL(); return }
  window.addEventListener('message', message)
  timeout = setTimeout(() => { frameError.value = '播放器未响应，请重试' }, 15000)
  cleanup(() => { clearTimeout(timeout); window.removeEventListener('message', message) })
}, {flush:'post'})
onMounted(() => {
  media = window.matchMedia?.('(prefers-reduced-motion: reduce)')
  motion()
  media?.addEventListener('change', motion)
  document.addEventListener('visibilitychange', visibility)
  if (typeof IntersectionObserver !== 'undefined' && card.value) {
    observer = new IntersectionObserver(([entry]) => { visible.value = !!entry?.isIntersecting })
    observer.observe(card.value)
  } else visible.value = true
})
onBeforeUnmount(() => {
  controller.abort()
  observer?.disconnect()
  document.removeEventListener('visibilitychange', visibility)
  media?.removeEventListener('change', motion)
  releaseURL()
})
</script>
<template>
  <figure ref="card" class="mermaid-card animation-card" :class="{'animation-preview':previewMode}">
    <header>
      <strong>{{ scene?.title || filename || '动画包' }}</strong>
      <div class="actions">
        <button type="button" class="button small" :disabled="!scene" :aria-pressed="!code" @click="code=false">动画</button>
        <button type="button" class="button small" :aria-pressed="code" @click="code=true">代码</button>
        <DiagramMenu :allow-save="!!conversationId" :save-disabled="!scene || opening || saving || form" :saved="saved" @save="openForm">
          <button type="button" :disabled="!scene" @click="download"><AppIcon name="download" :size="15"/>下载动画包</button>
          <button v-if="!previewMode" type="button" :disabled="!scene" @click="preview='full'"><AppIcon name="maximize" :size="15"/>全屏</button>
        </DiagramMenu>
      </div>
    </header>
    <div v-if="error || frameError" class="animation-status" role="alert">{{ error || frameError }} <button type="button" class="button small" @click="retry">重试</button></div>
    <template v-if="code">
      <pre><code>{{ shownCode }}</code></pre>
      <RecordPagination v-if="pageCount > 1" v-model:offset="codePage" :total="pageCount" :page-size="1" label="动画源码分页" unit="页"/>
    </template>
    <template v-else-if="scene">
      <div class="animation-stage" :style="{aspectRatio:scene.canvas.width+'/'+scene.canvas.height}">
        <iframe v-if="active" :key="channel" ref="frame" :src="'/static/preview/animation.html#'+channel" sandbox="allow-scripts" referrerpolicy="no-referrer" title="动画播放器" tabindex="-1"/>
        <button v-if="!previewMode" type="button" class="stage-preview" aria-label="预览动画" @click="preview='window'"/>
      </div>
      <div class="animation-body">
        <div class="animation-controls" role="group" aria-label="播放控制">
          <button type="button" class="button small" :disabled="!ready || reduced" @click="send(playing?'pause':'play')">{{ playing ? '暂停' : '播放' }}</button>
          <button type="button" class="button small" :disabled="!ready || index<=0" @click="send('step',-1)">上一步</button>
          <button type="button" class="button small" :disabled="!ready || index>=scene.steps.length-1" @click="send('step',1)">下一步</button>
          <button type="button" class="button small" :disabled="!ready" @click="send('seek',0)">重置</button>
          <label>速度 <select v-model.number="speed" :disabled="!ready || reduced" @change="send('speed',speed)"><option v-for="rate in [.5,1,1.5,2]" :key="rate" :value="rate">{{ rate }}×</option></select></label>
          <button v-if="!previewMode" type="button" class="button small" @click="preview='window'">预览</button>
        </div>
        <div class="animation-progress">
          <input type="range" aria-label="进度" min="0" :max="scene.durationMs" step="1" :value="elapsed" :disabled="!ready" @input="send('seek',Number(($event.target as HTMLInputElement).value))">
          <small>{{ (elapsed/1000).toFixed(1) }} / {{ (scene.durationMs/1000).toFixed(1) }} 秒</small>
        </div>
        <div class="animation-caption">
          <strong v-if="current">第 {{ index+1 }} / {{ scene.steps.length }} 步 · {{ current.title }}</strong>
          <p>{{ current?.detail || scene.summary }}</p>
          <ul v-if="current?.notes.length"><li v-for="(note,i) in current.notes" :key="i">{{ note }}</li></ul>
          <details><summary>动画说明</summary><p>{{ scene.accessibility.alt }}</p></details>
        </div>
      </div>
    </template>
    <p v-if="saveError" class="animation-status" role="status">{{ saveError }} <button type="button" class="button small" @click="lookup">检查状态</button></p>
  </figure>
  <MaterialFileDialog v-if="form" :filename="name" :folders="folders" :save="promote" @close="form=false"/>
  <FilePreview v-if="preview" :file="{id:'animation',filename:name}" url="" :animation-source="source" :conversation-id="conversationId" :initial-fullscreen="preview==='full'" @close="preview=null; lookup()"/>
</template>
<style scoped>
.animation-card { min-inline-size:0; width:100%; overflow-wrap:anywhere }
.animation-card > header { position:relative; z-index:1 }
.animation-card header :deep(.mermaid-menu) { position:static }
.animation-card header :deep(.mermaid-menu-items) { box-sizing:border-box; min-inline-size:0; width:min(100%, 11rem); right:0 }
.animation-card header strong { flex:1 1 8rem }
.animation-card header .actions,.animation-controls { display:flex; flex-wrap:wrap; gap:.4rem; align-items:center }
.animation-card header .actions { margin:0 }
.animation-stage { position:relative; width:100%; min-height:5rem; max-height:65dvh; background:var(--surface-alt) }
.animation-stage iframe,.stage-preview { position:absolute; inset:0; border:0; width:100%; height:100% }
.stage-preview { background:transparent; cursor:zoom-in }
.stage-preview:focus-visible { outline-offset:-3px }
.animation-status,.animation-body { padding:.65rem .8rem }
.animation-progress { display:flex; align-items:center; gap:.6rem; margin:.5rem 0; font-variant-numeric:tabular-nums }
.animation-progress input { flex:1; min-width:0; accent-color:var(--accent) }
.animation-controls label { display:inline-flex; align-items:center; gap:.25rem }
.animation-caption p,.animation-caption ul { margin:.25rem 0 }
.animation-card summary { cursor:pointer }
.animation-card pre { max-height:50dvh; overflow:auto }
.animation-preview { display:flex; flex-direction:column; height:100%; }
.animation-preview > header,.animation-preview > nav { flex:none; }
.animation-preview .animation-stage { flex:1 1 0; max-height:none; }
.animation-preview .animation-body { flex:none; max-height:50%; overflow:auto; }
.animation-preview pre { flex:1; min-height:0; max-height:none; margin:0; }
@media (pointer:coarse) { .animation-controls button,.animation-controls select,.animation-card header button { min-height:44px } }
</style>
