<script setup lang="ts">
import { useConfirmation } from '../composables/useConfirmation'
import { defineAsyncComponent, computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElMessage } from 'element-plus'
import { request, notifyError } from '../api/client'
import { useDialogueSession } from '../composables/useDialogueSession'
import { useDialogueTasks } from '../composables/useDialogueTasks'
import { dialogueTranscript } from '../utils/dialogueTranscript'
import type { Session } from '../composables/dialogueTypes'
import ToolActivity from '../components/chat/ToolActivity.vue'
import MessageCopyMenu from '../components/chat/MessageCopyMenu.vue'
import EmbeddingNotice from '../components/chat/EmbeddingNotice.vue'
import ChatMarkdown from '../components/chat/ChatMarkdown.vue'
import ChatNotices from '../components/chat/ChatNotices.vue'
import ContextCompacted from '../components/chat/ContextCompacted.vue'
import { date } from '../utils/format'
import type { ChatFile, QuestionReply } from '../types'
import MarkdownContent from '../components/MarkdownContent.vue'
import ComposerToolbar from '../components/chat/ComposerToolbar.vue'
import ComposerInput from '../components/chat/ComposerInput.vue'
import ChatFilesManager from '../components/chat/ChatFilesManager.vue'
import ComposerAction from '../components/chat/ComposerAction.vue'
import { chatTextFits } from '../utils/chatTextBudget'
import AgentQuestionPrompt from '../components/chat/AgentQuestionPrompt.vue'
import EffortPicker from '../components/chat/EffortPicker.vue'
import ConversationTools from '../components/chat/ConversationTools.vue'
import DialogueTaskSidebar from '../components/collaboration/DialogueTaskSidebar.vue'
import AppIcon from '../components/AppIcon.vue'
import RecordPagination from '../components/RecordPagination.vue'
import WindowClose from '../components/WindowClose.vue'
import WindowControls from '../components/WindowControls.vue'
import { requestId } from '../utils/requestId'

const confirmAction = useConfirmation()
const FilePreview = defineAsyncComponent(() => import('../components/FilePreview.vue'))
const IntentDialog = defineAsyncComponent(() => import('../components/collaboration/IntentDialog.vue'))
const copyMenu=ref<InstanceType<typeof MessageCopyMenu>>()
const route=useRoute(), router=useRouter()
const message = ref('')
const writeState = ref<'idle' | 'sending' | 'working'>('idle')
const busy = computed(() => writeState.value !== 'idle')
const resourceBusy=ref(false), taskMenu=ref(false)
const filePreview=ref<ChatFile|null>(null)
const transcript=ref<HTMLElement>()
const {
  sessions,current,taskId,detail,search,offset,total,taskMap,taskSearch,taskOffset,taskTotal,
  switching,shownTask,allMessages,historyEvents,resetHistory,cancelHistory,polling,
} = useDialogueSession(transcript, () => { filePreview.value = null }, closeSession)

const toolPending = ref(false), effortPending = ref(false)
const toolsSaving = computed(() => toolPending.value || effortPending.value)
const commandsDisabled = computed(() => busy.value || switching.value || resourceBusy.value || !detail.value)
const taskClosed = computed(() => !!taskId.value && detail.value?.task?.status === 'ended')
const sendDisabled = computed(() => commandsDisabled.value || toolsSaving.value || taskClosed.value || (!message.value
  .trim() && !pausedTask.value) || (!/^\/compact(?:\s|$)/.test(message.value.trim()) && (detail.value?.task
  ?.can_supplement === false ||
  detail.value?.task?.status === 'awaiting_acceptance' || (!!detail.value?.task?.needs_confirmation && !
    userQuestion.value))))
const activeTask=computed(()=>detail.value?.task || detail.value?.chat)
const runningTask = computed(() => activeTask.value?.status === 'running' ? activeTask.value : undefined)
const activeStage = computed(() => {
  if (!runningTask.value) return ''
  if (runningTask.value.sending_prepared) return '正在发送'
  const stage = runningTask.value.tool_activity?.at(-1)
  return stage?.status === 'running' ? stage.steps?.at(-1)?.message || stage.name : stage?.name || '准备回答'
})
const pausedTask = computed(() => activeTask.value && (activeTask.value.status === 'paused' || (activeTask.value
  .status === 'awaiting_input' && activeTask.value.can_supplement)) ? activeTask.value : undefined)
const questionPrompt = ref<InstanceType<typeof AgentQuestionPrompt>>()
const userQuestion = computed(() => !switching.value && pausedTask.value?.can_supplement === true ? pausedTask.value.user_question : undefined)
const transport=computed(()=>detail.value?.current_transport_mode==='acps'?'ACPS'
  :detail.value?.current_transport_mode==='internal'?'平台内部传输':'')
const progress = computed(() => {
  const task = activeTask.value
  if (!task) return {
    label: '主会话',
    tone: 'neutral',
    note: ''
  }
  if (task.rejected) return {
    label: '任务已拒绝',
    tone: 'error',
    note: ''
  }
  if (task.auto_retrying || task.status === 'awaiting_identity') return {
    label: '正在自动重试',
    tone: 'waiting',
    note: task.error || task.waiting_reason || ''
  }
  if (task.can_retry) return {
    label: '重试已超时',
    tone: 'error',
    note: task.error || ''
  }
  if (task.status === 'awaiting_input' && task.waiting_label) return {
    label: task.waiting_label,
    tone: 'waiting',
    note: task.waiting_reason || ''
  }
  if (task.status === 'awaiting_input' && typeof task.can_supplement === 'boolean') return {
    label: task.can_supplement ? '等待本人补充信息' : '等待对方补充信息',
    tone: 'waiting',
    note: task.waiting_reason || ''
  }
  if (task.status === 'awaiting_input') return {
    label: task.needs_confirmation ? '等待本人确认' : task.waiting_for_other ? '等待对方确认' : '等待用户',
    tone: 'waiting',
    note: task.waiting_reason || ''
  }
  if (task.status === 'ended') return {
    label: task.message_delivered ? '已回复' : '任务已结束',
    tone: 'success',
    note: task.end_reason || task.reason || ''
  }
  if (task.status === 'running' && task.sending_prepared) return {
    label: '正在发送',
    tone: 'active',
    note: ''
  }
  if (task.status === 'running') return {
    label: (task.active_speaker || '分身') + '正在回答',
    tone: 'active',
    note: ''
  }
  return {
    label: taskStatuses[task.status] || task.status,
    tone: task.status === 'failed' ? 'error' : 'neutral',
    note: task.error || task.waiting_reason || ''
  }
})
const taskStatuses: Record < string, string > = {
  queued: '等待处理',
  running: '进行中',
  awaiting_acceptance: '等待接收',
  awaiting_input: '等待输入',
  paused: '已暂停',
  ended: '已结束',
  failed: '失败'
}
function selectTask(value: string) {
  if (!busy.value) enterSession(current.value, value)
}
function taskPage(value: number) { taskOffset.value=value; void polling.restart() }
const windowPanel = ref<HTMLElement>()
const openFullscreen = ref(false)
let sessionTrigger: HTMLElement | undefined
const resourceId = computed(() => detail.value?.resource_conversation_id || '')
const sharedFilesRevision = computed(() => {
  const view=detail.value
  const tasks=view?.task ? [view.task] : [view?.chat,...(view?.message_tasks || [])]
  return JSON.stringify([view?.task?.expires_at, [...new Set(tasks.flatMap(task => task?.files?.map(file => file.id) || []))].sort()])
})
const settingsId = computed(() => detail.value?.settings_conversation_id ?? resourceId.value)
const fileIds = ref<string[]>([])
const { rounds, intent, drafting, saveRounds, toggleAutoAccept, retry, draft, resetDraft } =
  useDialogueTasks({ current, detail, switching, message, action, scope: () => scope })
let scope = 0, pending = ''
const id=()=>requestId()
const compressionThrough = computed(() => detail.value?.summary_through || 0)
const compactedAfter = computed(() => allMessages.value.filter(row => row.turn <= compressionThrough.value).at(-1)?.turn)
const transcriptRows = computed(() => dialogueTranscript(allMessages.value, taskMap.value))
async function action(path: string, method: string, body: unknown, onSuccess: (result: {
  message: string;compressed ? : boolean
}) => void, state: 'sending' | 'working' = 'working') {
  if (busy.value || switching.value) return
  const ticket = scope
  writeState.value = state
  try {
    const result = await request < {
      message: string;compressed ? : boolean
    } > (path, method, body)
    if (ticket !== scope) return
    onSuccess(result)
    if (ticket === scope) await polling.refresh()
  } catch (error) {
    if (ticket === scope) notifyError(error)
  } finally {
    if (ticket === scope) writeState.value = 'idle'
  }
}
function sessionSummary(session: Session) {
  return session.messages?.filter(message => message.speaker_role === 'agent').at(-1)?.content.replace(/\s+/g, ' ').trim()
}
function finishMessage(requestId:string) {
  if (pending === requestId) { message.value=''; pending='' }
}
function send(answer?: QuestionReply) {
  if (sendDisabled.value || !current.value) return
  const question = userQuestion.value
  if (answer && question?.id !== answer.question_id) return
  if (!chatTextFits(answer?.message ?? message.value)) {
    ElMessage.warning('消息超过 64 KiB（UTF-8），请缩短后发送。')
    return
  }
  if (question && !answer && !message.value.trim()) { questionPrompt.value?.open(); return }
  if (!answer && /^\/compact(?:\s|$)/.test(message.value.trim())) {
    const submitted = message.value
    const query = taskId.value ? '?task_id='+encodeURIComponent(taskId.value) : ''
    return action('/agent-sessions/'+encodeURIComponent(current.value)+'/compact'+query,'POST',{focus:submitted.trim().slice(8).trim()}, result => {
      if (message.value === submitted) message.value=''
      if (!result.compressed) ElMessage.info(result.message)
      else if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
    })
  }
  const steering = !answer && !!runningTask.value
  const token = answer?.request_id || (pending ||= id())
  const target = pausedTask.value?.id || runningTask.value?.id || taskId.value
  const path = target ? '/agent-dialogues/'+encodeURIComponent(target)+'/resume'
    : '/agent-sessions/'+encodeURIComponent(current.value)+'/messages'
  return action(path,'POST',{message:answer?.message ?? message.value,file_ids:answer || steering ? [] : fileIds.value,request_id:token,
    ...(steering ? {steer:true} : {}),
    ...(question ? {question_id:question.id} : {}),
    ...(pausedTask.value ? {expected_revision:pausedTask.value.revision} : target ? {} : {new_task:true,delivery_mode:'message'})}, () => {
    if (!answer) finishMessage(token)
    if (steering) ElMessage.success('补充已提交，将在当前分身发言后处理。')
    if (question && activeTask.value?.user_question?.id === question.id) delete activeTask.value.user_question
  }, 'sending')
}
function taskAction(name:string) {
  const target = name === 'cancel' ? runningTask.value?.id || taskId.value : taskId.value
  if (!target || busy.value || switching.value || resourceBusy.value) return
  const confirms = name==='accept' || name==='confirm'
  if (confirms && !chatTextFits(message.value)) {
    ElMessage.warning('消息超过 64 KiB（UTF-8），请缩短后发送。')
    return
  }
  const submit = () => {
    const token = pending ||= id()
    return action('/agent-dialogues/'+encodeURIComponent(target)+'/'+name, 'POST',
      confirms ? {message:message.value,file_ids:fileIds.value,request_id:token} : {},
      () => { if (confirms) finishMessage(token) })
  }
  if (!confirms && name !== 'end') return submit()
  return confirmAction(confirms ? '确认由分身继续处理本任务？所选文件和补充意图将一并发送。' : '结束当前任务？聊天记录将保留。',
    '确认任务操作', () => [scope, current.value, taskId.value, commandsDisabled.value, activeTask.value?.status,
      activeTask.value?.revision, activeTask.value?.needs_confirmation, activeTask.value?.can_accept,
      confirms ? [message.value, fileIds.value] : null], submit)
}
function remove(path:string,text:string,method='POST',close=false) {
  if (busy.value || switching.value) return
  return confirmAction(text, '确认清理记录', () => [
    scope, current.value, taskId.value, busy.value, switching.value, activeTask.value?.status, activeTask.value?.revision,
  ], () => action(path,method,{}, () => {
    if (close) closeSession()
    else { resetHistory(); detail.value=null; enterSession(current.value,'') }
  }))
}
function enterSession(session:string, task:string) {
  if (session === current.value && task === taskId.value && detail.value) return
  scope++
  writeState.value='idle'; resetDraft()
  message.value=''; pending=''; filePreview.value=null; cancelHistory()
  if (session !== current.value) {
    resetHistory(); detail.value=null; shownTask.value=''; fileIds.value=[]; resourceBusy.value=false
    taskMenu.value=false; taskSearch.value=''; taskOffset.value=0; taskTotal.value=0
  }
  current.value=session; taskId.value=task; switching.value=!!session
  if (window.matchMedia?.('(max-width: 600px)').matches) taskMenu.value=false
  void polling.restart()
}
function openSession(id: string, event: MouseEvent) {
  openFullscreen.value = true
  sessionTrigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : undefined
  enterSession(id,'')
}
watch(current, async id => {
  if (id) {
    await nextTick()
    windowPanel.value?.focus()
  } else {
    sessionTrigger?.focus()
    sessionTrigger = undefined
  }
})
function escapeSession() {
  const popover=windowPanel.value?.querySelector<HTMLElement>(':popover-open')
  if(popover) { popover.hidePopover(); return }
  closeSession()
}
function closeSession() {
  openFullscreen.value = false
  enterSession('','')
  if (route.query.session || route.query.task) {
    const { session: _session, task: _task, ...query } = route.query
    void router.replace({ query })
  }
}
watch(()=>[route.query.session,route.query.task], ([session,task], previous)=>{
  if (typeof session==='string' && session) enterSession(session, typeof task==='string' ? task : '')
  else if (previous?.[0] && current.value) closeSession()
}, {immediate:true})
onBeforeUnmount(() => { scope++ })
</script>
<template>
  <section class="card peer-session-list">
    <h2 data-guide-target="dialogues">分身间会话</h2>
    <p class="sub">主会话由双方分身自主回复。任务单独确认，完成后将总结返回本人主分身记忆。</p>
    <form class="actions dialogue-settings" @submit.prevent="saveRounds">
      <label class="field">任务最大轮次
        <input placeholder=" " class="form-control" v-model.number="rounds" type="number" min="1" max="1000" required>
      </label>
      <button class="button small" :disabled="busy || rounds===undefined">保存设置</button>
    </form>
    <p class="sub">默认 10 轮，一轮为双方各发言一次，采用双方设置中较小值。</p>
    <div class="actions">
      <button type="button" class="button small danger" @click="remove('/agent-sessions/delete-all','删除已结束会话和已完成任务记录？')">
        删除全部记录</button>
    </div>
    <article v-for="session in sessions" :key="session.session_id" class="notification-row ui-record-row">
      <button type="button" class="notification-line" :aria-label="'与 '+session.peer_name+' 的对话'"
        @click="openSession(session.session_id, $event)">
        <strong>与 {{ session.peer_name }} 的对话</strong>
        <span v-if="sessionSummary(session)" class="notification-summary">{{ sessionSummary(session) }}</span>
      </button>
    </article>
    <p v-if="!sessions.length" class="sub">{{ search ? '暂无匹配会话。' : '暂无分身间会话。向自己的分身提出发送消息或委派任务即可发起。' }}</p>
    <RecordPagination :offset="offset" :total="total" :search="search" label="分身会话分页"
      @update:offset="offset=$event;polling.restart()" @update:search="search=$event;offset=0;polling.restart()" />
  </section>
  <Teleport to="body">
    <Transition name="avatar-pop">
      <section v-if="current" id="agent-dialogue-window" ref="windowPanel" tabindex="-1"
        class="agent-dialogue-window avatar-window" role="dialog" aria-label="分身间会话"
        @keydown.esc.stop.prevent="escapeSession">
        <div class="cardhead window-header chat-header">
          <button type="button" class="button small peer-task-toggle" :aria-expanded="taskMenu"
            aria-controls="peer-task-navigation" @click="taskMenu=!taskMenu">
            <AppIcon name="panel" :size="15" />任务侧栏
          </button>
          <div class="peer-window-heading">
            <h2>{{ detail?.task ? '任务 · ' + detail.peer_name : detail?.peer_name || '分身对话' }}</h2>
            <span class="dialogue-state" role="status" :title="activeStage">{{ switching ? '正在切换…' : progress.label }}
              <template v-if="!switching && activeStage">：{{ activeStage }}</template>
              <template v-if="detail?.task && detail.task.round_limit!=null"> ·
                {{ detail.task.rounds_done ?? 0 }}/{{ detail.task.round_limit }} 轮</template>
              <template v-if="transport"> · {{ transport }}</template>
            </span>
          </div>
          <div class="window-controls">
            <WindowControls anchor-bottom-right :initially-fullscreen="openFullscreen" />
            <WindowClose @close="closeSession" />
          </div>
        </div>
        <div class="peer-workspace" :class="{ 'tasks-open': taskMenu }">
          <DialogueTaskSidebar :open="taskMenu" :task-id="taskId" :tasks="detail?.tasks" :drafting="drafting"
            :disabled="commandsDisabled" :auto-accept="detail?.auto_accept"
            :auto-accept-available="detail?.auto_accept_available" :task-offset="taskOffset" :task-total="taskTotal"
            :task-search="taskSearch" @select="selectTask" @draft="draft"
            @remove="remove(`/agent-sessions/${encodeURIComponent(current)}/tasks/delete-all`,'删除已完成任务记录？')"
            @toggle="toggleAutoAccept" @page="taskPage" @search="taskSearch=$event;taskPage(0)" />
          <main class="peer-chat-pane" :aria-busy="switching">
            <section v-if="detail?.task" :key="shownTask" class="peer-task-context">
              <h3 :title="detail.task.objective">{{ detail.task.objective }}</h3>
              <div v-if="detail.task.can_accept" class="peer-request-bar" role="group" aria-label="处理任务请求">
                <strong>收到任务请求</strong>
                <div class="peer-request-actions">
                  <button type="button" class="button danger" :disabled="commandsDisabled" @click="taskAction('reject')">拒绝任务
                  </button>
                  <button type="button" class="button primary" :disabled="commandsDisabled" @click="taskAction('accept')">
                    同意任务</button>
                </div>
              </div>
              <div v-else-if="progress.note || detail.task.rejected || detail.task.status==='awaiting_input'"
                class="peer-feedback" :data-tone="progress.tone" role="status">
                <strong>{{ progress.label }}</strong>
                <span v-if="progress.note">{{ progress.note }}</span>
              </div>
              <button v-if="!taskClosed && !detail.task.can_accept" type="button" class="button danger small"
                :disabled="commandsDisabled" @click="taskAction('end')">结束任务</button>
              <p v-if="detail.task.workspace_expired" class="sub" role="status">任务文件已过期。聊天记录可继续查看。</p>
              <p v-else-if="detail.task.expires_at" class="sub" role="status">
                任务文件将于 {{ date(detail.task.expires_at) }} 自动删除。需要保留请转存到资料库。
              </p>
              <details v-if="detail.task.returned_summary" class="compact-disclosure peer-result">
                <summary>
                  <AppIcon name="chevron" :size="14" />任务总结已返回主分身记忆
                </summary>
                <MarkdownContent :source="detail.task.returned_summary" :diagrams="false" />
              </details>
            </section>
            <p v-else-if="detail?.chat?.status==='awaiting_input'" class="notice" role="status">{{ progress.label }}
              <template v-if="progress.note"> · {{ progress.note }}</template>
            </p>
            <p v-else-if="detail?.chat?.error" class="notice error">{{ detail.chat.error }}</p>
            <ElButton v-if="activeTask?.can_retry" size="small" :disabled="switching" @click="retry(activeTask)">重新编辑并发送
            </ElButton>
            <EmbeddingNotice v-bind="detail?.embedding_status" :peer-name="detail?.peer_name" />
            <MessageCopyMenu :key="current+shownTask" ref="copyMenu" />
            <div ref="transcript" class="dialogue-log" role="log" aria-label="分身会话消息记录" tabindex="0" v-on="historyEvents"
              @contextmenu="copyMenu?.open($event)" @keydown="copyMenu?.open($event)">
              <p v-if="detail && !allMessages.length" class="sub">{{ detail.task ? '本任务暂无消息。' : '主会话暂无消息。' }}</p>
              <ContextCompacted v-if="compressionThrough && compactedAfter === undefined" />
              <template v-for="{ item, task, starts, ends, files } in transcriptRows"
                v-memo="[item, task, starts, ends, files, resourceId, shownTask, item.turn === compactedAfter]"
                :key="shownTask+':'+item.turn">
                <div v-if="starts" class="peer-request-boundary" role="separator" :title="task?.objective">请求开始 ·
                  {{ task?.delivery_mode==='message'?'发送消息':'任务' }}
                  <template v-if="task?.created_at"> · {{ date(task?.created_at) }}</template>
                </div>
                <article v-if="item.speaker_role==='user'" class="peer-user-note" :data-history-message="item.turn"
                  data-copy-message tabindex="0" aria-haspopup="menu" :class="item.is_own?'peer-own':'peer-other'">
                  <small>{{ item.is_own?'我':item.speaker_name }}补充要求</small>
                  <ChatMarkdown hide-citations :artifact-reply="false" :source="item.content" :attachments="files"
                    :conversation-id="resourceId" @preview="filePreview=$event" />
                </article>
                <article v-else class="peer-turn peer-message" :data-history-message="item.turn" data-copy-message
                  tabindex="0" aria-haspopup="menu" :class="item.is_own?'peer-own':'peer-other'">
                  <strong class="peer-speaker-name">{{ item.speaker_name }}</strong>
                  <div class="bubble">
                    <ToolActivity v-if="item.tool_activity?.length" :items="item.tool_activity" />
                    <ChatMarkdown hide-citations :source="item.content" :events="item.tool_activity" :attachments="files"
                      :conversation-id="resourceId" @preview="filePreview=$event" />
                    <ChatNotices :items="item.tool_activity"
                      :message="item.state === 'ended' || item.state === 'awaiting_input' ? item.state_detail || (item.state === 'ended' ? '任务已结束' : '等待用户') : undefined" />
                  </div>
                </article>
                <div v-if="ends" class="peer-request-boundary" role="separator">请求结束<template v-if="task?.ended_at"> ·
                    {{ date(task?.ended_at) }}</template>
                </div>
                <ContextCompacted v-if="item.turn === compactedAfter" />
              </template>
              <article v-if="runningTask && (!runningTask.sending_prepared || !detail?.task)"
                :key="runningTask.id + ':' + runningTask.active_speaker" class="peer-turn peer-pending"
                :class="runningTask.active_speaker_is_own ? 'peer-own' : 'peer-other'">
                <strong class="peer-speaker-name">{{ runningTask.active_speaker || '分身' }}</strong>
                <div class="bubble">
                  <ToolActivity :items="runningTask.tool_activity || []" :pending="activeStage" />
                  <ChatNotices :items="runningTask.tool_activity" />
                </div>
              </article>
            </div>
            <div class="peer-composer">
              <AgentQuestionPrompt v-if="!taskClosed" ref="questionPrompt" :key="current+'/'+shownTask"
                :scope="'peer:'+current+'/'+shownTask" :question="userQuestion" :disabled="commandsDisabled || toolsSaving"
                @answer="send" />
              <ComposerInput v-model="message"
                :usage-url="current ? '/agent-sessions/'+encodeURIComponent(current)+'/compact'+(taskId?'?task_id='+encodeURIComponent(taskId):'') : ''"
                :usage-revision="(detail?.history_revision||'')+'/'+(detail?.summary_through||0)"
                :disabled="switching || taskClosed" label="分身会话消息" @submit="send()"
                :placeholder="taskClosed ? '任务已结束' : runningTask || pausedTask ? '补充任务所需信息…' : '输入消息或任务目标…'"
                @update:model-value="pending=''" />
              <div class="chat-compose-actions">
                <ComposerToolbar class="peer-composer-toolbar" label="分身会话工具栏">
                  <ChatFilesManager v-if="resourceId || taskClosed" :conversation-id="resourceId" label="共享文件"
                    :shared-revision="sharedFilesRevision" :read-only="taskClosed"
                    :busy="busy || switching || detail?.task?.status==='running'" @selected="fileIds=$event"
                    @uploading="resourceBusy=$event" />
                  <ConversationTools :conversation-id="settingsId" @saving="toolPending=$event"
                    :busy="taskClosed || busy || switching || resourceBusy || detail?.task?.status==='running'" />
                  <details class="composer-tool chat-command-menu">
                    <summary class="chat-command chat-icon-command" aria-label="会话操作" title="会话操作">
                      <AppIcon name="more" :size="18" />
                    </summary>
                    <div class="composer-popover chat-command-panel">
                      <template v-if="taskId">
                        <button type="button" class="chat-menu-item"
                          :disabled="commandsDisabled || detail?.task?.status==='paused' || detail?.task?.status==='ended'"
                          @click="taskAction('cancel')">
                          <AppIcon name="clock" :size="16" />暂停
                        </button>
                        <button type="button" class="chat-menu-item danger" :disabled="commandsDisabled"
                          @click="remove(`/agent-dialogues/${encodeURIComponent(taskId)}`,'删除本方任务记录？双方都删除后才回收共享工作区。未完成任务将终止。','DELETE')">
                          <AppIcon name="trash" :size="16" />删除任务
                        </button>
                      </template>
                      <button v-else type="button" class="chat-menu-item" :disabled="commandsDisabled"
                        @click="remove(`/agent-sessions/${encodeURIComponent(current)}/clear`,'清空本方记录与记忆？')">
                        <AppIcon name="rebuild" :size="16" />清空本方记录与记忆
                      </button>
                      <button type="button" class="chat-menu-item danger" :disabled="commandsDisabled"
                        @click="remove(`/agent-sessions/${encodeURIComponent(current)}`,'删除本方会话视图？','DELETE',true)">
                        <AppIcon name="trash" :size="16" />删除会话
                      </button>
                    </div>
                  </details>
                  <span v-if="writeState==='sending'" role="status">正在发送…</span>
                  <button v-if="detail?.task?.needs_confirmation" type="button" class="button chat-command"
                    :disabled="commandsDisabled" @click="taskAction('confirm')">确认并补充意图</button>
                  <div class="composer-primary-actions">
                    <EffortPicker :conversation-id="settingsId" :busy="!!runningTask" :read-only="taskClosed"
                      @saving="effortPending=$event" />
                    <ComposerAction v-if="runningTask && !message.trim()" label="暂停任务" stop :disabled="commandsDisabled"
                      @click="taskAction('cancel')" />
                    <ComposerAction v-else :resume="!!pausedTask && !message.trim()"
                      :label="runningTask ? '发送补充' : pausedTask ? (message.trim() ? '补充并继续' : '继续任务') : '发送'"
                      :aria-busy="writeState==='sending'" :disabled="sendDisabled" @click="send()" />
                  </div>
                </ComposerToolbar>
              </div>
            </div>
          </main>
        </div>
      </section>
    </Transition>
  </Teleport>
  <IntentDialog v-if="intent" :intent="intent" @closed="intent=null"
    @sent="(session,task)=>{intent=null;enterSession(session,task)}" />
  <FilePreview v-if="filePreview?.download_url" :file="filePreview" :url="filePreview.download_url"
    @close="filePreview=null" />
</template>
