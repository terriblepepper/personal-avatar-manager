<script setup lang="ts">
import { useConfirmation } from '../composables/useConfirmation'
import { defineAsyncComponent, computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElOption, ElSelect, ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '../components/AppIcon.vue'
import ComposerToolbar from '../components/chat/ComposerToolbar.vue'
import ComposerInput from '../components/chat/ComposerInput.vue'
import ComposerAction from '../components/chat/ComposerAction.vue'
import { chatTextFits } from '../utils/chatTextBudget'
import AgentQuestionPrompt from '../components/chat/AgentQuestionPrompt.vue'
import EffortPicker from '../components/chat/EffortPicker.vue'
import ConversationTools from '../components/chat/ConversationTools.vue'
import { request, notifyError } from '../api/client'
import { useChatHistory } from '../composables/useChatHistory'
import { observeGeneration, submitAndObserve } from '../api/generations'
import type { ChatFile, GenerationRequest, GenerationSnapshot, QuestionReply } from '../types'
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
import HistoryMessage from '../components/chat/HistoryMessage.vue'

const confirmAction = useConfirmation()
const FilePreview = defineAsyncComponent(() => import('../components/FilePreview.vue'))
interface Conversation {id:string;title:string;created_at:string;message_count:number;memory_active:boolean;compression_count:number}
const route=useRoute(),router=useRouter(),conversations=ref<Conversation[]>([]),conversationId=ref('')
const pending = ref < GenerationSnapshot | null > (null),
  draft = ref(''),
  busy = ref(false),
  loading = ref(false),
  uploading = ref(false),
  selectedFiles = ref < string[] > ([]),
  error = ref(''),
  embedding = ref < boolean > ()
const history = useChatHistory(conversationId, loading)
const { messages, log, loadingOlder, historyEvents, scrollBottom } = history
const compressionThrough = computed(() => messages.value.at(-1)?.summary_through || 0)
const compactedAfter = computed(() => messages.value.filter(row => row.id <= compressionThrough.value).at(-1)?.id)
const pauseId = computed(() => pending.value?.state === 'awaiting_input' ? pending.value.agent_pause_id : undefined)
const questionPrompt = ref<InstanceType<typeof AgentQuestionPrompt>>()
const userQuestion = computed(() => pauseId.value && (!pending.value?.conversation_id || pending.value.conversation_id === conversationId.value)
  ? pending.value?.user_question : undefined)
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
    await loadMessages(id, controller.signal, result.done ? rid : undefined)
  } catch (e) { notifyError(e) }
  finally { if (stopping.value === rid) stopping.value = '' }
}
const copyMenu=ref<InstanceType<typeof MessageCopyMenu>>()
const preview=ref<{file:{id:string;filename:string};url:string}|null>(null)
const current=computed(()=>conversations.value.find(row=>row.id===conversationId.value))
let controller=new AbortController(),generation:AbortController|undefined,sequence=0,destroyed=false
const unconfirmed = new Map<string, GenerationRequest>()
const resource='/conversations/'
const selectedURL=(id:string)=>resource+encodeURIComponent(id)
function previewAttachment(file: ChatFile) {
  const url = chatPreviewURL(file, conversationId.value)
  if (url) preview.value = {file, url}
}
let snapshotFrame: number | undefined
let paintSnapshot: (() => void) | undefined
function clearSnapshotFrame() {
  if (snapshotFrame !== undefined) cancelAnimationFrame(snapshotFrame)
  snapshotFrame = undefined
  paintSnapshot = undefined
}
function queueSnapshot(value: GenerationSnapshot, signal: AbortSignal, id: string) {
  if (signal.aborted || id !== conversationId.value) return
  if (value.done || value.error || value.state === 'awaiting_input') { snapshot(value); return }
  paintSnapshot = () => { if (!signal.aborted && id === conversationId.value) snapshot(value) }
  snapshotFrame ??= requestAnimationFrame(() => {
    snapshotFrame = undefined
    paintSnapshot?.()
  })
}
function snapshot(value: GenerationSnapshot) {
  clearSnapshotFrame()
  pending.value = value
  busy.value = !value.done && !value.error
    && !['failed', 'cancelled', 'indeterminate', 'awaiting_input'].includes(value.state || '')
  void scrollBottom()
}
async function loadMessages(id:string, signal:AbortSignal, completedRequest?:string) {
  const completed = completedRequest && pending.value?.done && !pauseId.value &&
    (!pending.value.request_id || pending.value.request_id === completedRequest) ? pending.value : undefined
  await history.load(id, signal, () => {
    if (completed && pending.value === completed) pending.value = null
  })
}
async function observe(value:GenerationSnapshot,id:string){
  if(!value.request_id)return
  generation?.abort();const active=new AbortController();generation=active;snapshot(value)
  const current = () => !active.signal.aborted && id===conversationId.value
  try {
    const result = await observeGeneration(value.request_id, event => queueSnapshot(event, active.signal, id), active.signal)
    if (!current()) return
    snapshot(result)
    await loadMessages(id, active.signal, result.done ? value.request_id : undefined)
  } catch (e) {
    if (current()) { error.value=e instanceof Error?e.message:'暂时无法查询生成状态'; busy.value=false }
  }
}
async function loadConversation(id: string) {
  clearSnapshotFrame()
  history.cancelOlder()
  controller.abort();
  generation?.abort();
  controller = new AbortController();
  const active = controller,
    ticket = ++sequence
  pending.value = null;
  busy.value = false;
  loading.value = true;
  error.value = ''
  try {
    const latest = await request < GenerationSnapshot | null > ('/chat/pending?conversation_id=' + encodeURIComponent(
      id), 'GET', undefined, active.signal)
    // A succeeded job has already persisted its messages. Read the history after its status.
    await loadMessages(id, active.signal)
    if (active.signal.aborted || ticket !== sequence) return
    if (latest?.state === 'awaiting_input' || latest?.state === 'cancelled') snapshot(latest)
    else if (latest && !latest.done && !latest.error) void observe(latest, id)
    else if (latest?.error) pending.value = latest
  } catch (e) {
    if (!active.signal.aborted) error.value = e instanceof Error ? e.message : '无法读取会话'
  } finally {
    if (ticket === sequence) loading.value = false
  }
}
async function load() {
  try {
    const value = await request < {
      items: Conversation[];default_id: string;embedding_configured: boolean
    } > ('/conversations?channel=assistant&personal=true', 'GET', undefined, controller.signal)
    if (destroyed) return;
    conversations.value = value.items;
    embedding.value = value.embedding_configured
    const requested = typeof route.query.conversation === 'string' ? route.query.conversation : ''
    const id = value.items.some(row => row.id === requested) ? requested : value.items.some(row => row.id ===
      conversationId.value) ? conversationId.value : value.default_id
    if (conversationId.value === id) await loadConversation(id);
    else conversationId.value = id
  } catch (e) {
    if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : '无法读取会话'
  }
}
async function select(id:string){if(!id)return;conversationId.value=id;await router.replace({query:{...route.query,conversation:id}})}
async function newConversation() {
  try {
    const row = await request < {
      id: string
    } > ('/conversations', 'POST', {
      channel: 'assistant',
      title: '新会话'
    });
    await select(row.id);
    await load()
  } catch (e) {
    notifyError(e)
  }
}
async function rename() {
  if (!current.value) return;
  try {
    const {
      value
    } = await ElMessageBox.prompt('会话标题', '重命名', {
      inputPlaceholder: '请输入会话标题',
      inputValue: current.value.title,
      inputValidator: text => !!text.trim() && text.length <= 80 || '标题应为 1–80 字符'
    });
    await request(selectedURL(conversationId.value), 'PATCH', {
      title: value.trim()
    });
    current.value.title = value.trim()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') notifyError(e)
  }
}
async function remove(clear: boolean) {
  if (busy.value || uploading.value) return
  const target = conversationId.value
  await confirmAction(clear ? '清空当前会话聊天记录、记忆及附件？已转存资料与已学到的风格仍保留。' : '删除当前会话、聊天、记忆及附件？已转存资料与已学到的风格仍保留。', clear ? '确认清空会话' : '确认删除会话',
    () => [conversationId.value, current.value?.id, busy.value, uploading.value], async () => {
    await request(selectedURL(target) + (clear ? '/clear' : '') + '?personal=true&channel=assistant', clear ? 'POST' : 'DELETE')
    if (conversationId.value !== target) return
    selectedFiles.value = []
    if (!clear) conversationId.value = ''
    await load()
  })
}
async function send(control: NonNullable<GenerationRequest['agent_control']> = 'continue', answer?: QuestionReply) {
  const pause = pauseId.value ? pending.value : null
  const id = conversationId.value
  const question = userQuestion.value
  if (answer && (!pause || question?.id !== answer.question_id)) return
  if (question && !answer && control === 'continue' && !draft.value.trim()) { questionPrompt.value?.open(); return }
  const actionText = { continue: answer?.message ?? draft.value.trim(), retry: '重试', end: '结束任务' }
  const message = pause ? actionText[control] : draft.value.trim()
  if ((!message && !pause) || !id || busy.value || uploading.value || toolsSaving.value || loading.value) return
  if ((!pause || control === 'continue') && !chatTextFits(answer?.message ?? draft.value)) {
    ElMessage.warning('消息超过 64 KiB（UTF-8），请缩短后发送。')
    return
  }
  if (!answer && /^\/compact(?:\s|$)/.test(message)) {
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
  const files = answer || (pause && control !== 'continue') ? [] : [...selectedFiles.value], previous = unconfirmed.get(id)
  const sameFiles = previous && previous.attachment_ids.length === files.length && files.every(file => previous.attachment_ids.includes(file))
  const questionId = control === 'continue' ? question?.id : undefined
  const command: GenerationRequest = previous && previous.message === message && sameFiles && previous.question_id === questionId
    && previous.agent_pause_id === pause?.agent_pause_id && previous.agent_control === (pause ? control : undefined) ? previous : {
    request_id: answer?.request_id || requestId(), conversation_id: id, message, channel: 'assistant', personal: true, attachment_ids: files,
    ...(pause ? { agent_pause_id: pause.agent_pause_id, agent_control: control } : {}),
    ...(questionId ? {question_id:questionId} : {}),
  }
  unconfirmed.set(id, command)
  generation?.abort()
  const active = new AbortController()
  generation = active
  const previousDraft = draft.value
  busy.value = true
  error.value = ''
  if (!answer && control === 'continue') draft.value = ''
  pending.value = { message, reply: '', status: '正在发送问题…', stage: 'sending', done: false }
  await scrollBottom(true)
  let received = false
  try {
    const value = await submitAndObserve(command, event => queueSnapshot(event, active.signal, id), active.signal, () => {
      if (!active.signal.aborted && pending.value && id === conversationId.value) snapshot({...pending.value, request_id: command.request_id, state: 'queued', status: '已接收，等待执行'})
    })
    if (active.signal.aborted || id !== conversationId.value) return
    received = true
    unconfirmed.delete(id)
    snapshot(value)
    if (!answer && value.error && value.state !== 'indeterminate') draft.value = previousDraft
    if (pause && value.error) {
      const latest = await request<GenerationSnapshot | null>('/chat/pending?conversation_id=' + encodeURIComponent(id), 'GET', undefined, active.signal)
      if (!active.signal.aborted && id === conversationId.value && latest?.state === 'awaiting_input') {
        pending.value = latest
        error.value = value.error
      }
    }
    await loadMessages(id, active.signal, value.done ? command.request_id : undefined)
    if (active.signal.aborted || id!==conversationId.value) return
    if (value.done && !pauseId.value) {
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
        if (!answer) draft.value = previousDraft
      }
    }
  } finally { if (generation === active && !active.signal.aborted) busy.value = false }
}
const quickPrompts=['介绍一下你自己','有哪些分身','查询我的申请状态']
async function ask(text:string){draft.value=text;await send()}
function openSession(session:string,task?:string){void router.push({path:'/dialogues',query:{session,task}})}
function resume(){if(!document.hidden&&!destroyed&&!busy.value&&conversationId.value)void loadConversation(conversationId.value)}
watch(conversationId,id=>{
  history.reset();selectedFiles.value=[]
  if(id)void loadConversation(id)
})
watch(() => route.query.conversation, value => {
  if (typeof value === 'string' && value !== conversationId.value && conversations.value.some(row => row.id ===
      value)) conversationId.value = value
})
onMounted(() => {
  void load();
  window.addEventListener('online', resume);
  window.addEventListener('pageshow', resume);
  document.addEventListener('visibilitychange', resume)
})
onUnmounted(() => {
  clearSnapshotFrame();
  destroyed = true;
  controller.abort();
  generation?.abort();
  window.removeEventListener('online', resume);
  window.removeEventListener('pageshow', resume);
  document.removeEventListener('visibilitychange', resume)
})
</script>
<template>
  <section class="card chat-card" id="personal-chat-card">
    <header class="chat-header personal-chat-header">
      <h2>我的分身</h2>
      <div class="personal-chat-session">
        <ElSelect :model-value="conversationId" class="chat-session-select" aria-label="选择会话" @change="select">
          <ElOption v-for="item in conversations" :key="item.id" :value="item.id" :label="item.title" />
        </ElSelect>
      </div>
      <ComposerToolbar class="chat-header-commands" label="会话命令">
        <button type="button" class="button chat-command" :disabled="busy||uploading" @click="newConversation">
          <span aria-hidden="true">＋</span>新建会话
        </button>
        <details class="composer-tool chat-command-menu">
          <summary class="chat-command chat-icon-command" aria-label="会话操作" title="会话操作">
            <AppIcon name="more" :size="18" />
          </summary>
          <div class="composer-popover chat-command-panel">
            <div id="personal-chat-style" />
            <button type="button" class="chat-menu-item" :disabled="!conversationId||busy||uploading" @click="rename">
              <AppIcon name="rename" :size="16" />重命名
            </button>
            <button type="button" class="chat-menu-item" :disabled="!conversationId||busy||uploading" @click="remove(true)">
              <AppIcon name="rebuild" :size="16" />清空记录
            </button>
            <button type="button" class="chat-menu-item danger" :disabled="!conversationId||busy||uploading"
              @click="remove(false)">
              <AppIcon name="trash" :size="16" />删除会话
            </button>
          </div>
        </details>
      </ComposerToolbar>
    </header>
    <p class="sub">{{ current?.message_count || 0 }} 条消息<template v-if="embedding"> ·
        {{ current?.memory_active?'已启用压缩记忆':'较早对话按需压缩' }} · 可检索本人公开及私密资料</template>
    </p>
    <EmbeddingNotice :configured="embedding" />
    <MessageCopyMenu :key="conversationId" ref="copyMenu" />
    <p v-if="error" class="notice error" role="alert">{{ error }}
      <ElButton size="small" @click="load">重新连接</ElButton>
    </p>
    <p v-if="loading" role="status">正在读取会话…</p>

    <div ref="log" class="chatlog vue-chat-log" role="log" aria-label="对话消息" aria-live="polite" tabindex="0"
      :aria-busy="loadingOlder" v-on="historyEvents" @contextmenu="copyMenu?.open($event)" @keydown="copyMenu?.open($event)">
      <div v-if="!messages.length&&!loading" class="bubble" data-copy-message tabindex="0" aria-haspopup="menu">
        你好，我是你的个人分身。可直接提问、使用技能，或委托我与其他分身会话。</div>
      <ContextCompacted v-if="compressionThrough && compactedAfter === undefined" />
      <template v-for="message in messages" :key="message.id"
        v-memo="[message, conversationId, message.id === compactedAfter]">
        <HistoryMessage :message="message" :conversation-id="conversationId" @preview="previewAttachment"
          @opened="openSession" @updated="loadConversation(conversationId)" />
        <ContextCompacted v-if="message.id === compactedAfter" />
      </template>
      <template v-if="pending">
        <article v-if="pending.message && !pending.done && !pauseId && pending.result?.mode!=='agent_control'"
          class="bubble user pending-user" data-copy-message tabindex="0" aria-haspopup="menu">
          <MarkdownContent :source="pending.message||''" />
        </article>
        <article class="bubble pending-answer" data-copy-message tabindex="0" aria-haspopup="menu"
          :class="{incomplete:!!pending.error && !pauseId}">
          <ToolActivity :items="pending.tool_events||[]" :awaiting-input="!!pauseId"
            :model-progress="pending.stage === 'model_progress'" :pending="busy ? pending.status || '准备回答' : undefined">
            <template v-if="pending.usage || pending.target || pending.used_skills?.length" #default>
              <small v-if="pending.target" class="stream-note">回答分身：{{ pending.target.name }}</small>
              <small v-if="pending.used_skills?.length" class="stream-note">
                本轮技能：{{ pending.used_skills.map(item=>item.name).join('、') }}</small>
              <small v-if="pending.usage" class="stream-note">本次用量：输入 {{ pending.usage.input_tokens ?? '未报告' }} / 输出
                {{ pending.usage.output_tokens ?? '未报告' }} tokens</small>
            </template>
          </ToolActivity>
          <ChatMarkdown :streaming="busy" v-if="pending.reply || (!pauseId && completedFiles.length)" :source="pending.reply"
            :attachments="pauseId ? [] : completedFiles" @preview="previewAttachment" :events="pending.tool_events"
            :diagrams="pending.mermaid_enabled!==false" :conversation-id="conversationId" />
          <ChatNotices :items="pending.tool_events" :message="pauseId ? pending.status : pending.error"
            :tone="!pauseId && pending.error?'error':'info'" />
          <div v-if="pauseId" class="chat-retry-actions">
            <button v-if="pending.stream_retryable" type="button" class="button small primary"
              :disabled="busy||loading||uploading||toolsSaving" @click="send('retry')">重试</button>
            <button type="button" class="button small" :disabled="busy||loading||uploading||toolsSaving"
              @click="send('end')">结束任务</button>
          </div>
        </article>
      </template>
    </div>
    <AgentQuestionPrompt ref="questionPrompt" :key="conversationId" :scope="'chat:'+conversationId" :question="userQuestion"
      :disabled="busy || uploading || toolsSaving || loading" @answer="answer => send('continue', answer)" />
    <form id="personal-chat-compose" data-guide-target="chat" class="personal-compose" @submit.prevent="send()">
      <ComposerInput v-model="draft" :usage-url="conversationId ? selectedURL(conversationId)+'/compact' : ''"
        :usage-revision="(messages.at(-1)?.id || 0)+'/'+compressionThrough" label="聊天消息"
        :placeholder="pauseId ? (pending?.stream_retryable ? '可直接重试，也可补充要求后继续…' : '补充任务所需信息…') : '向自己的分身提问…'"
        :disabled="busy||loading||!conversationId" submit-on-enter @submit="send()" />
    </form>
    <div class="chat-compose-actions">
      <ComposerToolbar class="personal-chat-toolbar">
        <template v-if="conversationId">
          <details class="chat-quick composer-tool">
            <summary title="常用提问" aria-label="常用提问">常用提问</summary>
            <div class="composer-popover quick chat-command-panel">
              <button v-for="text in quickPrompts" :key="text" type="button" class="chat-menu-item"
                :disabled="busy||uploading" @click="ask(text)">{{ text }}</button>
            </div>
          </details>
          <ChatSettings v-if="embedding !== false" :conversation-id="conversationId" :busy="busy||uploading"
            :revision="messages.length" />
          <ChatFilesManager :conversation-id="conversationId" :busy="busy" @selected="value=>selectedFiles=value"
            @uploading="value=>uploading=value" />
          <ConversationTools :conversation-id="conversationId" :busy="busy||uploading" @saving="toolPending=$event" />
        </template>
        <div class="composer-primary-actions">
          <EffortPicker :conversation-id="conversationId" :busy="busy||uploading" @saving="effortPending=$event" />
          <ComposerAction v-if="runningId" label="暂停任务" stop :disabled="!!stopping || loading" @click="stop" />
          <ComposerAction v-else type="submit" form="personal-chat-compose" :resume="!!pauseId"
            :label="busy?'回答中…':pauseId?(draft.trim()?'补充并继续':'继续任务'):'发送'"
            :disabled="busy||uploading||toolsSaving||loading||!conversationId||(!pauseId&&!draft.trim())" />
        </div>
      </ComposerToolbar>
    </div>
  </section>
  <FilePreview v-if="preview" :file="preview.file" :url="preview.url" @close="preview=null" />
</template>

<style scoped>
.chat-retry-actions{display:flex;gap:.5rem;margin-top:.5rem}
</style>
