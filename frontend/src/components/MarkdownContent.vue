<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { marked, type Token, type Tokens } from 'marked'
import DOMPurify from 'dompurify'
import type { ChatFile } from '../types'
import { previewKind } from '../utils/previewFile'
import { chatFileURL } from '../utils/chatFileURL'
import MermaidDiagram from './MermaidDiagram.vue'
import InlineTextFile from './chat/InlineTextFile.vue'

const GeneratedImage = defineAsyncComponent(() => import('./chat/GeneratedImage.vue'))

const props = withDefaults(defineProps<{ source: string; streaming?: boolean; documentMode?: boolean; manualPictures?: boolean; diagrams?: boolean; conversationId?: string; files?: ChatFile[]; artifactReply?: boolean; sandboxSources?: string[]; savedFileIds?: ReadonlySet<string> }>(), { diagrams: true })
const emit = defineEmits<{ preview: [file: ChatFile]; promote: [file: ChatFile] }>()
function fileLink(node: HTMLAnchorElement | HTMLImageElement, url: URL) {
  if (!props.files || url.origin !== location.origin) return false
  if (!/^\/api\/(?:me\/attachments\/|conversations\/[^/]+\/files\/)/.test(url.pathname)) return false
  const file = props.files.find(item => chatFileURL(item, props.conversationId) === url.pathname)
  if (file && node instanceof HTMLImageElement && /\.(?:png|jpe?g|webp|gif|bmp)$/i.test(file.filename)) {
    node.src = url.pathname
    node.loading = 'lazy'
    node.referrerPolicy = 'no-referrer'
    return true
  }
  const label = file?.filename || node.textContent || node.getAttribute('alt') || '会话文件'
  if (file) {
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'button small file-link'
    button.dataset.previewFile = file.id
    button.textContent = label
    node.replaceWith(button)
  } else node.replaceWith(document.createTextNode(label))
  return true
}
function preview(event: MouseEvent) {
  const id = event.target instanceof HTMLElement && event.target.closest<HTMLElement>('[data-preview-file]')?.dataset.previewFile
  const file = id && props.files?.find(item => item.id === id)
  if (file) emit('preview',file)
}
function clean(tokens: Token[]) {
  const fragment = DOMPurify.sanitize(marked.parser(tokens), {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: ['p','br','hr','h1','h2','h3','h4','h5','h6','strong','em','del','s','blockquote','ul','ol','li','pre','code','a','img','table','thead','tbody','tr','th','td','input'],
    ALLOWED_ATTR: ['href','title','src','alt','type','checked','start','align'], ALLOW_DATA_ATTR: false,
  })
  for (const link of fragment.querySelectorAll('a')) {
    try { const url = new URL(link.getAttribute('href') || '', location.href); if (!['http:','https:','mailto:','tel:'].includes(url.protocol)) throw Error(); if (fileLink(link,url)) continue; link.href = url.href; link.rel = 'noopener noreferrer'; link.target = '_blank' }
    catch { link.removeAttribute('href') }
  }
  for (const image of fragment.querySelectorAll('img')) {
    try {
      const src = image.getAttribute('src') || ''
      if (props.manualPictures) {
        const picture = /^(?:\.\/)?picture\/用户操作手册(?:-purge)?\/([a-z0-9-]+\.png)$/.exec(decodeURIComponent(src))?.[1]
        if (!picture) throw Error()
        image.src = '/api/help/用户操作手册.md?picture=' + picture
      } else {
        const url = new URL(src, location.href)
        if (props.documentMode || url.origin !== location.origin || !url.pathname.startsWith('/api/') || url.username || url.password) throw Error()
        if (fileLink(image,url)) continue
        image.src = url.href
      }
      image.loading = 'lazy'
      image.referrerPolicy = 'no-referrer'
    }
    catch { image.replaceWith(document.createTextNode(`[图片：${image.alt || '外部图片未自动加载'}]`)) }
  }
  if (props.artifactReply && props.sandboxSources?.length) {
    for (const code of fragment.querySelectorAll('code')) {
      const value = code.textContent.trim()
      if (value && props.sandboxSources.some(source => source === value || value.includes(source))) code.remove()
    }
  }
  for (const input of fragment.querySelectorAll('input')) { if (input.type === 'checkbox') { input.disabled = true; input.tabIndex = -1 } else input.remove() }
  const host = document.createElement('div'); host.append(fragment); return host.innerHTML
}
type FileBlock = {kind:'file'; file:ChatFile; url:string; mode:ReturnType<typeof previewKind>}
const htmlPage = /<!doctype\s+html\b|<html(?=[\s>])/i
const htmlFile = (file: ChatFile) => file.origin === 'generated' && /\.html?$/i.test(file.filename)
const pythonLanguages = new Set(['python','python3','py','py3'])
const htmlSourceLanguages = new Set(['html','htm','css','javascript','js','typescript','ts',...pythonLanguages])

// Marked exposes hard line breaks separately. Keep resolved Markdown links and literal code intact.
function attachmentParts(paragraph: Tokens.Paragraph) {
  const parts: (Tokens.Paragraph | string)[] = []
  const inline = paragraph.tokens
  let start = 0
  function append(end: number) {
    if (end > start) parts.push({...paragraph, tokens:inline.slice(start,end)})
  }
  for (let i = 0; i < inline.length; i++) {
    const item = inline[i]
    if (item.type !== 'text' || (i && inline[i-1].type !== 'br') || (i+1 < inline.length && inline[i+1].type !== 'br')) continue
    const match = /^ {0,3}\[\[attachment:([\w-]{1,80})\]\][ \t]*$/.exec(item.raw)
    if (!match) continue
    append(i-1)
    parts.push(match[1])
    start = i+2
  }
  append(inline.length)
  return parts
}

function closedFence(raw: string) {
  const opening = /^ {0,3}(`{3,}|~{3,})/.exec(raw)?.[1]
  const closing = /\n {0,3}(`{3,}|~{3,})[ \t]*(?:\n[ \t]*)*$/.exec(raw)?.[1]
  return !!opening && !!closing && closing[0] === opening[0] && closing.length >= opening.length
}

type TextBlock = { kind: 'text'; source: string }
let stableSource = ''
let stableBlocks: TextBlock[] = []
function plainInline(token: Token): boolean {
  if (token.type === 'text') return !token.tokens || token.tokens.every(plainInline)
  return token.type === 'br' || token.type === 'escape'
    || ['strong', 'em', 'del'].includes(token.type) && 'tokens' in token && !!token.tokens?.every(plainInline)
}

const rendered = computed(() => {
  const source = props.source || ''
  if (!props.streaming || !source.startsWith(stableSource)) {
    stableSource = ''
    stableBlocks = []
  }
  const blocks: ({ kind: 'text' | 'diagram'; source: string } | FileBlock)[] = [...stableBlocks]
  const embedded = new Set<string>()
  let pending: Token[] = []
  const generatedHtml = props.artifactReply ? props.files?.filter(htmlFile) || [] : []
  function addFile(file: ChatFile) {
    if (embedded.has(file.id)) return
    const url = chatFileURL(file, props.conversationId)
    if (!url && !file.expired) return
    flush()
    embedded.add(file.id)
    blocks.push({kind:'file',file,url,mode:previewKind(file.filename.split('.').pop()?.toLowerCase() || '')})
  }
  function flush() {
    if (pending.length) { blocks.push({ kind: 'text', source: clean(pending) }); pending = [] }
  }
  const tokens = marked.lexer(source.slice(stableSource.length), { gfm: true, breaks: true })
  let index = 0
  // Later definitions can resolve earlier bracketed links. Only freeze self-contained prose.
  while (props.streaming && index < tokens.length) {
    const token = tokens[index]!
    if ((token.type !== 'paragraph' && token.type !== 'heading') || token.raw.includes('[')
        || !token.tokens?.every(plainInline)) break
    const space = tokens[index + 1]?.type === 'space' ? tokens[index + 1] : undefined
    const raw = token.raw + (space?.raw || '')
    if (!raw.endsWith('\n\n') || !source.startsWith(raw, stableSource.length)) break
    const block: TextBlock = { kind: 'text', source: clean([token]) }
    stableBlocks.push(block)
    blocks.push(block)
    stableSource += raw
    index += space ? 2 : 1
  }
  for (const token of tokens.slice(index)) {
    if (props.files && token.type === 'paragraph' && token.raw.includes('[[attachment:')) {
      for (const part of attachmentParts(token as Tokens.Paragraph)) {
        if (typeof part !== 'string') { pending.push(part); continue }
        const file = props.files.find(item => item.id === part)
        if (file) addFile(file)
      }
      continue
    }
    if (props.artifactReply && token.type === 'html' && htmlPage.test(token.raw)) continue
    if (token.type !== 'code') { pending.push(token); continue }
    const language = token.lang?.trim().toLowerCase().split(/\s+/)[0] || ''
    if (props.artifactReply && language !== 'mermaid') {
      const code = token.text.trim()
      if (language === 'html' || language === 'htm' || htmlPage.test(code)
          || generatedHtml.length && htmlSourceLanguages.has(language)
          || props.sandboxSources && (pythonLanguages.has(language)
            || code && props.sandboxSources.some(source => source.includes(code) || code.includes(source)))) continue
    }
    if (props.diagrams && language === 'mermaid' && closedFence(token.raw)) {
      flush()
      blocks.push({ kind: 'diagram', source: token.text })
    } else pending.push(token)
  }
  for (const file of generatedHtml) addFile(file)
  flush()
  return blocks
})
</script>

<template>
  <div class="markdown-content" :class="{ 'manual-pictures': manualPictures }" @click="preview">
    <template v-for="(block, index) in rendered" :key="block.kind === 'file' ? block.file.id : index">
      <MermaidDiagram v-if="block.kind === 'diagram'" :source="block.source" :conversation-id="conversationId" />
      <template v-else-if="block.kind === 'file'">
        <p v-if="block.file.expired" class="expired-file sub" role="status">{{ block.file.filename }} · 文件已过期</p>
        <GeneratedImage v-else-if="block.mode === 'image'" :file="block.file" :url="block.url" @preview="emit('preview',$event)" />
        <InlineTextFile v-else-if="block.mode === 'text' || block.mode === 'mermaid' || block.mode === 'html'" :file="block.file" :url="block.url" :mode="block.mode" :saved="savedFileIds?.has(block.file.id)" @preview="emit('preview',$event)" @promote="emit('promote',$event)"/>
        <audio v-else-if="block.mode === 'audio'" :src="block.url" :aria-label="block.file.filename" controls preload="none"/>
        <video v-else-if="block.mode === 'video'" :src="block.url" :aria-label="block.file.filename" controls playsinline preload="none"/>
        <button v-else type="button" class="button small file-link" @click="emit('preview',block.file)">{{ block.file.filename }}</button>
      </template>
      <div v-else class="markdown-prose" v-html="block.source" />
    </template>
  </div>
</template>

<style scoped>
.manual-pictures :deep(img) { max-height: none; }
.expired-file { overflow-wrap:anywhere; margin-block:0.65rem; }
.markdown-content :is(audio,video) { max-inline-size:100%; max-block-size:60dvh; }
.markdown-content :deep(.file-link) { max-inline-size:100%; overflow-wrap:anywhere; text-align:start; }
</style>
