<script setup lang="ts">
import { defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { request, notifyError } from '../api/client'
import { requestId } from '../utils/requestId'

import AppIcon from './AppIcon.vue'
import DiagramMenu from './DiagramMenu.vue'
import MaterialFileDialog from './MaterialFileDialog.vue'
import type { FilePromotion } from '../types'
import PreviewZoom from './PreviewZoom.vue'
import { usePreviewZoom } from '../composables/usePreviewZoom'

const FilePreview = defineAsyncComponent(() => import('./FilePreview.vue'))

const props = defineProps<{ source: string; conversationId?: string; filename?: string; previewMode?: boolean }>()
const frame = ref<HTMLIFrameElement>(), height = ref(180), code = ref(false)
const viewport = ref<HTMLElement>(), sheet = ref<HTMLElement>(), preview = ref('')
const { scale: zoom, naturalWidth, percent, fit, setScale } = usePreviewZoom(viewport, sheet, ref(props.previewMode ? 'diagram' : 'image'))
const svg = ref(''), error = ref(''), blocked = ref(0), link = ref(''), saved = ref(false), promoting = ref(false)
let channel = requestId(), ready = false, timer: ReturnType<typeof setTimeout> | undefined
let controller = new AbortController()
function send(resources: Record<string, string> = {}, icons: Record<string, unknown> = {}) {
  if (!ready) return
  channel = requestId(); svg.value = ''; error.value = ''
  frame.value?.contentWindow?.postMessage({ type: 'render', channel, code: props.source, resources, icons, font: getComputedStyle(document.body).fontFamily }, '*')
  clearTimeout(timer); timer = setTimeout(() => { error.value = '绘图超过 20 秒，请简化图表或重试' }, 20000)
}
function message(event: MessageEvent<unknown>) {
  if (event.source !== frame.value?.contentWindow || event.origin !== 'null' || !event.data || typeof event.data !== 'object') return
  const data = event.data
  if (!('mermaidSandbox' in data) || !data.mermaidSandbox || !('type' in data)) return
  if (data.type === 'ready') { ready = true; send(); return }
  if (!('channel' in data) || data.channel !== channel) return
  if (data.type === 'complete' && 'svg' in data && typeof data.svg === 'string' && data.svg.length <= 4 * 1024 * 1024) {
    clearTimeout(timer); svg.value = data.svg
    if ('blocked' in data) blocked.value = Number(data.blocked) || 0
    naturalWidth.value = Math.max(1, Number('width' in data && data.width) || 320)
    height.value = Math.max(1, Number('height' in data && data.height) || 180)
  } else if (data.type === 'error' && 'message' in data) { clearTimeout(timer); error.value = String(data.message) }
  else if (data.type === 'preview') void openPreview()
  else if (data.type === 'link' && 'url' in data && typeof data.url === 'string') {
    try { const url = new URL(data.url); if (['http:','https:'].includes(url.protocol) && !url.username && !url.password) link.value = url.href } catch { /* invalid sandbox link */ }
  } else if (data.type === 'callback' && 'name' in data) ElMessage.info('图表交互：' + String(data.name).slice(0, 80))
}
function download(kind: 'mmd' | 'svg' | 'png') {
  const save = (blob: Blob) => { const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = (props.filename?.replace(/\.mmd$/i, '') || 'diagram') + '.' + kind; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
  if (kind !== 'png') save(new Blob([kind === 'mmd' ? props.source : svg.value], { type: kind === 'svg' ? 'image/svg+xml' : 'text/plain' }))
  else pngCanvas().then(canvas => canvas.toBlob(blob => {
    if (blob) save(blob)
    else notifyError(Error('无法导出 PNG'))
  }, 'image/png')).catch(notifyError)
}
function sizedSvg(width: number, height: number) {
  const root = new DOMParser().parseFromString(svg.value, 'image/svg+xml').documentElement
  root.setAttribute('width', String(width))
  root.setAttribute('height', String(height))
  return new XMLSerializer().serializeToString(root)
}
async function pngCanvas() {
  const width = naturalWidth.value, h = height.value
  const scale = Math.min(1, 4096 / Math.max(width, h))
  const canvas = document.createElement('canvas')
  canvas.width = Math.ceil(width * scale)
  canvas.height = Math.ceil(h * scale)
  const image = new Image()
  image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(sizedSvg(canvas.width, canvas.height))
  await image.decode()
  const context = canvas.getContext('2d'); if (!context) throw Error('无法导出图形'); context.drawImage(image, 0, 0)
  return canvas
}
function openPreview() {
  if (svg.value) preview.value = sizedSvg(naturalWidth.value, height.value)
}
async function external() {
  try {
    const resources: Record<string,string> = {}, icons: Record<string,unknown> = {}
    const urls = [...new Set([...props.source.matchAll(/(?:\bimg\s*:\s*["']|<img\b[^>]*\bsrc\s*=\s*["'])(https:\/\/[^"'<>\s]+)["']/gi)].map(m => m[1]!))]
    for (const url of urls.slice(0, 5)) resources[url] = (await request<{ data_url: string }>('/diagrams/image', 'POST', { url }, controller.signal)).data_url
    const packs = new Map<string, Set<string>>()
    for (const match of props.source.matchAll(/\b([a-z][a-z0-9-]{0,39}):([a-z0-9][a-z0-9-]{0,79})\b/g)) {
      const prefix = match[1]!, name = match[2]!
      if (!['http','https'].includes(prefix)) { if (!packs.has(prefix)) packs.set(prefix, new Set()); packs.get(prefix)!.add(name) }
    }
    for (const [prefix, names] of packs) icons[prefix] = await request('/diagrams/icons', 'POST', { prefix, names: [...names].slice(0,32) }, controller.signal)
    send(resources, icons)
  } catch (e) { notifyError(e) }
}
async function promote(value: FilePromotion) {
  const { conversationId, source } = props
  if (!conversationId || !source.trim()) return
  await request('/conversations/' + encodeURIComponent(conversationId) + '/diagrams/promote',
    'POST', { source, ...value }, controller.signal)
  if (props.conversationId !== conversationId || props.source !== source) return
  saved.value = true
  promoting.value = false
  ElMessage.success('已保存到资料库')
}
watch([() => props.conversationId, () => props.source], () => { saved.value = false; promoting.value = false; send() })
onMounted(() => window.addEventListener('message', message))
onUnmounted(() => { controller.abort(); clearTimeout(timer); window.removeEventListener('message', message) })
</script>
<template>
  <figure class="mermaid-card" :class="{'preview-diagram':previewMode}">
    <header>
      <small role="status">Mermaid · {{ error || (svg ? '绘图完成' : '正在绘图…') }}</small>
      <div class="actions">
        <button type="button" class="button small" :aria-pressed="!code" @click="code=false">图形</button>
        <button type="button" class="button small" :aria-pressed="code" @click="code=true">代码</button>
        <DiagramMenu :allow-save="!!conversationId" :save-disabled="!source.trim() || promoting" :saved="saved" @save="promoting=true">
          <button type="button" @click="download('mmd')"><AppIcon name="download" :size="15"/>下载代码（.mmd）</button>
          <button type="button" :disabled="!svg" @click="download('png')"><AppIcon name="download" :size="15"/>下载 PNG 图片</button>
          <button type="button" :disabled="!svg" @click="download('svg')"><AppIcon name="download" :size="15"/>下载 SVG 图片</button>
          <template v-if="blocked" #extra><button type="button" @click="external">加载外部图片与图标（{{ blocked }}）</button></template>
        </DiagramMenu>
      </div>
    </header>
    <pre v-show="code"><code>{{ source }}</code></pre>
    <div class="diagram-tools">
      <button type="button" class="button small icon-button" title="重试绘图" aria-label="重试绘图" :disabled="!svg && !error" @click="send()"><AppIcon name="rebuild" :size="16"/></button>
      <PreviewZoom v-if="!code" :percent="percent" :fit="fit" @scale="setScale"/>
    </div>
    <div v-show="!code" ref="viewport" class="diagram-viewport" tabindex="0" aria-label="图表，可滚动查看" @keydown.enter.self.prevent="openPreview" @keydown.space.self.prevent="openPreview">
      <div ref="sheet" :style="{ width: (naturalWidth || 320) * zoom + 'px', height: height * zoom + 'px' }">
        <iframe ref="frame" src="/static/preview/mermaid.html" title="Mermaid 隔离图表" sandbox="allow-scripts" referrerpolicy="no-referrer"/>
      </div>
    </div>
    <p v-if="link"><a class="button small" :href="link" target="_blank" rel="noopener noreferrer">打开图表链接</a></p>
  </figure>
  <MaterialFileDialog v-if="promoting" filename="Mermaid绘图.mmd" :save="promote" @close="promoting=false"/>
  <FilePreview v-if="preview" :file="{ id: 'diagram', filename: 'Mermaid 图表.svg' }" url="" :diagram-svg="preview" @close="preview=''"/>
</template>
<style scoped>
.diagram-viewport { max-height: 65dvh; overflow: auto; overflow-anchor: none; }
.preview-diagram { display:flex; flex-direction:column; height:100%; }
.preview-diagram > pre { flex:1; min-height:0; overflow:auto; margin:0; }
.preview-diagram .diagram-viewport { flex:1; min-height:0; max-height:none; }
.diagram-viewport > div { margin-inline: auto; }
.diagram-viewport iframe { display: block; border: 0; width: 100%; height: 100%; max-width: none; }
.diagram-tools { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: start; gap: .5rem; padding: .5rem; }
.diagram-tools :deep(.preview-zoom) { justify-content: flex-end; }
</style>
