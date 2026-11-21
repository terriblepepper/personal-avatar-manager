<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onUnmounted, ref, shallowRef, watch } from 'vue'
import AppDialog from './AppDialog.vue'
import PreviewZoom from './PreviewZoom.vue'
import { usePreviewZoom } from '../composables/usePreviewZoom'
import { marked } from 'marked'
import { markText, normalize, pdfMatches } from '../utils/previewMatches'
import RecordPagination from './RecordPagination.vue'
import MarkdownContent from './MarkdownContent.vue'
import DocumentFields from './DocumentFields.vue'
import { readAnimation } from '../utils/readAnimation'
import { MAX_ANIMATION_BYTES } from '../utils/animation'
import { decodePreviewText, previewKind, readPreviewBytes, TEXT_PREVIEW_LIMIT } from '../utils/previewFile'
import MermaidDiagram from './MermaidDiagram.vue'

const HtmlFrame = defineAsyncComponent(() => import('./HtmlFrame.vue'))
const AnimationDiagram = defineAsyncComponent(() => import('./AnimationDiagram.vue'))
const props = defineProps<{ file: { id: string; filename: string; content_type?: string }; url: string; imageData?: string; animationSource?: string; conversationId?: string; initialFullscreen?: boolean }>()
const emit = defineEmits<{ close: [] }>()
interface PDFPage {
  getViewport(options: { scale: number }): { width: number; height: number; convertToViewportPoint(x: number, y: number): number[] }
  render(options: { canvasContext: CanvasRenderingContext2D; viewport: { width: number; height: number } }): { promise: Promise<void>; cancel(): void }
  getTextContent(): Promise<{ items: unknown[] }>
}
interface PDFDocument { numPages: number; getPage(page: number): Promise<PDFPage>; destroy(): Promise<void> }
interface PDFLibrary { GlobalWorkerOptions: { workerSrc: string }; getDocument(options: Record<string, unknown>): { promise: Promise<PDFDocument>; destroy(): Promise<void> } }
function isLibrary(value: unknown): value is PDFLibrary { return !!value && typeof value === 'object' && 'getDocument' in value && typeof value.getDocument === 'function' && 'GlobalWorkerOptions' in value }
const kind = ref(''), text = ref(''), objectURL = ref(''), error = ref(''), loading = ref(false)
const source = ref(false), page = ref(1), pages = ref(1)
const search = ref(''), matches = ref<number[] | null>(null), searchMessage = ref('')
let searchSequence = 0
const contents = ref<HTMLDetailsElement>()
const initiallyOpen = window.innerWidth > 650
const documentBody = ref<HTMLElement>(), sheet = ref<HTMLElement>()
const { available, naturalWidth, naturalHeight, scale, percent, fit, setScale, resetZoom } = usePreviewZoom(documentBody, sheet, kind)
const activePage = shallowRef<PDFPage>()
const paper = computed(() => activePage.value?.getViewport({ scale: 1 }))
const zoomable = computed(() => ['text', 'image', 'pdf'].includes(kind.value))
const sheetWidth = computed(() => (naturalWidth.value || available.value) * scale.value)
const hasSourceView = computed(() => kind.value === 'html' || kind.value === 'text' && !manual.value
  && (['md', 'markdown'].includes(extension.value) || !!structured.value || !!tabular.value.length))
const canvas = ref<HTMLCanvasElement>(), pdf = shallowRef<PDFDocument>(), controller = shallowRef<AbortController>()
let renderTask: { promise: Promise<void>; cancel(): void } | undefined, renderSequence = 0
const extension = computed(() => props.file.filename.split('.').pop()?.toLowerCase() || '')
const safeURL = computed(() => {
  try { const url = new URL(props.url, location.href); return url.origin === location.origin && url.pathname.startsWith('/api/') && !url.username && !url.password ? url.href : '' } catch { return '' }
})
const diagramImage = computed(() => props.imageData?.startsWith('data:image/png;base64,') ? props.imageData : '')
const downloadURL = computed(() => diagramImage.value || safeURL.value)
const manual = computed(() => !!safeURL.value && new URL(safeURL.value).pathname.startsWith('/api/help/')
  && ['md', 'markdown'].includes(extension.value))
const chapters = computed(() => {
  if (!manual.value) return []
  const tokens = marked.lexer(text.value)
  const depth = tokens.some(token => token.type === 'heading' && token.depth === 2) ? 2 : 1
  const result: { title: string; source: string }[] = []
  let section = { title: '文档', source: '' }, started = false
  for (const token of tokens) {
    if (token.type === 'heading' && token.depth === depth) {
      if (started) { result.push(section); section = { title: '', source: '' } }
      section.title = token.text
      started = true
    }
    section.source += token.raw
  }
  return [...result, section]
})
const textPages = computed(() => {
  if (kind.value !== 'text') return []
  if (manual.value) return chapters.value.map(chapter => chapter.source)
  const chunks = text.value.match(/[\s\S]{1,12000}/g) || ['']
  return chunks
})
const shownText = computed(() => textPages.value[page.value - 1] || '')
const structured = computed<{ value: unknown } | null>(() => {
  if (extension.value !== 'json' || !text.value) return null
  try { return { value: JSON.parse(text.value) } } catch { return null }
})
const tabular = computed(() => {
  const separator = extension.value === 'csv' ? ',' : extension.value === 'tsv' ? '\t' : ''
  if (!separator || !text.value) return []
  const rows: string[][] = []
  let row: string[] = [], cell = '', quoted = false
  for (let index = 0; index < text.value.length; index++) {
    const char = text.value[index]
    if (char === '"') {
      if (quoted && text.value[index + 1] === '"') { cell += '"'; index++ }
      else if (quoted || !cell) quoted = !quoted
      else cell += char
    } else if (!quoted && char === separator) { row.push(cell); cell = '' }
    else if (!quoted && (char === '\n' || char === '\r')) {
      if (char === '\r' && text.value[index + 1] === '\n') index++
      rows.push([...row, cell]); row = []; cell = ''
    } else cell += char
  }
  if (row.length || cell) rows.push([...row, cell])
  return quoted ? [] : rows
})
const richText = computed(() => !source.value && (!!structured.value || tabular.value.length > 0))
const visibleRows = computed(() => tabular.value.slice(1 + (page.value - 1) * 100, 1 + page.value * 100))
watch([richText, tabular, textPages], () => {
  if (kind.value !== 'text') return
  pages.value = richText.value
    ? Math.max(1, Math.ceil((tabular.value.length - 1) / 100))
    : textPages.value.length
  page.value = Math.min(page.value, pages.value)
})

function clearSearch() {
  searchSequence++
  search.value = ''; matches.value = null; searchMessage.value = ''
}
function go(number: number) {
  searchSequence++
  searchMessage.value = ''
  page.value = number
}
function goToChapter(number: number) {
  go(number)
  if (window.innerWidth <= 650 && contents.value) {
    contents.value.open = false
    contents.value.querySelector('summary')?.focus()
  }
}
function cleanup() {
  clearSearch()
  activePage.value = undefined
  controller.value?.abort(); renderSequence++; renderTask?.cancel(); void pdf.value?.destroy().catch(() => {}); pdf.value = undefined
  if (objectURL.value) URL.revokeObjectURL(objectURL.value); objectURL.value = ''
}
const open = ref(true)
function imageLoaded(event: Event) {
  const image = event.currentTarget as HTMLImageElement
  naturalWidth.value = image.naturalWidth
  naturalHeight.value = image.naturalHeight
}

async function renderPDF() {
  const ticket = ++renderSequence, previous = renderTask
  previous?.cancel()
  if (previous) await previous.promise.catch(() => {})
  const item = activePage.value, target = canvas.value
  if (ticket !== renderSequence || !item || !target) return
  try {
    const display = item.getViewport({ scale: scale.value })
    if (![display.width, display.height].every(size => Number.isFinite(size) && size > 0)) throw Error('页面尺寸异常，请下载原件查看')
    // Keep large zoom levels scrollable without allocating an equally large bitmap.
    const density = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(4000000 / (display.width * display.height)), 16000 / Math.max(display.width, display.height))
    const viewport = item.getViewport({ scale: scale.value * density })
    const context = target.getContext('2d')
    if (!context) return
    target.width = Math.max(1, Math.round(viewport.width))
    target.height = Math.max(1, Math.round(viewport.height))
    renderTask = item.render({ canvasContext: context, viewport })
    await renderTask.promise
  } catch (e) { if (ticket === renderSequence && e instanceof Error && e.name !== 'RenderingCancelledException') error.value = e.message }
}
async function load() {
  cleanup(); const active = new AbortController(); controller.value = active
  loading.value = true; error.value = ''; text.value = ''; page.value = 1; pages.value = 1; source.value = false; resetZoom()
  try {
    if (extension.value === 'avanim') {
      kind.value = 'animation'
      const value = props.animationSource ?? await readAnimation(props.url, active.signal)
      if (!active.signal.aborted) text.value = value
      return
    }
    if (diagramImage.value) { kind.value = 'image'; return }
    if (!safeURL.value) throw Error('只能预览本平台有权访问的文件')
    const ext = extension.value
    const url = new URL(safeURL.value)
    if (ext === 'docx' && /^\/api\/me\/attachments\/[\w-]+$/.test(url.pathname)) {
      kind.value = 'text'
      const bytes = await readPreviewBytes(url.pathname+'/preview-text',TEXT_PREVIEW_LIMIT,active.signal)
      if (!active.signal.aborted) text.value = new TextDecoder().decode(bytes)
      return
    }
    kind.value = previewKind(ext)
    if (!kind.value) return
    const limit = kind.value === 'html' ? MAX_ANIMATION_BYTES : ['audio','video'].includes(kind.value) ? 64 * 1024 * 1024 : ['text','mermaid'].includes(kind.value) ? TEXT_PREVIEW_LIMIT : 10 * 1024 * 1024
    const bytes = await readPreviewBytes(safeURL.value,limit,active.signal)
    if (active.signal.aborted) return
    if (['text','mermaid','html'].includes(kind.value)) {
      text.value = decodePreviewText(bytes)
      if (kind.value === 'text') pages.value = textPages.value.length
    } else if (kind.value === 'pdf') {
      const moduleURL = '/static/vendor/pdfjs/legacy/build/pdf.mjs', value: unknown = await import(/* @vite-ignore */ moduleURL)
      if (active.signal.aborted) return
      if (!isLibrary(value)) throw Error('PDF 组件加载失败')
      value.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/legacy/build/pdf.worker.mjs'
      const task = value.getDocument({ data: bytes, cMapUrl: '/static/vendor/pdfjs/cmaps/', cMapPacked: true, standardFontDataUrl: '/static/vendor/pdfjs/standard_fonts/', wasmUrl: '/static/vendor/pdfjs/wasm/', isEvalSupported: false, enableXfa: false, maxImageSize: 16000000, isOffscreenCanvasSupported: false })
      active.signal.addEventListener('abort', () => { void task.destroy().catch(() => {}) }, { once: true })
      const document = await task.promise
      if (active.signal.aborted) { await document.destroy(); return }
      pdf.value = document; pages.value = document.numPages
    } else {
      const mime: Record<string,string> = { mp3:'audio/mpeg', wav:'audio/wav', m4a:'audio/mp4', ogg:'audio/ogg', flac:'audio/flac', mp4:'video/mp4', mov:'video/quicktime', webm:'video/webm', png:'image/png', jpg:'image/jpeg', jpeg:'image/jpeg', webp:'image/webp', gif:'image/gif', bmp:'image/bmp' }
      objectURL.value = URL.createObjectURL(new Blob([bytes], { type: mime[ext] || 'application/octet-stream' }))
    }
  } catch (e) { if (!active.signal.aborted) error.value = e instanceof Error ? e.message : '预览失败' }
  finally { if (!active.signal.aborted) loading.value = false }
}
async function pageText(number: number) {
  if (pdf.value) {
    const pdfPage = await pdf.value.getPage(number)
    const { items } = await pdfPage.getTextContent()
    return items.map(item => item && typeof item === 'object' && 'str' in item && typeof item.str === 'string' ? item.str : '').join('')
  }
  if (richText.value) return tabular.value.length
    ? [tabular.value[0], ...tabular.value.slice(1 + (number - 1) * 100, 1 + number * 100)].flat().join(' ')
    : text.value
  return textPages.value[number - 1] || ''
}
async function find(value: string, direction: 1 | -1 = 1) {
  const needle = normalize(value), changed = normalize(search.value) !== needle
  if (!needle) { clearSearch(); return }
  const ticket = ++searchSequence
  search.value = value
  if (changed || !matches.value) {
    matches.value = null
    searchMessage.value = '正在搜索文档…'
    const found: number[] = []
    try {
      for (let number = 1; number <= pages.value; number++) {
        const content = await pageText(number)
        if (ticket !== searchSequence) return
        if (normalize(content).includes(needle)) found.push(number)
      }
    } catch (e) {
      if (ticket === searchSequence) searchMessage.value = '搜索失败：' + (e instanceof Error ? e.message : String(e))
      return
    }
    matches.value = found
    searchMessage.value = ''
    if (found.length) page.value = found[0]
  } else if (matches.value.length) {
    const index = matches.value.indexOf(page.value)
    page.value = matches.value[index < 0 ? 0 : (index + direction + matches.value.length) % matches.value.length]
  }
}
const boxes = ref<ReturnType<typeof pdfMatches>>([])
watch([activePage, matches, page, search, sheet], async ([item, found, number, query, content], _, cleanup) => {
  boxes.value = []
  if (!found?.includes(number) || !content) return
  if (kind.value === 'text') {
    const clear = markText(content, query)
    if (clear) cleanup(clear)
    return
  }
  if (!item) return
  let current = true
  cleanup(() => { current = false })
  try {
    const { items } = await item.getTextContent()
    if (!current) return
    boxes.value = pdfMatches(items, query, item.getViewport({scale:1}))
    await nextTick()
    if (current) content.querySelector('mark')?.scrollIntoView({block:'center',inline:'nearest'})
  } catch (error) { if (current) searchMessage.value = error instanceof Error ? error.message : '无法标记搜索结果' }
}, {flush:'post'})
watch(source, clearSearch)
watch(page, () => { if (documentBody.value) documentBody.value.scrollTop=0 })
watch([pdf, page], async ([document, number], _, cleanup) => {
  let current = true
  cleanup(() => { current = false })
  if (!document) return
  try {
    const item = await document.getPage(number)
    if (current) { activePage.value = item; naturalWidth.value = item.getViewport({ scale: 1 }).width }
  } catch (e) { if (current) error.value = e instanceof Error ? e.message : '无法读取 PDF 页面' }
})
watch([activePage, scale, canvas], renderPDF, { flush: 'post' })
watch([() => props.url, () => props.animationSource, extension, diagramImage], load, { immediate: true })
onUnmounted(cleanup)
</script>
<template>
  <AppDialog align-center v-model="open" :title="file.filename" :subtitle="manual ? '帮助文档' : '文件预览'" close-label="关闭预览"
    width="min(1100px, calc(100vw - 1rem))" class="vue-file-preview" :class="{ 'preview-media-window': ['image','pdf','animation','video','mermaid','html'].includes(kind) && !error }"
    :initial-fullscreen="initialFullscreen" @closed="emit('close')">
    <div v-if="kind !== 'animation' || downloadURL" class="preview-toolbar">
      <button v-if="hasSourceView" type="button" class="button small" :aria-pressed="source" @click="source = !source">{{ source ? '返回阅读' : '查看源码' }}</button>
      <a v-if="downloadURL" :href="downloadURL" :download="file.filename" class="button small">{{ diagramImage ? '下载图片' : manual ? '下载手册' : extension === 'mmd' ? '下载源码' : '下载原件' }}</a>
      <span v-if="chapters.length && !loading && !error" aria-label="当前章节">{{ chapters[page - 1]?.title }}</span>
      <PreviewZoom v-if="zoomable && !loading && !error" :percent="percent" :fit="fit" :fit-label="kind === 'image' ? '适应窗口' : '适应宽度'" @scale="setScale"/>
    </div>
    <p v-if="loading" role="status">正在读取文件…</p><p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <div class="preview-reader" :class="{ 'manual-layout': chapters.length && !loading && !error }">
    <div ref="documentBody" class="preview-document overflow-auto" :data-kind="kind" :class="{ 'visual-preview': kind === 'pdf' || kind === 'image' }">
      <div v-if="zoomable" class="preview-stage"><div ref="sheet" class="preview-sheet" :style="{ width: sheetWidth ? sheetWidth + 'px' : undefined }">
      <canvas v-if="kind === 'pdf'" ref="canvas" :aria-label="'PDF 第 ' + page + ' 页'"
        :style="paper ? { aspectRatio: paper.width + '/' + paper.height } : {}"/>
      <img v-else-if="kind === 'image' && (diagramImage || objectURL)" :src="diagramImage || objectURL" :alt="file.filename"
        @load="imageLoaded">
      <div v-else-if="kind === 'text'" :style="{ zoom: scale }"><DocumentFields v-if="!source && structured" :value="structured.value"/>
        <table v-else-if="!source && tabular.length" class="preview-data-table">
          <thead><tr><th v-for="(cell,index) in tabular[0]" :key="index" scope="col">{{ cell }}</th></tr></thead>
          <tbody><tr v-for="(row,index) in visibleRows" :key="index"><td v-for="(cell,column) in row" :key="column">{{ cell }}</td></tr></tbody>
        </table>
        <MarkdownContent v-else-if="!source && ['md','markdown'].includes(extension)" :source="shownText"/><pre v-else>{{ shownText }}</pre></div>
      <mark v-for="(box, index) in boxes" :key="index" class="preview-pdf-match" aria-label="搜索匹配" :style="box"/>
      </div></div>
      <template v-else-if="kind === 'html' && !loading && !error">
        <pre v-if="source" class="html-source">{{ text }}</pre>
        <HtmlFrame v-else :source="text" :filename="file.filename"/>
      </template>
      <AnimationDiagram v-else-if="kind === 'animation' && !loading && !error" :source="text" :filename="file.filename" :conversation-id="conversationId" preview-mode/>
      <MermaidDiagram v-else-if="kind === 'mermaid' && !loading && !error" :source="text" :filename="file.filename" preview-mode/>
      <audio v-else-if="kind === 'audio' && objectURL" :src="objectURL" :aria-label="file.filename" controls preload="metadata" />
      <video v-else-if="kind === 'video' && objectURL" :src="objectURL" :aria-label="file.filename" controls preload="metadata" />
      <p v-else-if="!loading && !error">此格式请下载原件查看。</p>
    </div>
    <nav v-if="chapters.length && !loading && !error" class="manual-contents" aria-label="章节目录">
      <details ref="contents" :open="initiallyOpen">
        <summary>目录</summary>
        <button v-for="(chapter, index) in chapters" :key="index" type="button" class="button small"
          :aria-current="page === index + 1 ? 'page' : undefined" @click="goToChapter(index + 1)">{{ chapter.title }}</button>
      </details>
    </nav>
    </div>
    <RecordPagination v-if="!loading && !error && (kind === 'text' || kind === 'pdf')" :offset="page-1" :total="pages" :page-size="1"
      :search="search" :label="manual ? '章节分页' : '文档分页'" :unit="manual ? '章' : '页'" @update:offset="go($event + 1)" @update:search="find"/>
    <div v-if="matches || searchMessage" class="preview-matches">
      <small role="status">{{ searchMessage || (matches?.length ? matches.length + (manual ? ' 章匹配' : ' 页匹配') + ' · ' + (matches.indexOf(page) + 1) + ' / ' + matches.length : '未找到匹配内容') }}</small>
      <template v-if="matches?.length">
        <button class="button small" type="button" title="Shift+Enter 上一处" @click="find(search, -1)">上一处</button>
        <button class="button small" type="button" title="Enter 下一处" @click="find(search, 1)">下一处</button>
      </template>
    </div>
  </AppDialog>
</template>

<style scoped>
.preview-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; border-block: 1px solid var(--border); padding-block: .5rem; }
.preview-stage { display: flex; min-width: 100%; min-height: 100%; width: max-content; }
.preview-sheet { position: relative; flex: none; margin-inline: auto; min-width: 0; }
.preview-pdf-match { position: absolute; pointer-events: none; background: rgb(255 200 0 / 36%); }
:global(::highlight(preview-match)) { background: #ffe598; color: #201a0a; }
.visual-preview .preview-sheet { margin-block: auto; }
.preview-sheet > canvas, .preview-sheet > img { display: block; width: 100%; height: auto; max-width: none; }
.preview-reader { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.preview-document > audio { display: block; width: 100%; }
.preview-document > video { display: block; width: 100%; height: 100%; object-fit: contain; }
.preview-document > :is(.animation-card, .mermaid-card) { margin: 0; }
.preview-document > .html-frame { block-size: min(66dvh, 780px); }
.html-source { white-space: pre-wrap; overflow-wrap: anywhere; }
.manual-layout { display: grid; grid-template-columns: minmax(0, 1fr) 3rem; grid-template-rows: minmax(0,1fr); min-height: 0; }
.manual-layout:has(details[open]) { grid-template-columns: minmax(0, 1fr) min(30%, 12rem); }
.manual-layout :deep(.markdown-content) { max-width:46rem; margin-inline:auto; padding:clamp(10px,2vw,28px); line-height:1.8; overflow-wrap:anywhere; }
.manual-layout :deep(.markdown-content h2) { margin-block:0 1rem; font-size:1.45rem; }
.manual-layout :deep(.markdown-content h3) { margin-top:1.4rem; font-size:1.05rem; }
.manual-layout :deep(.markdown-content li + li) { margin-top:.5rem; }
.manual-layout :deep(.markdown-content th) { color:var(--text); background:var(--accent-subtle); }
.manual-layout :deep(.markdown-content blockquote) { margin-inline:0; border-inline-start:3px solid var(--accent); padding:.6rem 1rem; background:var(--surface-alt); }
@media(max-width:650px) {
  .manual-layout,.manual-layout:has(details[open]) { display:flex; flex-direction:column; }
  .manual-contents { order:-1; max-height:28dvh; }
}
.manual-contents { overflow: auto; padding: .3rem; border-inline-start: 1px solid var(--border); }
.manual-contents summary { cursor: pointer; font-weight: 600; }
.manual-contents button { display: block; width: 100%; margin-block: .25rem; white-space: normal; text-align: start; }
.manual-contents button[aria-current="page"] { color: var(--accent); background: var(--accent-subtle); }
.preview-matches { display: flex; align-items: center; justify-content: flex-end; gap: .4rem; }
</style>
