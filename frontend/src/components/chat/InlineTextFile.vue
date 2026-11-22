<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ChatFile } from '../../types'
import { MAX_ANIMATION_BYTES } from '../../utils/animation'
import { decodePreviewText, readPreviewBytes, TEXT_PREVIEW_LIMIT } from '../../utils/previewFile'
import MermaidDiagram from '../MermaidDiagram.vue'

const MarkdownContent = defineAsyncComponent(() => import('../MarkdownContent.vue'))
const HtmlFrame = defineAsyncComponent(() => import('../HtmlFrame.vue'))
const props = defineProps<{file:ChatFile; url:string; mode:'mermaid'|'text'|'html'; saved?:boolean}>()
const emit = defineEmits<{preview:[file:ChatFile]; promote:[file:ChatFile]}>()
const host = ref<HTMLElement>(), visible = ref(false), text = ref(''), note = ref('')
const running = ref(props.mode === 'html' && props.file.origin === 'generated')
const markdown = computed(() => /\.(?:md|markdown)$/i.test(props.file.filename))
let observer: IntersectionObserver | undefined
let releaseTimer: ReturnType<typeof setTimeout> | undefined
onMounted(() => {
  if (!host.value || !('IntersectionObserver' in window)) { visible.value = true; return }
  observer = new IntersectionObserver(entries => {
    const near = entries.some(entry => entry.isIntersecting)
    if (props.mode === 'html') {
      clearTimeout(releaseTimer)
      // Keep a playing frame through brief reversals at the edge of the scrollport.
      if (near || !text.value) visible.value = near
      else releaseTimer = setTimeout(() => { visible.value = false }, 600)
    } else if (near) { visible.value = true; observer?.disconnect() }
  },{rootMargin:'160px'})
  observer.observe(host.value)
})
onUnmounted(() => { observer?.disconnect(); clearTimeout(releaseTimer) })
watch([visible,() => props.url,() => props.file.size,() => props.mode,running], async ([near,url,size,mode,activeHtml],_,cleanup) => {
  text.value = ''; note.value = ''
  if (!near || (mode === 'html' && !activeHtml)) return
  const limit = mode === 'html' ? MAX_ANIMATION_BYTES : TEXT_PREVIEW_LIMIT
  if (size && size > limit) { note.value = '文件超过内嵌预览大小限制'; return }
  const active = new AbortController()
  cleanup(() => active.abort())
  try {
    const signal = mode === 'html' ? active.signal : AbortSignal.any([active.signal,AbortSignal.timeout(15000)])
    const bytes = await readPreviewBytes(url,limit,signal)
    if (active.signal.aborted) return
    const value = decodePreviewText(bytes)
    text.value = mode !== 'text' ? value : value.slice(0,12000)
    if (!value) note.value = '空文件'
    else if (mode === 'text' && value.length > 12000) note.value = '已展示前 12,000 字，点击文件名查看完整内容'
  } catch (error) { if (!active.signal.aborted) note.value = error instanceof Error ? error.message : '无法预览原件' }
}, {immediate:true})
</script>

<template>
  <section ref="host" class="inline-document">
    <button type="button" class="button small file-link" @click="emit('preview',file)">{{ file.filename }}</button>
    <div v-if="mode === 'html'" class="html-actions">
      <button type="button" class="button small" @click="running = !running">{{ running ? '停止预览' : '运行预览' }}</button>
      <button type="button" class="button small" @click="emit('preview',file)">小窗查看</button>
      <button v-if="file.origin !== 'material' && !file.promoted_id && !saved" type="button" class="button small"
        @click="emit('promote',file)">转存资料</button>
    </div>
    <div v-if="mode === 'html' && running" class="html-viewport">
      <HtmlFrame v-if="text" :source="text" :filename="file.filename"/>
      <small v-else-if="note || visible" class="sub">{{ note || '正在加载预览…' }}</small>
    </div>
    <small v-else-if="note" class="sub">{{ note }}</small>
    <MermaidDiagram v-else-if="text && mode === 'mermaid'" :source="text" :filename="file.filename"/>
    <div v-else-if="text && mode === 'text'" class="inline-text">
      <MarkdownContent v-if="markdown" :source="text" :diagrams="false" document-mode/>
      <pre v-else>{{ text }}</pre>
    </div>
  </section>
</template>

<style scoped>
.inline-document { min-width:0; margin-block:.4rem; }
.html-actions { display:flex; flex-wrap:wrap; gap:.35rem; margin-block:.35rem; }
/* Reserve the frame before loading and while the offscreen document is released. */
.html-viewport { block-size:clamp(230px, 49svh, 480px); overflow:hidden; }
.html-viewport :deep(.html-frame) { block-size:100%; box-sizing:border-box; }
.inline-document small { display:block; }
.inline-text { overflow:auto; max-height:24rem; overscroll-behavior:contain; }
.inline-text pre { white-space:pre-wrap; overflow-wrap:anywhere; }
</style>
