<script setup lang="ts">
import { computed, ref } from 'vue'
import { marked } from 'marked'
import { ElMessage } from 'element-plus'
import { request } from '../../api/client'
import { chatFileURL, chatPreviewURL } from '../../utils/chatFileURL'
import AppIcon from '../AppIcon.vue'
import ContextMenu from '../ContextMenu.vue'
import MarkdownContent from '../MarkdownContent.vue'
import MaterialFileDialog from '../MaterialFileDialog.vue'
import type { ChatFile, FilePromotion, ToolEvent } from '../../types'

const props = withDefaults(defineProps<{ source: string; events?: ToolEvent[]; attachments?: ChatFile[]; conversationId?: string; diagrams?: boolean; artifactReply?: boolean; hideCitations?: boolean }>(), { diagrams:true, artifactReply:true })
const emit = defineEmits<{ preview: [file: ChatFile] }>()
const panel = ref<HTMLElement>(), trigger = ref<HTMLButtonElement>(), open = ref(false)
const saved = ref(new Set<string>()), promoting = ref<ChatFile>()
const menu = ref<InstanceType<typeof ContextMenu>>()
function openFileMenu(event: MouseEvent | KeyboardEvent, file: ChatFile) {
  const url = chatPreviewURL(file, props.conversationId), anchor = event.currentTarget
  if (!url || !(anchor instanceof HTMLElement)) return
  const actions = /\.mmd$/i.test(file.filename)
    ? [{label:'预览与下载', icon:'download', run:() => preview(file)}]
    : [{label:'下载', icon:'download', run:() => {
      const link = document.createElement('a')
      link.href = url
      link.download = file.filename
      link.click()
    }}]
  if (file.origin === 'material' && !/\.mmd$/i.test(file.filename)) actions.unshift({label:'预览', icon:'book', run:() => preview(file)})
  if (file.origin !== 'material' && chatFileURL(file, props.conversationId) && !file.promoted_id && !saved.value.has(file.id)) actions.push({label:'转存资料', icon:'save', run:() => {
    panel.value?.hidePopover()
    promoting.value = file
  }})
  void menu.value?.open(event, anchor, actions)
}
async function promote(value: FilePromotion) {
  const file = promoting.value
  const url = file && file.origin !== 'material' && chatFileURL(file, props.conversationId)
  if (!file || !url) return
  await request(url.slice(4)+'/promote','POST',value)
  saved.value.add(file.id)
  promoting.value = undefined
  ElMessage.success('已保存到资料库')
}
const sandboxSources = computed(() => {
  const events = props.events?.filter(item => item.group === 'sandbox')
  return events?.length ? events.flatMap(item => {
    const input = item.input
    if (input && typeof input === 'object' && 'code' in input && typeof input.code === 'string' && input.code.trim())
      return [input.code.trim()]
    return []
  }) : undefined
})
const content = computed(() => {
  const citations = new Map<string, Set<string>>()
  let body = props.events?.some(item => item.group === 'notice' && item.status === 'required' && item.name === props.source.trim()) ? '' : props.source
  const last = body.includes('引用来源') ? marked.lexer(body).filter(token => token.type !== 'space').at(-1) : undefined
  if (last?.type === 'blockquote') {
    const [heading, ...lines] = last.raw.trimEnd().split(/\r?\n/).map(line => line.replace(/^>\s?/, '').trim())
    const sources = lines.map(line => /^(.+) · (RAG|源文件)$/.exec(line))
    if (/^引用来源[：:]$/.test(heading) && sources.length && sources.every(Boolean)) {
      for (const source of sources) if (source && !props.hideCitations) {
        const types = citations.get(source[1]) || new Set<string>()
        types.add(source[2])
        citations.set(source[1],types)
      }
      body = body.trimEnd().slice(0, -last.raw.trimEnd().length).trimEnd()
    }
  }
  const entries: { key:string; name:string; kinds:Set<string>; file?:ChatFile; url?:string }[] = []
  const seen = new Set<string>(), names = new Set<string>()
  for (const file of props.attachments || []) {
    if (seen.has(file.id)) continue
    seen.add(file.id)
    names.add(file.filename)
    const kinds = new Set(citations.get(file.filename))
    kinds.add(({material:'资料原件', generated:'生成文件', attachment:'附件'} as Record<string,string>)[file.origin || ''] || '会话文件')
    if (file.expired) kinds.add('已过期')
    entries.push({key:'file/'+file.id, name:file.filename, kinds, file, url:chatPreviewURL(file,props.conversationId)})
  }
  for (const [name,kinds] of citations) {
    if (!names.has(name)) entries.push({key:'citation/'+name, name, kinds})
  }
  return { body, entries }
})

function toggle() {
  const node = panel.value, anchor = trigger.value?.getBoundingClientRect()
  if (!node || !anchor) return
  if (node.matches(':popover-open')) { node.hidePopover(); return }
  node.showPopover()
  const bounds = node.getBoundingClientRect()
  node.style.left = Math.max(12, Math.min(anchor.left, innerWidth - bounds.width - 12)) + 'px'
  node.style.top = (innerHeight - anchor.bottom >= bounds.height + 8
    ? anchor.bottom + 8 : Math.max(12, anchor.top - bounds.height - 8)) + 'px'
}

function sourceToggled() {
  open.value = panel.value?.matches(':popover-open') ?? false
  if (!open.value) menu.value?.close()
}

function preview(file: ChatFile) {
  panel.value?.hidePopover()
  emit('preview', file)
}
</script>

<template>
  <MarkdownContent v-if="content.body || attachments?.length" :source="content.body" :conversation-id="conversationId" :diagrams="diagrams" :artifact-reply="artifactReply" :sandbox-sources="sandboxSources" :files="attachments || []" :saved-file-ids="saved" @preview="emit('preview',$event)" @promote="promoting = $event"/>
  <div v-if="content.entries.length" class="answer-sources">
    <button ref="trigger" type="button" class="source-trigger" aria-haspopup="dialog" :aria-expanded="open" @click="toggle">
      <span class="source-avatar-stack" aria-hidden="true">
        <span v-for="entry in content.entries.slice(0, 3)" :key="entry.key" class="source-avatar" :data-kind="entry.file ? 'original' : 'rag'">
          <AppIcon :name="entry.file ? 'import' : 'book'"/>
        </span>
      </span>{{ content.entries.length }} {{ hideCitations ? '份文件' : '份资料' }}
    </button>
    <div ref="panel" class="source-popover" popover="auto" role="dialog" :aria-label="hideCitations ? '交付文件' : '引用来源'" @toggle="sourceToggled">
      <div class="source-popover-head"><strong>{{ hideCitations ? '交付文件' : '资料与来源' }} · {{ content.entries.length }}</strong></div>
      <div class="source-chips">
        <template v-for="entry in content.entries" :key="entry.key">
          <span v-if="entry.file" class="source-file"><button type="button" class="source-chip" :disabled="!entry.url" :title="entry.name" aria-haspopup="menu"
            @click="preview(entry.file)" @contextmenu="openFileMenu($event,entry.file)" @keydown="openFileMenu($event,entry.file)">
            <AppIcon name="import"/><span class="source-filename">{{ entry.name }}</span><small>{{ [...entry.kinds].join(' / ') }}</small>
          </button><a v-if="entry.url" :href="entry.url" :download="entry.name" class="source-download"
            :aria-label="'下载 '+entry.name" :title="'下载 '+entry.name"><AppIcon name="download" :size="15"/></a></span>
          <span v-else class="source-chip" :title="entry.name">
            <AppIcon name="book"/><span class="source-filename">{{ entry.name }}</span><small>{{ [...entry.kinds].join(' / ') }}</small>
          </span>
        </template>
      </div>
    </div>
  </div>
  <ContextMenu ref="menu" label="附件操作"/>
  <MaterialFileDialog v-if="promoting" :filename="promoting.filename" :save="promote" @close="promoting=undefined"/>
</template>

<style scoped>
.source-file { display:inline-flex; align-items:center; max-width:100%; min-width:0; }
.source-download { display:grid; place-items:center; flex:none; min-width:2rem; min-height:2rem; color:var(--accent); border-radius:50%; }
.source-download:is(:hover,:focus-visible) { background:var(--ui-hover); }
@media (pointer:coarse) { .source-download { min-width:44px; min-height:44px; } }
</style>
