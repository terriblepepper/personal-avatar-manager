<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElButton } from 'element-plus'
import { request, notifyError } from '../../api/client'
import { useLivePolling } from '../../composables/usePlatformUpdates'
import IntentDialog from './IntentDialog.vue'
import { requestId } from '../../utils/requestId'
interface Intent {id:string;conversation_id:string;target_query:string;target_aic:string;objective:string;shared_content:string;delivery_mode?:string}
const props=defineProps<{event:Record<string,unknown>}>(),emit=defineEmits<{opened:[sessionId:string,taskId:string]}>()
const live=ref<Record<string,unknown>>({}),busy=ref(false),intent=ref<Intent|null>(null)
let retryRequest=''
const value=computed(()=>({...props.event,...live.value})), string=(v:unknown)=>typeof v==='string'?v:''
const taskId=computed(()=>string(value.value.task_id)||string(value.value.id))
const state = computed(() => {
  const task = value.value
  if (task.rejected) return 'rejected'
  if (task.auto_retrying || task.status === 'awaiting_identity') return 'retrying'
  if (task.can_retry) return 'retry_timeout'
  if (task.sending_prepared && task.status === 'running') return 'sending'
  const status = string(task.status) || string(task.state) || 'submitted'
  if (status !== 'awaiting_input') return status
  if (typeof task.can_supplement === 'boolean') return task.can_supplement ? 'awaiting_self_input' : 'awaiting_peer_input'
  return task.needs_confirmation ? 'awaiting_self' : 'awaiting_other'
})
const labels:Record<string,string>={awaiting_self_input:'等待本人补充信息',awaiting_peer_input:'等待对方补充信息',awaiting_self:'等待本人确认',awaiting_other:'等待对方确认',ended:'任务已结束',awaiting_input:'等待用户',submitted:'请求已提交',sending:'正在发送',queued:'处理中',running:'分身正在回答',awaiting_acceptance:'等待对方确认',awaiting_identity:'正在自动重试',retrying:'正在自动重试',retry_timeout:'重试已超时',paused:'已暂停',failed:'处理失败',cancelled:'已取消',rejected:'任务已拒绝'}
useLivePolling('dialogue', async signal => {
  const result = await request<{task:Record<string,unknown>}>(`/agent-dialogues/${encodeURIComponent(taskId.value)}`, 'GET', undefined, signal)
  if (!signal.aborted) live.value = result.task
}, () => 5000, {enabled: () => !!taskId.value && !['ended','failed','cancelled','rejected'].includes(state.value)})
async function open(){if(!taskId.value)return;busy.value=true;try{const result=await request<{session_id:string}>(`/agent-dialogues/${encodeURIComponent(taskId.value)}`);emit('opened',result.session_id,taskId.value)}catch(e){notifyError(e)}finally{busy.value=false}}
async function retry(){if(!taskId.value||busy.value)return;retryRequest ||= requestId();busy.value=true;try{intent.value=await request<Intent>(`/agent-dialogues/${encodeURIComponent(taskId.value)}/retry-draft`,'POST',{request_id:retryRequest});retryRequest=''}catch(e){notifyError(e)}finally{busy.value=false}}
</script>
<template><section class="task-event-card" :class="event.kind==='start'?'task-event-start':'task-event-end'"><header><span class="task-event-badge">{{ event.mode==='message'?'消息':event.kind==='start'?'任务开始':'任务结束' }}</span><strong class="task-event-excerpt">{{ event.title || '分身任务' }}</strong></header><div v-if="event.summary" class="task-event-excerpt task-event-summary">{{ event.summary }}</div><div class="task-status peer-message-status" role="status">● {{ string(value.waiting_label) || labels[state] || state }} <span v-if="value.error || value.reason || value.waiting_reason || value.end_reason">· {{ value.error || value.reason || value.waiting_reason || value.end_reason }}</span><span v-if="state==='retrying'">；每 30 秒自动尝试，最多等待 5 分钟</span></div><ElButton v-if="taskId" :loading="busy" @click="open">查看{{ event.mode==='message'?'会话':'任务' }} ↗</ElButton><ElButton v-if="value.can_retry" :loading="busy" @click="retry">重新编辑并发送</ElButton><IntentDialog v-if="intent" :intent="intent" @closed="intent=null" @sent="(session,task)=>{intent=null;emit('opened',session,task)}"/></section></template>
