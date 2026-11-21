<script setup lang="ts">
import { defineAsyncComponent, computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElMessage } from 'element-plus'
import { request, notifyError } from '../api/client'
import { usePolling } from '../composables/usePolling'
import { useHistoryScroll } from '../composables/useHistoryScroll'
import ToolActivity from '../components/chat/ToolActivity.vue'
import MessageCopyMenu from '../components/chat/MessageCopyMenu.vue'
import EmbeddingNotice from '../components/chat/EmbeddingNotice.vue'
import ChatMarkdown from '../components/chat/ChatMarkdown.vue'
import ChatNotices from '../components/chat/ChatNotices.vue'
import ContextCompacted from '../components/chat/ContextCompacted.vue'
import { date } from '../utils/format'
import type { ChatFile, Page, ToolEvent } from '../types'
import MarkdownContent from '../components/MarkdownContent.vue'
import ComposerToolbar from '../components/chat/ComposerToolbar.vue'
import ComposerInput from '../components/chat/ComposerInput.vue'
import ChatFilesManager from '../components/chat/ChatFilesManager.vue'
import ConversationTools from '../components/chat/ConversationTools.vue'
import AppIcon from '../components/AppIcon.vue'
import RecordPagination from '../components/RecordPagination.vue'
import WindowClose from '../components/WindowClose.vue'
import WindowControls from '../components/WindowControls.vue'
import { requestId } from '../utils/requestId'

const FilePreview = defineAsyncComponent(() => import('../components/FilePreview.vue'))
const IntentDialog = defineAsyncComponent(() => import('../components/collaboration/IntentDialog.vue'))
interface Session { session_id:string; peer_name:string; messages?: Pick<Message, 'speaker_role' | 'content'>[] }
interface Message { dialogue_id:string; turn:number; speaker_name:string; speaker_role:string; speaker_user_id?:string; content:string; state?:string; state_detail?:string; task_action?:string; tool_activity?:ToolEvent[]; is_own:boolean }
interface TaskFile extends ChatFile {message_id:number|null;sender_user_id:string;download_url:string}
interface Task { can_supplement?:boolean; revision?:number; files?:TaskFile[];id:string; objective:string; status:string; can_accept?:boolean; needs_confirmation?:boolean; delivery_mode:string; can_retry?:boolean; auto_retrying?:boolean; error?:string; rounds_done?:number; round_limit?:number;tool_activity?:ToolEvent[];skill_commands?:string;created_at?:string;ended_at?:string;returned_summary?:string;waiting_reason?:string;waiting_for_other?:boolean; end_reason?:string; reason?:string; message_delivered?:boolean; active_speaker?:string; active_speaker_is_own?:boolean }
interface Intent {id:string;conversation_id:string;target_query:string;target_aic:string;objective:string;shared_content:string}
interface Detail { summary_through?:number; history_revision?:string; task_page?:Page<Task>; embedding_status?:{configured?:boolean;peerConfigured?:boolean}; resource_conversation_id:string; current_transport_mode?:string; chat?:Task; peer_name:string; messages:Message[]; message_tasks?:Task[]; tasks:Task[]; task?:Task; has_more:boolean }
interface Skill {id:string;name:string;enabled:boolean}
const copyMenu=ref<InstanceType<typeof MessageCopyMenu>>()
const skills=ref<Skill[]>([])
const route=useRoute(), router=useRouter()
const sessions=ref<Session[]>([]), current=ref(''), taskId=ref(''), detail=ref<Detail|null>(null), message=ref(''), rounds=ref<number>(), search=ref(''), offset=ref(0), total=ref(0)
const writeState = ref<'idle' | 'sending' | 'working'>('idle')
const busy = computed(() => writeState.value !== 'idle')
const resourceBusy=ref(false), taskMenu=ref(false), switching=ref(false), shownTask=ref('')
const filePreview=ref<ChatFile|null>(null)
const toolsSaving = ref(false)
const commandsDisabled = computed(() => busy.value || switching.value || resourceBusy.value || !detail.value)
const sendDisabled = computed(() => commandsDisabled.value || toolsSaving.value || !message.value.trim() || (!/^\/compact(?:\s|$)/.test(message.value.trim()) && (detail.value?.task?.can_supplement === false ||
  detail.value?.task?.status === 'ended' || detail.value?.task?.status === 'awaiting_acceptance' || !!detail.value?.task?.needs_confirmation)))
const activeTask=computed(()=>detail.value?.task || detail.value?.chat)
const runningTask = computed(() => activeTask.value?.status === 'running' ? activeTask.value : undefined)
const activeStage = computed(() => {
  if (!runningTask.value) return ''
  const stage = runningTask.value.tool_activity?.at(-1)
  return stage?.status === 'running' ? stage.steps?.at(-1)?.message || stage.name : stage?.name || '准备回答'
})
const pausedTask = computed(() => activeTask.value?.status === 'awaiting_input' && activeTask.value.can_supplement ? activeTask.value : undefined)
const transport=computed(()=>detail.value?.current_transport_mode==='acps'?'ACPS'
  :detail.value?.current_transport_mode==='internal'?'平台内部传输':'')
const progress=computed(()=>{
  const task=activeTask.value
  if (!task) return { label:'主会话',tone:'neutral',note:'' }
  if (task.auto_retrying || task.status==='awaiting_identity') return {label:'正在自动重试',tone:'waiting',note:task.error || task.waiting_reason || ''}
  if (task.can_retry) return {label:'重试已超时',tone:'error',note:task.error || ''}
  if (task.status==='awaiting_input' && typeof task.can_supplement === 'boolean') return {label:task.can_supplement?'等待本人补充信息':'等待对方补充信息',tone:'waiting',note:task.waiting_reason || ''}
  if (task.status==='awaiting_input') return {label:task.needs_confirmation?'等待本人确认':task.waiting_for_other?'等待对方确认':'等待用户',tone:'waiting',note:task.waiting_reason || ''}
  if (task.status==='ended') return {label:task.message_delivered?'已回复':'任务已结束',tone:'success',note:task.end_reason || task.reason || ''}
  if (task.status==='running') return {label:(task.active_speaker || '分身')+'正在回答',tone:'active',note:''}
  return {label:taskStatuses[task.status] || task.status,tone:task.status==='failed'?'error':'neutral',note:task.error || task.waiting_reason || ''}
})
function messageFeedback(item:Message) {
  const source = item.content.trimEnd()
  const trailer = !item.task_action && /(?:^|\n)\[(?:等待用户|方案待确认)\]\s*([^\n]*)$/.exec(source)
  const body = trailer ? source.slice(0,trailer.index).trimEnd() : source
  const state = item.state==='ended'?'ended':item.state==='awaiting_input'||trailer?'awaiting_input':''
  return {body,state,detail:item.state_detail || (trailer ? trailer[1] : '')}
}
const taskStatuses: Record<string,string> = { queued:'等待处理', running:'进行中', awaiting_acceptance:'等待接收', awaiting_input:'等待输入', paused:'已暂停', ended:'已结束', failed:'失败' }
function selectTask(value: string) {
  if (!busy.value) enterSession(current.value, value)
}
function taskPage(value: number) { taskOffset.value=value; void polling.restart() }
const windowPanel = ref<HTMLElement>()
const transcript = ref<HTMLElement>()
const {historyEvents, captureAnchor} = useHistoryScroll(transcript, loadOlder)
const openFullscreen = ref(false)
let sessionTrigger: HTMLElement | undefined
const taskMap=ref<Record<string,Task>>({})
const taskSearch=ref(''),taskOffset=ref(0),taskTotal=ref(0)
const hasOlder=ref(false), allMessages=computed(()=>[...new Map([...older.value,...(detail.value?.messages||[])].map(item=>[item.turn,item])).values()].filter(item=>!(item.speaker_role==='user'&&item.speaker_name==='用户授权共享资料')).sort((a,b)=>a.turn-b.turn))
const activeSkills = computed(() => {
  const commands = detail.value?.task?.skill_commands || ''
  const names = [...commands.matchAll(/\$("(?:\\.|[^"\\])*"|[^\s$]+)/g)].map(match => {
    if (!match[1].startsWith('"')) return match[1]
    try { return String(JSON.parse(match[1])) } catch { return match[1] }
  })
  return skills.value.filter(skill => names.some(name=>name.toLocaleLowerCase()===skill.name.toLocaleLowerCase()))
})
const resourceId = computed(() => detail.value?.resource_conversation_id || '')
const intent=ref<Intent|null>(null), drafting=ref(false), fileIds=ref<string[]>([]), draftRequest=ref(''), older=ref<Message[]>([])
let disposed = false, scope = 0, pending = '', olderRequest:AbortController|undefined
const id=()=>requestId()
const compressionThrough = computed(() => detail.value?.summary_through || 0)
const compactedAfter = computed(() => allMessages.value.filter(row => row.turn <= compressionThrough.value).at(-1)?.turn)
const transcriptRows = computed(() => {
  const messages = allMessages.value
  const firstAgentTurn = new Map<string, Map<string | undefined, number>>()
  for (const message of messages) {
    let speakers = firstAgentTurn.get(message.dialogue_id)
    if (!speakers) firstAgentTurn.set(message.dialogue_id, speakers = new Map())
    if (message.speaker_role === 'agent' && !speakers.has(message.speaker_user_id)) speakers.set(message.speaker_user_id, message.turn)
  }
  const filesByDialogue = new Map<string, Map<number, TaskFile[]>>()
  for (const [dialogue, speakers] of firstAgentTurn) {
    const filesByTurn = new Map<number, TaskFile[]>()
    filesByDialogue.set(dialogue, filesByTurn)
    for (const file of taskMap.value[dialogue]?.files || []) {
      const turn = file.message_id || speakers.get(file.sender_user_id)
      if (turn === undefined) continue
      const files = filesByTurn.get(turn)
      if (files) files.push(file)
      else filesByTurn.set(turn, [file])
    }
  }
  return messages.map((item, index) => {
    const task = taskMap.value[item.dialogue_id]
    return { item, task, files:filesByDialogue.get(item.dialogue_id)?.get(item.turn), feedback:messageFeedback(item),
      starts:messages[index - 1]?.dialogue_id !== item.dialogue_id,
      ends:messages[index + 1]?.dialogue_id !== item.dialogue_id && task?.status === 'ended' }
  })
})
function isDenied(error: unknown) {
  return error instanceof Error && 'status' in error && [401,403,404].includes(Number(error.status))
}
function detailPath(session:string, task:string) {
  return task ? '/agent-dialogues/' + encodeURIComponent(task) : '/agent-sessions/' + encodeURIComponent(session)
}
function resetHistory(closePreview = true) {
  olderRequest?.abort()
  olderRequest = undefined
  older.value = []
  taskMap.value = {}
  hasOlder.value = false
  if (closePreview) filePreview.value = null
}
async function refresh(signal: AbortSignal) {
  const session = current.value, task = taskId.value
  try {
    if (!session) {
      const query = new URLSearchParams({summary:'true',search:search.value,offset:String(offset.value)})
      const page = await request<Page<Session>>('/agent-sessions?' + query, 'GET', undefined, signal)
      if (!signal.aborted) { sessions.value = page.items; offset.value = page.offset; total.value = page.total }
      return
    }
    const path = detailPath(session,task)
    const query = new URLSearchParams()
    if (taskOffset.value) query.set('task_offset', String(taskOffset.value))
    if (taskSearch.value) query.set('task_search', taskSearch.value)
    const tail = () => request<Detail>(path + (query.size ? '?' + query : ''), 'GET', undefined, signal)
    let value = await tail()
    if (signal.aborted) return
    const previous = detail.value
    const sameHistory = () => !!previous?.history_revision && shownTask.value === task
      && previous.history_revision === value.history_revision
    let retain = sameHistory() && value.has_more
    let cursor = previous?.messages.at(-1)?.turn ?? 0
    const first = value.messages[0]?.turn ?? 0
    const missing:Message[] = [], messageTasks:Task[] = []
    // The latest ten messages need not overlap a tab's previous tail after it wakes up.
    while (retain && cursor < first) {
      const page = await request<Detail>(path + '?after=' + cursor, 'GET', undefined, signal)
      if (signal.aborted) return
      if (page.history_revision !== value.history_revision) {
        retain = false
        value = await tail()
        break
      }
      const next = page.messages.at(-1)?.turn
      if (next === undefined || next <= cursor) break
      missing.push(...page.messages)
      messageTasks.push(...(page.message_tasks || []))
      cursor = next
      if (!page.has_more) break
    }
    if (signal.aborted) return
    // Older servers return the filtered task page separately.
    const page = value.task_page ?? await request<Page<Task>>(pathForTasks(session), 'GET', undefined, signal)
    if (signal.aborted) return
    const log = transcript.value
    const atBottom = log && log.scrollHeight - log.clientHeight - log.scrollTop <= 2
    const beforeRun = previous?.task || previous?.chat, afterRun = value.task || value.chat
    const activityChanged = afterRun?.status === 'running' && (
      beforeRun?.status !== 'running' || beforeRun.id !== afterRun.id ||
      beforeRun.tool_activity?.length !== afterRun.tool_activity?.length
    )
    const followTail = !previous || shownTask.value !== task || (
      atBottom && !olderRequest && (activityChanged || previous.summary_through !== value.summary_through || previous.messages.at(-1)?.turn !== value.messages.at(-1)?.turn)
    )
    if (retain && previous) {
      const rows = [...older.value,...previous.messages,...missing].filter(item => item.turn < first)
      older.value = [...new Map(rows.map(item => [item.turn,item])).values()]
    } else {
      resetHistory(!sameHistory())
      hasOlder.value = value.has_more
    }
    for (const row of [...(retain ? messageTasks : []),...(value.message_tasks || []),...page.items,...(value.task ? [value.task] : [])]) taskMap.value[row.id] = row
    taskTotal.value = page.total
    taskOffset.value = page.offset
    shownTask.value = task
    detail.value = {...value, tasks:page.items}
    switching.value = false
    if (followTail) {
      await nextTick()
      if (!signal.aborted && transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
    }
  } catch (error) {
    if (signal.aborted) return
    if (isDenied(error)) {
      sessions.value = []; total.value = 0
      if (session) closeSession()
    } else {
      if (switching.value && detail.value) taskId.value = shownTask.value
      switching.value = false
    }
    notifyError(error)
  }
}
function pathForTasks(session:string) {
  return '/agent-sessions/' + encodeURIComponent(session) + '/tasks?' + new URLSearchParams({offset:String(taskOffset.value),search:taskSearch.value})
}
const polling = usePolling(refresh, () => {
  const tasks = [activeTask.value,...(detail.value?.tasks || [])]
  return tasks.some(t => t && (t.auto_retrying || ['queued','running','awaiting_identity'].includes(t.status))) ? 1500 : 30000
})
async function action(path:string, method:string, body:unknown, onSuccess:(result:{message:string;compressed?:boolean})=>void, state:'sending'|'working'='working') {
  if (busy.value || switching.value) return
  const ticket = scope
  writeState.value = state
  try {
    const result = await request<{message:string;compressed?:boolean}>(path, method, body)
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
function saveRounds() {
  return action('/agent-dialogue-settings','PUT',{max_rounds:rounds.value}, () => ElMessage.success('自动聊天轮次设置已保存'))
}
function finishMessage(requestId:string) {
  if (pending === requestId) { message.value=''; pending='' }
}
function send() {
  if (sendDisabled.value || !current.value) return
  if (/^\/compact(?:\s|$)/.test(message.value.trim())) {
    const submitted = message.value
    const query = taskId.value ? '?task_id='+encodeURIComponent(taskId.value) : ''
    return action('/agent-sessions/'+encodeURIComponent(current.value)+'/compact'+query,'POST',{focus:submitted.trim().slice(8).trim()}, result => {
      if (message.value === submitted) message.value=''
      if (!result.compressed) ElMessage.info(result.message)
      else if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
    })
  }
  const token = pending ||= id()
  const target = pausedTask.value?.id || taskId.value
  const path = target ? '/agent-dialogues/'+encodeURIComponent(target)+'/resume'
    : '/agent-sessions/'+encodeURIComponent(current.value)+'/messages'
  return action(path,'POST',{message:message.value,file_ids:fileIds.value,request_id:token,
    ...(pausedTask.value ? {expected_revision:pausedTask.value.revision} : target ? {} : {new_task:true,delivery_mode:'message'})}, () => finishMessage(token), 'sending')
}
function removeSkill(skill:Skill) {
  if (!taskId.value || busy.value || switching.value) return
  const token = pending ||= id()
  return action('/agent-dialogues/'+encodeURIComponent(taskId.value)+'/resume','POST',
    {message:'', request_id:token, remove_skills:[skill.id]}, () => { if (pending === token) pending='' })
}
function taskAction(name:string) {
  const pause = name === 'end' ? pausedTask.value : undefined
  const target = pause?.id || taskId.value || (name === 'end' ? runningTask.value?.id : undefined)
  if (!target || busy.value || switching.value || resourceBusy.value) return
  const confirms = name==='accept' || name==='confirm'
  if (confirms && !confirm('确认由分身继续处理本任务？所选文件和补充意图将一并发送。')) return
  const token = pending ||= id()
  const query = pause?.revision !== undefined ? '?expected_revision=' + pause.revision : ''
  return action('/agent-dialogues/'+encodeURIComponent(target)+'/'+name+query, 'POST',
    confirms ? {message:message.value,file_ids:fileIds.value,request_id:token} : {}, () => finishMessage(token))
}
async function loadDraft(path:string, body:Record<string,string>) {
  if (switching.value || drafting.value || intent.value) return
  const ticket = scope
  drafting.value = true
  draftRequest.value ||= id()
  try {
    const value = await request<Intent>(path,'POST',{...body,request_id:draftRequest.value})
    if (ticket === scope) { draftRequest.value=''; intent.value=value }
  } catch (error) {
    if (ticket === scope) notifyError(error)
  } finally {
    if (ticket === scope) drafting.value=false
  }
}
function retry(task:Task) { return loadDraft('/agent-dialogues/'+encodeURIComponent(task.id)+'/retry-draft',{}) }
function draft() {
  if (current.value) return loadDraft('/agent-sessions/'+encodeURIComponent(current.value)+'/task-draft',{message:message.value.trim()})
}
async function loadOlder() {
  const session=current.value, task=shownTask.value, previous=detail.value
  if (!hasOlder.value || !session || switching.value || !previous?.messages.length || olderRequest) return
  const controller = new AbortController()
  olderRequest = controller
  const before=Math.min(...previous.messages.map(m=>m.turn),...older.value.map(m=>m.turn))
  try {
    const value=await request<Detail>(detailPath(session,task)+'?before='+before,'GET',undefined,controller.signal)
    if (controller.signal.aborted || session !== current.value || task !== shownTask.value || switching.value) return
    if (value.history_revision !== previous.history_revision) {
      resetHistory(); detail.value = null
      await polling.restart()
      return
    }
    const restore = captureAnchor()
    older.value=[...value.messages,...older.value]
    for (const row of value.message_tasks || []) taskMap.value[row.id]=row
    hasOlder.value=value.has_more && value.messages.some(message => message.turn < before)
    await nextTick()
    if (!controller.signal.aborted && session === current.value && task === shownTask.value) restore()
  } catch (error) {
    if (!controller.signal.aborted && session === current.value && task === shownTask.value) {
      if (isDenied(error)) closeSession()
      notifyError(error)
    }
  } finally { if (olderRequest === controller) olderRequest = undefined }
}
function remove(path:string,text:string,method='POST',close=false) {
  if (busy.value || switching.value || !confirm(text)) return
  return action(path,method,{}, () => {
    if (close) closeSession()
    else { resetHistory(); detail.value=null; enterSession(current.value,'') }
  })
}
function enterSession(session:string, task:string) {
  if (session === current.value && task === taskId.value && detail.value) return
  scope++
  writeState.value='idle'; drafting.value=false; intent.value=null; draftRequest.value=''
  message.value=''; pending=''; filePreview.value=null; olderRequest?.abort()
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
onMounted(async () => {
  try { skills.value = await request<Skill[]>('/me/skills') } catch(e) { if (!disposed) notifyError(e) }
  try { rounds.value = (await request<{max_rounds:number}>('/agent-dialogue-settings')).max_rounds } catch(e) { if (!disposed) notifyError(e) }
})
onBeforeUnmount(() => { disposed=true; scope++; olderRequest?.abort() })
</script>
<template>
<section class="card peer-session-list">
  <h2 data-guide-target="dialogues">分身间会话</h2>
  <p class="sub">主会话由双方分身自主回复。任务单独确认，完成后将总结返回本人主分身记忆。</p>
  <form class="actions dialogue-settings" @submit.prevent="saveRounds">
    <label class="field">任务最大轮次<input placeholder=" " class="form-control" v-model.number="rounds" type="number" min="1" max="1000" required></label>
    <button class="button small" :disabled="busy || rounds===undefined">保存设置</button>
  </form>
  <p class="sub">默认 10 轮，一轮为双方各发言一次，采用双方设置中较小值。</p>
  <div class="actions"><button type="button" class="button small danger" @click="remove('/agent-sessions/delete-all','删除已结束会话和已完成任务记录？')">删除全部记录</button></div>
  <article v-for="session in sessions" :key="session.session_id" class="notification-row ui-record-row">
    <button type="button" class="notification-line" :aria-label="'与 '+session.peer_name+' 的对话'" @click="openSession(session.session_id, $event)">
      <strong>与 {{ session.peer_name }} 的对话</strong><span v-if="sessionSummary(session)" class="notification-summary">{{ sessionSummary(session) }}</span>
    </button>
  </article>
  <p v-if="!sessions.length" class="sub">{{ search ? '暂无匹配会话。' : '暂无分身间会话。向自己的分身提出发送消息或委派任务即可发起。' }}</p>
  <RecordPagination :offset="offset" :total="total" :search="search" label="分身会话分页" @update:offset="offset=$event;polling.restart()" @update:search="search=$event;offset=0;polling.restart()"/></section><Teleport to="body"><Transition name="avatar-pop"><section v-if="current" id="agent-dialogue-window" ref="windowPanel" tabindex="-1" class="agent-dialogue-window avatar-window" role="dialog" aria-label="分身间会话" @keydown.esc.stop.prevent="escapeSession"><div class="cardhead window-header chat-header"><button type="button" class="button small peer-task-toggle" :aria-expanded="taskMenu" aria-controls="peer-task-navigation" @click="taskMenu=!taskMenu"><AppIcon name="panel" :size="15"/>任务侧栏</button><div class="peer-window-heading">
  <h2>{{ detail?.task ? '任务 · ' + detail.peer_name : detail?.peer_name || '分身对话' }}</h2>
  <span class="dialogue-state" role="status" :title="activeStage">{{ switching ? '正在切换…' : progress.label }}<template v-if="!switching && activeStage">：{{ activeStage }}</template><template v-if="detail?.task && detail.task.round_limit!=null"> · {{ detail.task.rounds_done ?? 0 }}/{{ detail.task.round_limit }} 轮</template><template v-if="transport"> · {{ transport }}</template></span>
</div><div class="window-controls"><WindowControls anchor-bottom-right :initially-fullscreen="openFullscreen"/><WindowClose @close="closeSession"/></div></div><div class="peer-workspace" :class="{ 'tasks-open': taskMenu }"><aside :inert="!taskMenu" :aria-hidden="!taskMenu" id="peer-task-navigation" class="peer-task-nav" aria-label="任务侧栏">
  <div class="peer-task-actions">
    <button type="button" class="button small" :aria-current="!taskId ? 'page' : undefined" @click="selectTask('')">主会话</button>
    <button type="button" class="button small primary" :disabled="drafting" @click="draft">{{ drafting ? '正在打开…' : '＋ 创建任务' }}</button>
    <button type="button" class="button small danger peer-delete-records" aria-label="删除全部任务记录" title="删除全部任务记录" @click="remove(`/agent-sessions/${encodeURIComponent(current)}/tasks/delete-all`,'删除已完成任务记录？')"><AppIcon name="trash" :size="14"/><span>删除记录</span></button>
  </div>

  <div class="peer-task-list">
    <button v-for="task in detail?.tasks || []" :key="task.id" type="button" :aria-current="task.id===taskId" :title="task.objective" @click="selectTask(task.id)">
      <strong>{{ task.objective }}</strong><small>{{ taskStatuses[task.status] || task.status }}</small>
    </button>
    <p v-if="detail && !detail.tasks.length" class="sub">暂无任务</p>
  </div>
  <RecordPagination class="peer-task-paging" :offset="taskOffset" :total="taskTotal" :search="taskSearch" label="任务分页" @update:offset="taskPage" @update:search="taskSearch=$event;taskPage(0)"/>
</aside><main class="peer-chat-pane" :aria-busy="switching">
  <section v-if="detail?.task" :key="shownTask" class="peer-task-context">
    <h3 :title="detail.task.objective">{{ detail.task.objective }}</h3>
    <div v-if="progress.note || detail.task.can_accept" class="peer-feedback" :data-tone="progress.tone" role="status">
      <strong>{{ detail.task.can_accept ? '收到任务请求' : progress.label }}</strong>
      <span v-if="progress.note">{{ progress.note }}</span>
    </div>
    <details v-if="detail.task.returned_summary" class="compact-disclosure peer-result">
      <summary><AppIcon name="chevron" :size="14"/>任务总结已返回主分身记忆</summary>
      <MarkdownContent :source="detail.task.returned_summary" :diagrams="false"/>
    </details>
  </section>
  <p v-else-if="detail?.chat?.status==='awaiting_input' && detail.chat.waiting_reason" class="notice" role="status">{{ detail.chat.waiting_reason }}</p>
  <p v-else-if="detail?.chat?.error" class="notice error">{{ detail.chat.error }}</p>
  <ElButton v-if="activeTask?.can_retry" size="small" :disabled="switching" @click="retry(activeTask)">重新编辑并发送</ElButton>
  <EmbeddingNotice v-bind="detail?.embedding_status" :peer-name="detail?.peer_name"/>
  <MessageCopyMenu :key="current+shownTask" ref="copyMenu"/>
  <div ref="transcript" class="dialogue-log" role="log" aria-label="分身会话消息记录" tabindex="0" v-on="historyEvents" @contextmenu="copyMenu?.open($event)" @keydown="copyMenu?.open($event)">
    <p v-if="detail && !allMessages.length" class="sub">{{ detail.task ? '本任务暂无消息。' : '主会话暂无消息。' }}</p>
    <ContextCompacted v-if="compressionThrough && compactedAfter === undefined"/>
    <template v-for="{ item, task, starts, ends, files, feedback } in transcriptRows" v-memo="[item, task, starts, ends, files, resourceId, shownTask, item.turn === compactedAfter]" :key="shownTask+':'+item.turn">
      <div v-if="starts" class="peer-request-boundary" role="separator" :title="task?.objective">请求开始 · {{ task?.delivery_mode==='message'?'发送消息':'任务' }}<template v-if="task?.created_at"> · {{ date(task?.created_at) }}</template></div>
      <article v-if="item.speaker_role==='user'" class="peer-user-note" :data-history-message="item.turn" data-copy-message tabindex="0" aria-haspopup="menu" :class="item.is_own?'peer-own':'peer-other'">
        <small>{{ item.is_own?'我':item.speaker_name }}补充要求</small><ChatMarkdown :artifact-reply="false" :source="item.content" :attachments="files" :conversation-id="resourceId" @preview="filePreview=$event"/>
      </article>
      <article v-else class="peer-turn peer-message" :data-history-message="item.turn" data-copy-message tabindex="0" aria-haspopup="menu" :class="item.is_own?'peer-own':'peer-other'">
        <strong class="peer-speaker-name">{{ item.speaker_name }}</strong>
        <div class="bubble">
          <ToolActivity v-if="item.tool_activity?.length" :items="item.tool_activity"/>
          <ChatMarkdown :source="feedback.body" :events="item.tool_activity" :attachments="files" :conversation-id="resourceId" @preview="filePreview=$event"/>
          <ChatNotices :items="item.tool_activity" :message="feedback.state ? feedback.detail || (feedback.state==='ended'?'任务已结束':'等待用户') : undefined"/>
        </div>
      </article>
      <div v-if="ends" class="peer-request-boundary" role="separator">请求结束<template v-if="task?.ended_at"> · {{ date(task?.ended_at) }}</template></div>
      <ContextCompacted v-if="item.turn === compactedAfter"/>
    </template>
    <article v-if="runningTask" :key="runningTask.id + ':' + runningTask.active_speaker" class="peer-turn peer-pending"
      :class="runningTask.active_speaker_is_own ? 'peer-own' : 'peer-other'">
      <strong class="peer-speaker-name">{{ runningTask.active_speaker || '分身' }}</strong>
      <div class="bubble"><ToolActivity :items="runningTask.tool_activity || []" pending="准备回答"/><ChatNotices :items="runningTask.tool_activity"/></div>
    </article>
  </div><div class="peer-composer">
  <ComposerInput v-model="message" :catalog="skills" :session="taskId || current" :disabled="switching" label="分身会话消息" @submit="send()" :placeholder="pausedTask ? '补充任务所需信息…' : '输入消息或任务目标，$ 选择技能…'" @update:model-value="pending=''"/>
  <div class="chat-compose-actions">
  <ComposerToolbar class="peer-composer-toolbar" label="分身会话工具栏">
    <details class="composer-menu composer-tool">
      <summary class="chat-command" title="管理当前任务技能"><span aria-hidden="true">✧</span><span>技能</span><small>{{ activeSkills.length }}</small></summary>
      <div class="composer-popover">
        <h3>当前任务技能</h3>
        <p class="sub">在输入框键入 $ 选择技能。移出仅影响当前任务。</p>
        <p v-if="!activeSkills.length" class="sub">尚无已加载技能。</p>
        <div v-for="skill in activeSkills" :key="skill.id" class="session-skill">
          <span>{{ skill.name }}</span><button type="button" class="button small" :disabled="busy || switching || detail?.task?.status==='ended'" @click="removeSkill(skill)">移出本任务</button>
        </div>
      </div>
    </details>
    <ChatFilesManager :conversation-id="resourceId" :busy="busy || switching || detail?.task?.status==='running'" @selected="fileIds=$event" @uploading="resourceBusy=$event"/>
    <ConversationTools :conversation-id="resourceId" @saving="toolsSaving=$event" :busy="busy || switching || resourceBusy || detail?.task?.status==='running'"/>
    <details class="composer-tool chat-command-menu">
      <summary class="chat-command chat-icon-command" aria-label="会话操作" title="会话操作"><AppIcon name="more" :size="18"/></summary>
      <div class="composer-popover chat-command-panel">
        <template v-if="taskId">
          <button type="button" class="chat-menu-item" :disabled="commandsDisabled || detail?.task?.status==='paused' || detail?.task?.status==='ended'" @click="taskAction('cancel')"><AppIcon name="clock" :size="16"/>暂停</button>
          <button type="button" class="chat-menu-item" :disabled="commandsDisabled || detail?.task?.status==='ended'" @click="taskAction('end')"><AppIcon name="close" :size="16"/>结束任务</button>
          <button type="button" class="chat-menu-item danger" :disabled="commandsDisabled" @click="remove(`/agent-dialogues/${encodeURIComponent(taskId)}`,'删除任务记录？未完成任务将终止。','DELETE')"><AppIcon name="trash" :size="16"/>删除任务</button>
        </template>
        <button v-else type="button" class="chat-menu-item" :disabled="commandsDisabled" @click="remove(`/agent-sessions/${encodeURIComponent(current)}/clear`,'清空本方记录与记忆？')"><AppIcon name="rebuild" :size="16"/>清空本方记录与记忆</button>
        <button type="button" class="chat-menu-item danger" :disabled="commandsDisabled" @click="remove(`/agent-sessions/${encodeURIComponent(current)}`,'删除本方会话视图？','DELETE',true)"><AppIcon name="trash" :size="16"/>删除会话</button>
      </div>
    </details>
  </ComposerToolbar>
  <div class="peer-send-actions">
    <span v-if="writeState==='sending'" role="status">正在发送…</span>
    <button v-if="runningTask || (pausedTask && !message.trim())" type="button" class="button primary chat-command chat-send" :disabled="commandsDisabled" @click="taskAction('end')">停止<span aria-hidden="true">■</span></button>
    <button v-else type="button" class="button primary chat-command chat-send" :aria-busy="writeState==='sending'" :disabled="sendDisabled" @click="send">{{ pausedTask ? '补充并继续' : '发送' }}<AppIcon :name="writeState==='sending'?'clock':'up'" :size="15"/></button>
    <button v-if="detail?.task?.can_accept" type="button" class="button chat-command" :disabled="commandsDisabled" @click="taskAction('accept')">确认任务请求</button>
    <button v-if="detail?.task?.needs_confirmation" type="button" class="button chat-command" :disabled="commandsDisabled" @click="taskAction('confirm')">确认并补充意图</button>
  </div>
  </div>
</div></main></div></section></Transition></Teleport><IntentDialog v-if="intent" :intent="intent" @closed="intent=null" @sent="(session,task)=>{intent=null;enterSession(session,task)}"/><FilePreview v-if="filePreview?.download_url" :file="filePreview" :url="filePreview.download_url" @close="filePreview=null"/></template>
