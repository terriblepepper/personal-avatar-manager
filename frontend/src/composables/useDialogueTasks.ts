import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { request, notifyError } from '../api/client'
import { requestId } from '../utils/requestId'
import type { Detail, Intent, Task } from './dialogueTypes'

interface TaskActions {
  current: Ref<string>
  detail: Ref<Detail | null>
  switching: Ref<boolean>
  message: Ref<string>
  scope: () => number
  action: (path: string, method: string, body: unknown, onSuccess: () => void) => Promise<void>
}

export function useDialogueTasks({ current, detail, switching, message, scope, action }: TaskActions) {
  const rounds=ref<number>(), intent=ref<Intent|null>(null), drafting=ref(false), draftRequest=ref('')
  const controller = new AbortController()

  onMounted(async () => {
    try {
      const data = await request<{max_rounds:number}>('/agent-dialogue-settings', 'GET', undefined, controller.signal)
      if (!controller.signal.aborted) rounds.value = data.max_rounds
    } catch (error) { if (!controller.signal.aborted) notifyError(error) }
  })
  onBeforeUnmount(() => controller.abort())

  function resetDraft() { drafting.value=false; intent.value=null; draftRequest.value='' }

  function saveRounds() {
    return action('/agent-dialogue-settings','PUT',{max_rounds:rounds.value}, () => ElMessage.success('自动聊天轮次设置已保存'))
  }
  function toggleAutoAccept() {
    if (!current.value || !detail.value) return
    const enabled = !detail.value.auto_accept
    return action('/agent-sessions/'+encodeURIComponent(current.value)+'/auto-accept','PUT',{enabled}, () => {
      if (detail.value) detail.value.auto_accept=enabled
    })
  }
  async function loadDraft(path:string, body:Record<string,string>) {
    if (switching.value || drafting.value || intent.value) return
    const ticket = scope()
    drafting.value = true
    draftRequest.value ||= requestId()
    try {
      const value = await request<Intent>(path,'POST',{...body,request_id:draftRequest.value})
      if (ticket === scope()) { draftRequest.value=''; intent.value=value }
    } catch (error) {
      if (ticket === scope()) notifyError(error)
    } finally {
      if (ticket === scope()) drafting.value=false
    }
  }
  function retry(task:Task) { return loadDraft('/agent-dialogues/'+encodeURIComponent(task.id)+'/retry-draft',{}) }
  function draft() {
    if (current.value) return loadDraft('/agent-sessions/'+encodeURIComponent(current.value)+'/task-draft',{message:message.value.trim()})
  }

  return { rounds, intent, drafting, saveRounds, toggleAutoAccept, retry, draft, resetDraft }
}
