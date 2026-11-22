<script setup lang="ts">
import { defineAsyncComponent, computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElOption, ElSelect, ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '../components/AppIcon.vue'
import ComposerToolbar from '../components/chat/ComposerToolbar.vue'
import ComposerInput from '../components/chat/ComposerInput.vue'
import ComposerAction from '../components/chat/ComposerAction.vue'
import EffortPicker from '../components/chat/EffortPicker.vue'
import ConversationTools from '../components/chat/ConversationTools.vue'
import { request, notifyError } from '../api/client'
import { useHistoryScroll } from '../composables/useHistoryScroll'
import { observeGeneration, submitAndObserve } from '../api/generations'
import type { ChatFile, ChatMessage, GenerationRequest, GenerationSnapshot } from '../types'
import MessageCopyMenu from '../components/chat/MessageCopyMenu.vue'
import EmbeddingNotice from '../components/chat/EmbeddingNotice.vue'
import ChatMarkdown from '../components/chat/ChatMarkdown.vue'
import ChatNotices from '../components/chat/ChatNotices.vue'
import ContextCompacted from '../components/chat/ContextCompacted.vue'
import MarkdownContent from '../components/MarkdownContent.vue'
import ToolActivity from '../components/chat/ToolActivity.vue'
import ChatSettings from '../components/chat/ChatSettings.vue'
import { chatPreviewURL } from '../utils/chatFileURL'
import { requestId } from '../utils/requestId'
import ChatFilesManager from '../components/chat/ChatFilesManager.vue'
import ChatIntent from '../components/collaboration/ChatIntent.vue'
import TaskEventCard from '../components/collaboration/TaskEventCard.vue'

const FilePreview = defineAsyncComponent(() => import('../components/FilePreview.vue'))
interface Conversation {id:string;title:string;created_at:string;message_count:number;memory_active:boolean;compression_count:number}
const route=useRoute(),router=useRouter(),conversations=ref<Conversation[]>([]),conversationId=ref(''),messages=ref<ChatMessage[]>([])
const pending=ref<GenerationSnapshot|null>(null),draft=ref(''),busy=ref(false),loading=ref(false),loadingOlder=ref(false),hasOlder=ref(false),uploading=ref(false),selectedFiles=ref<string[]>([]),error=ref(''),embedding=ref<boolean>()
const compressionThrough = computed(() => messages.value.at(-1)?.summary_through || 0)
const compactedAfter = computed(() => messages.value.filter(row => row.id <= compressionThrough.value).at(-1)?.id)
const pauseId = computed(() => pending.value?.state === 'awaiting_input' ? pending.value.agent_pause_id : undefined)
const completedFiles = computed(() => {
  const job = pending.value
  return job?.done && job.state === 'succeeded' && !job.error ? job.result?.generated_files || [] : []
})
const stopping = ref('')
const toolPending = ref(false), effortPending = ref(false)
const toolsSaving = computed(() => toolPending.value || effortPending.value)
const runningId = computed(() => ['queued','running'].includes(pending.value?.state || '') ? pending.value?.request_id : undefined)
async function stop() {
  if (!runningId.value) return
  const rid = runningId.value, id = conversationId.value
  if (stopping.value === rid) return
  stopping.value = rid
  try {
    const result = await request<GenerationSnapshot>('/chat/generations/'+encodeURIComponent(rid)+'/stop','POST')
    if (id !== conversationId.value || pending.value?.request_id !== rid) return
    generation?.abort()
    busy.value = false
    unconfirmed.delete(id)
    snapshot(result)
    await loadMessages(id, controller.signal)
    if (id === conversationId.value && pending.value?.request_id === rid && result.done) pending.value = null
  } catch (e) { notifyError(e) }
  finally { if (stopping.value === rid) stopping.value = '' }
}
const copyMenu=ref<InstanceType<typeof MessageCopyMenu>>()
const log=ref<HTMLElement>(),input=ref<{focus:()=>void}>(),preview=ref<{file:{id:string;filename:string};url:string}|null>(null)
const skillCatalog=ref<{id:string;name:string;description?:string;enabled:boolean|number}[]>([])
const current=computed(()=>conversations.value.find(row=>row.id===conversationId.value))
let controller=new AbortController(),generation:AbortController|undefined,sequence=0,destroyed=false
let olderRequest: AbortController | undefined
const {historyEvents, captureAnchor} = useHistoryScroll(log, older)
const unconfirmed = new Map<string, GenerationRequest>()
const resource='/conversations/'
const selectedURL=(id:string)=>resource+encodeURIComponent(id)
function previewAttachment(file: ChatFile) {
  const url = chatPreviewURL(file, conversationId.value)
  if (url) preview.value = {file, url}
}
function nearBottom() {
  const el = log.value
  return !loadingOlder.value && (!el || el.scrollHeight-el.scrollTop-el.clientHeight < 80)
}
async function scrollBottom(force=false) {
  const el = log.value, id = conversationId.value
  if (!force && !nearBottom()) return
  await nextTick()
  if (el && el===log.value && id===conversationId.value) el.scrollTop=el.scrollHeight
}
function snapshot(value:GenerationSnapshot){pending.value=value;busy.value=!value.done&&!value.error&&!['failed','cancelled','indeterminate','awaiting_input'].includes(value.state||'');void scrollBottom()}
async function loadMessages(id:string, signal:AbortSignal) {
  const rows = await request<ChatMessage[]>('/chat?'+new URLSearchParams({personal:'true',channel:'assistant',limit:'20',conversation_id:id}),'GET',undefined,signal)
  if (signal.aborted || id !== conversationId.value) return
  const tail = new Set(rows.map(row=>row.id))
  const first = rows[0]
  const retained = first && messages.value.some(row=>tail.has(row.id))
    ? messages.value.filter(row=>row.id<first.id) : []
  const follow = !messages.value.length || nearBottom()
  const restore = follow ? undefined : captureAnchor()
  messages.value = [...retained,...rows]
  if (!retained.length) hasOlder.value = rows.length===20
  await nextTick()
  if (signal.aborted || id!==conversationId.value) return
  if (follow) await scrollBottom(true)
  else restore?.()
}
async function observe(value:GenerationSnapshot,id:string){
  if(!value.request_id)return
  generation?.abort();const active=new AbortController();generation=active;snapshot(value)
  const current = () => !active.signal.aborted && id===conversationId.value
  try {
    const result = await observeGeneration(value.request_id, event=>{if (current()) snapshot(event)}, active.signal)
    if (!current()) return
    snapshot(result)
    await loadMessages(id, active.signal)
    if (current() && result.done) pending.value=null
  } catch (e) {
    if (current()) { error.value=e instanceof Error?e.message:'暂时无法查询生成状态'; busy.value=false }
  }
}
async function loadConversation(id:string){
  olderRequest?.abort();olderRequest=undefined;loadingOlder.value=false
  controller.abort();generation?.abort();controller=new AbortController();const active=controller,ticket=++sequence
  pending.value=null;busy.value=false;loading.value=true;error.value=''
  try {
    const latest = await request<GenerationSnapshot | null>('/chat/pending?conversation_id=' + encodeURIComponent(id), 'GET', undefined, active.signal)
    // A succeeded job has already persisted its messages. Read the history after its status.
    await loadMessages(id, active.signal)
    if (active.signal.aborted || ticket !== sequence) return
    if (latest?.state === 'awaiting_input' || latest?.state === 'cancelled') snapshot(latest)
    else if (latest && !latest.done && !latest.error) void observe(latest, id)
    else if (latest?.error) pending.value = latest
    embedding.value = (await request<{ embedding_configured: boolean }>('/me', 'GET', undefined, active.signal)).embedding_configured
  }
  catch(e){if(!active.signal.aborted)error.value=e instanceof Error?e.message:'无法读取会话'}
  finally{if(ticket===sequence)loading.value=false}
}
async function load(){
  try{
    const value=await request<{items:Conversation[];default_id:string}>('/conversations?channel=assistant&personal=true','GET',undefined,controller.signal)
    if(destroyed)return;conversations.value=value.items
    const requested=typeof route.query.conversation==='string'?route.query.conversation:''
    const id=value.items.some(row=>row.id===requested)?requested:value.items.some(row=>row.id===conversationId.value)?conversationId.value:value.default_id
    if(conversationId.value===id)await loadConversation(id);else conversationId.value=id
  }catch(e){if(!controller.signal.aborted)error.value=e instanceof Error?e.message:'无法读取会话'}
}
async function select(id:string){if(!id)return;conversationId.value=id;await router.replace({query:{...route.query,conversation:id}})}
async function newConversation(){try{const row=await request<{id:string}>('/conversations','POST',{channel:'assistant',title:'新会话'});await select(row.id);await load()}catch(e){notifyError(e)}}
async function rename(){if(!current.value)return;try{const {value}=await ElMessageBox.prompt('会话标题','重命名',{inputPlaceholder:'请输入会话标题',inputValue:current.value.title,inputValidator:text=>!!text.trim()&&text.length<=80||'标题应为 1–80 字符'});await request(selectedURL(conversationId.value),'PATCH',{title:value.trim()});current.value.title=value.trim()}catch(e){if(e!=='cancel'&&e!=='close')notifyError(e)}}
async function remove(clear:boolean){
  if(busy.value||uploading.value)return
  if(!confirm(clear?'清空当前会话聊天记录、记忆及附件？已转存资料与已学到的风格仍保留。':'删除当前会话、聊天、记忆及附件？已转存资料与已学到的风格仍保留。'))return
  try{await request(selectedURL(conversationId.value)+(clear?'/clear':'')+'?personal=true&channel=assistant',clear?'POST':'DELETE');selectedFiles.value=[];if(!clear)conversationId.value='';await load()}catch(e){notifyError(e)}
}
async function send(control: 'continue' | 'retry' = 'continue') {
  const pause = pauseId.value ? pending.value : null
  const id = conversationId.value
  const message = pause && control === 'retry' ? '重试' : draft.value.trim()
  if ((!message && !pause) || !id || busy.value || uploading.value || toolsSaving.value || loading.value) return
  if (/^\/compact(?:\s|$)/.test(message)) {
    busy.value = true
    try {
      const active = controller.signal
      const result = await request<{message:string;compressed?:boolean;compression_count?:number}>(selectedURL(id)+'/compact','POST',{focus:message.slice(8).trim()},active)
      if (id === conversationId.value) {
        if (draft.value.trim() === message) draft.value = ''
        if (current.value && result.compression_count) {
          current.value.memory_active = true
          current.value.compression_count = result.compression_count
        }
        if (result.compressed) {
          await loadMessages(id, active)
          if (!active.aborted && id === conversationId.value) await scrollBottom(true)
        } else ElMessage.info(result.message)
      }
    } catch (e) { if (id === conversationId.value) notifyError(e) }
    finally { if (id === conversationId.value) busy.value = false }
    return
  }
  const files = pause && control !== 'continue' ? [] : [...selectedFiles.value], previous = unconfirmed.get(id)
  const sameFiles = previous && previous.attachment_ids.length === files.length && files.every(file => previous.attachment_ids.includes(file))
  const command: GenerationRequest = previous && previous.message === message && sameFiles
    && previous.agent_pause_id === pause?.agent_pause_id && previous.agent_control === (pause ? control : undefined) ? previous : {
    request_id: requestId(), conversation_id: id, message, channel: 'assistant', personal: true, attachment_ids: files,
    ...(pause ? { agent_pause_id: pause.agent_pause_id, agent_control: control } : {}),
  }
  unconfirmed.set(id, command)
  generation?.abort()
  const active = new AbortController()
  generation = active
  const previousDraft = draft.value
  busy.value = true
  error.value = ''
  if (control === 'continue') draft.value = ''
  pending.value = { message, reply: '', status: '正在发送问题…', stage: 'sending', done: false }
  await scrollBottom(true)
  let received = false
  try {
    const value = await submitAndObserve(command, event=>{if (!active.signal.aborted && id===conversationId.value) snapshot(event)}, active.signal, () => {
      if (!active.signal.aborted && pending.value && id === conversationId.value) snapshot({...pending.value, request_id: command.request_id, state: 'queued', status: '已接收，等待执行'})
    })
    if (active.signal.aborted || id !== conversationId.value) return
    received = true
    unconfirmed.delete(id)
    snapshot(value)
    if (value.error && value.state !== 'indeterminate') draft.value = previousDraft
    if (pause && value.error) {
      const latest = await request<GenerationSnapshot | null>('/chat/pending?conversation_id=' + encodeURIComponent(id), 'GET', undefined, active.signal)
      if (!active.signal.aborted && id === conversationId.value && latest?.state === 'awaiting_input') {
        pending.value = latest
        error.value = value.error
      }
    }
    await loadMessages(id, active.signal)
    if (active.signal.aborted || id!==conversationId.value) return
    if (value.done && !pauseId.value) {
      pending.value = null
      const data = await request<{items: Conversation[]}>('/conversations?channel=assistant&personal=true', 'GET', undefined, active.signal)
      if (!active.signal.aborted) conversations.value = data.items
    }
  } catch (e) {
    if (!active.signal.aborted && id === conversationId.value) {
      if (received) error.value = '刷新会话记录失败：' + (e instanceof Error ? e.message : '请重新连接')
      else {
        error.value = e instanceof Error ? e.message : '提交结果尚未确认，请查看会话记录'
        if (pause) pending.value = pause
        else if (pending.value) pending.value.error = error.value
        draft.value = previousDraft
      }
    }
  } finally { if (generation === active && !active.signal.aborted) busy.value = false }
}
async function older() {
  const id=conversationId.value, first=messages.value[0]
  if (!id || !first || loading.value || olderRequest || !hasOlder.value) return
  const active = new AbortController()
  olderRequest = active
  loadingOlder.value = true
  try {
    const query = new URLSearchParams({personal:'true',channel:'assistant',limit:'20',conversation_id:id,before:String(first.id)})
    const rows = await request<ChatMessage[]>('/chat?'+query,'GET',undefined,active.signal)
    if (active.signal.aborted || id!==conversationId.value || messages.value[0]?.id!==first.id) return
    const earlier = rows.filter(row=>row.id<first.id)
    const restore = captureAnchor()
    messages.value = [...earlier,...messages.value]
    hasOlder.value = earlier.length>0 && rows.length===20
    await nextTick()
    if (!active.signal.aborted && id===conversationId.value) restore()
  } catch (error) {
    if (!active.signal.aborted && id===conversationId.value) notifyError(error)
  } finally {
    if (olderRequest===active) { olderRequest=undefined; loadingOlder.value=false }
  }
}
function invoke(name:string){draft.value='$'+JSON.stringify(name)+' '+draft.value;void nextTick(()=>input.value?.focus())}
const quickPrompts=['介绍一下你自己','有哪些分身','查询我的申请状态']
async function ask(text:string){draft.value=text;await send()}
function openSession(session:string,task?:string){void router.push({path:'/dialogues',query:{session,task}})}
function resume(){if(!document.hidden&&!destroyed&&!busy.value&&conversationId.value)void loadConversation(conversationId.value)}
watch(conversationId,id=>{
  messages.value=[];hasOlder.value=false;selectedFiles.value=[]
  if(id)void loadConversation(id)
})
watch(()=>route.query.conversation,value=>{if(typeof value==='string'&&value!==conversationId.value&&conversations.value.some(row=>row.id===value))conversationId.value=value})
watch(()=>route.query.skill,value=>{if(typeof value==='string'&&value)invoke(value)},{immediate:true})
onMounted(()=>{void load();window.addEventListener('online',resume);window.addEventListener('pageshow',resume);document.addEventListener('visibilitychange',resume)})
onUnmounted(()=>{destroyed=true;olderRequest?.abort();controller.abort();generation?.abort();window.removeEventListener('online',resume);window.removeEventListener('pageshow',resume);document.removeEventListener('visibilitychange',resume)})
</script>
<template>
  <section class="card chat-card" id="personal-chat-card">
    <header class="chat-header personal-chat-header">
      <h2>我的分身</h2>
      <div class="personal-chat-session">
        <ElSelect :model-value="conversationId" class="chat-session-select" aria-label="选择会话" @change="select">
          <ElOption v-for="item in conversations" :key="item.id" :value="item.id" :label="item.title"/>
        </ElSelect>
      </div>
      <ComposerToolbar class="chat-header-commands" label="会话命令">
        <button type="button" class="button chat-command" :disabled="busy||uploading" @click="newConversation"><span aria-hidden="true">＋</span>新建会话</button>
        <details class="composer-tool chat-command-menu">
          <summary class="chat-command chat-icon-command" aria-label="会话操作" title="会话操作"><AppIcon name="more" :size="18"/></summary>
          <div class="composer-popover chat-command-panel">
            <div id="personal-chat-style"/>
            <button type="button" class="chat-menu-item" :disabled="!conversationId||busy||uploading" @click="rename"><AppIcon name="rename" :size="16"/>重命名</button>
            <button type="button" class="chat-menu-item" :disabled="!conversationId||busy||uploading" @click="remove(true)"><AppIcon name="rebuild" :size="16"/>清空记录</button>
            <button type="button" class="chat-menu-item danger" :disabled="!conversationId||busy||uploading" @click="remove(false)"><AppIcon name="trash" :size="16"/>删除会话</button>
          </div>
        </details>
      </ComposerToolbar>
    </header>
    <p class="sub">{{ current?.message_count || 0 }} 条消息 · {{ current?.memory_active?'已启用压缩记忆':'较早对话按需压缩' }}<template v-if="embedding"> · 可检索本人公开及私密资料</template></p>
    <EmbeddingNotice :configured="embedding"/>
    <MessageCopyMenu :key="conversationId" ref="copyMenu"/>
    <p v-if="error" class="notice error" role="alert">{{ error }} <ElButton size="small" @click="load">重新连接</ElButton></p><p v-if="loading" role="status">正在读取会话…</p>

    <div ref="log" class="chatlog vue-chat-log" role="log" aria-label="对话消息" aria-live="polite" tabindex="0" :aria-busy="loadingOlder" v-on="historyEvents" @contextmenu="copyMenu?.open($event)" @keydown="copyMenu?.open($event)"><div v-if="!messages.length&&!loading" class="bubble" data-copy-message tabindex="0" aria-haspopup="menu">你好，我是你的个人分身。可直接提问、使用技能，或委托我与其他分身会话。</div>
      <ContextCompacted v-if="compressionThrough && compactedAfter === undefined"/>
      <template v-for="message in messages" :key="message.id" v-memo="[message, conversationId, message.id === compactedAfter]">
      <article :data-chat-message="message.id" :data-history-message="message.id" class="bubble" data-copy-message tabindex="0" aria-haspopup="menu" :class="{user:message.role==='user','intent-message':!!message.intent,'task-event-bubble':!!message.task_event?.kind}"><ToolActivity :items="message.tool_events||[]"><template v-if="message.used_skills?.length" #default><small class="stream-note">本轮技能：{{ message.used_skills.map(item=>item.name).join('、') }}</small></template></ToolActivity><small v-if="message.responder_name" class="reply-source">{{ message.responder_name }}</small><TaskEventCard v-if="message.task_event?.kind" :event="message.task_event" @opened="openSession"/><ChatIntent v-else-if="message.intent" :intent="message.intent" @updated="loadConversation(conversationId)" @opened="openSession"/><ChatMarkdown v-else :artifact-reply="message.role !== 'user'" :source="message.content" :events="message.tool_events" :attachments="message.attachments" :conversation-id="conversationId" @preview="previewAttachment"/><ChatNotices :items="message.tool_events"/></article>
      <ContextCompacted v-if="message.id === compactedAfter"/>
      </template>
      <template v-if="pending"><article v-if="pending.message && !pending.done && !pauseId && pending.result?.mode!=='agent_control'" class="bubble user pending-user" data-copy-message tabindex="0" aria-haspopup="menu"><MarkdownContent :source="pending.message||''"/></article><article class="bubble pending-answer" data-copy-message tabindex="0" aria-haspopup="menu" :class="{incomplete:!!pending.error && !pauseId}"><ToolActivity :items="pending.tool_events||[]" :awaiting-input="!!pauseId" :model-progress="pending.stage === 'model_progress'" :pending="busy ? pending.status || '准备回答' : undefined">
        <template v-if="pending.usage || pending.target || pending.used_skills?.length" #default>
          <small v-if="pending.target" class="stream-note">回答分身：{{ pending.target.name }}</small>
          <small v-if="pending.used_skills?.length" class="stream-note">本轮技能：{{ pending.used_skills.map(item=>item.name).join('、') }}</small>
          <small v-if="pending.usage" class="stream-note">本次用量：输入 {{ pending.usage.input_tokens ?? '未报告' }} / 输出 {{ pending.usage.output_tokens ?? '未报告' }} tokens</small>
        </template></ToolActivity><ChatMarkdown v-if="pending.reply || (!pauseId && completedFiles.length)" :source="pending.reply" :attachments="pauseId ? [] : completedFiles" @preview="previewAttachment" :events="pending.tool_events" :diagrams="pending.mermaid_enabled!==false" :conversation-id="conversationId"/><ChatNotices :items="pending.tool_events" :message="pauseId ? pending.status : pending.error" :tone="!pauseId && pending.error?'error':'info'"/><div v-if="pauseId && pending.stream_retryable" class="chat-retry-actions">
          <button type="button" class="button small primary" :disabled="busy||loading||uploading||toolsSaving" @click="send('retry')">重试</button>
        </div></article></template>
    </div>
    <form id="personal-chat-compose" data-guide-target="chat" class="personal-compose" @submit.prevent="send()">
      <ComposerInput ref="input" v-model="draft" :catalog="skillCatalog" :session="conversationId" :usage-url="conversationId ? selectedURL(conversationId)+'/compact' : ''" :usage-revision="(messages.at(-1)?.id || 0)+'/'+compressionThrough" label="聊天消息" :placeholder="pauseId ? '补充任务所需信息…' : '向自己的分身提问，或输入 $ 选择技能…'" :disabled="busy||loading||!conversationId" submit-on-enter @submit="send()"/>
    </form>
    <div class="chat-compose-actions">
      <ComposerToolbar class="personal-chat-toolbar">
        <template v-if="conversationId">
          <details class="chat-quick composer-tool">
            <summary title="常用提问" aria-label="常用提问">常用提问</summary>
            <div class="composer-popover quick chat-command-panel"><button v-for="text in quickPrompts" :key="text" type="button" class="chat-menu-item" :disabled="busy||uploading" @click="ask(text)">{{ text }}</button></div>
          </details>
          <ChatSettings :conversation-id="conversationId" :busy="busy||uploading" :revision="messages.length" @catalog="skillCatalog=$event"/>
          <ChatFilesManager :conversation-id="conversationId" :busy="busy" @selected="value=>selectedFiles=value" @uploading="value=>uploading=value"/>
          <ConversationTools :conversation-id="conversationId" :busy="busy||uploading" @saving="toolPending=$event"/>
        </template>
        <div class="composer-primary-actions">
          <EffortPicker :conversation-id="conversationId" :busy="busy||uploading" @saving="effortPending=$event"/>
          <ComposerAction v-if="runningId" label="暂停任务" stop :disabled="!!stopping || loading" @click="stop"/>
          <ComposerAction v-else type="submit" form="personal-chat-compose" :resume="!!pauseId" :label="busy?'回答中…':pauseId?(draft.trim()?'补充并继续':'继续任务'):'发送'" :disabled="busy||uploading||toolsSaving||loading||!conversationId||(!pauseId&&!draft.trim())"/>
        </div>
      </ComposerToolbar>
    </div>
  </section><FilePreview v-if="preview" :file="preview.file" :url="preview.url" @close="preview=null"/>
</template>

<style scoped>
.chat-retry-actions{display:flex;gap:.5rem;margin-top:.5rem}
</style>
