import { computed, nextTick, onBeforeUnmount, ref, type Ref } from 'vue'
import { request, notifyError } from '../api/client'
import { useLivePolling } from './usePlatformUpdates'
import { useHistoryScroll } from './useHistoryScroll'
import type { Detail, Message, Session, Task } from './dialogueTypes'
import type { Page } from '../types'

export function useDialogueSession(transcript: Ref<HTMLElement | undefined>, clearPreview: () => void, denied: () => void) {
  const sessions=ref<Session[]>([]), current=ref(''), taskId=ref(''), detail=ref<Detail|null>(null)
  const search=ref(''), offset=ref(0), total=ref(0), switching=ref(false), shownTask=ref('')
  const older=ref<Message[]>([])
  let olderRequest: AbortController | undefined
  const {historyEvents, captureAnchor} = useHistoryScroll(transcript, loadOlder)

  function cancelHistory() { olderRequest?.abort() }
  onBeforeUnmount(cancelHistory)

  const taskMap=ref<Record<string,Task>>({})
  const taskSearch=ref(''),taskOffset=ref(0),taskTotal=ref(0)
  const hasOlder=ref(false), allMessages=computed(()=>[...new Map([...older.value,...(detail.value?.messages||[])].map(item=>[item.turn,item])).values()].filter(item=>!(item.speaker_role==='user'&&item.speaker_name==='用户授权共享资料')).sort((a,b)=>a.turn-b.turn))

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
    if (closePreview) clearPreview()
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
      const page = value.task_page
      const log = transcript.value
      const atBottom = log && log.scrollHeight - log.clientHeight - log.scrollTop <= 2
      if (value.task?.workspace_expired) clearPreview()
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
        if (session) denied()
      } else {
        if (switching.value && detail.value) taskId.value = shownTask.value
        switching.value = false
      }
      notifyError(error)
    }
  }
  const polling = useLivePolling('dialogue', refresh, () => {
    const tasks = [detail.value?.task || detail.value?.chat,...(detail.value?.tasks || [])]
    return tasks.some(t => t && (t.auto_retrying || ['queued','running','awaiting_identity'].includes(t.status))) ? 1500 : 30000
  })
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
        if (isDenied(error)) denied()
        notifyError(error)
      }
    } finally { if (olderRequest === controller) olderRequest = undefined }
  }

  return { sessions,current,taskId,detail,search,offset,total,taskMap,taskSearch,taskOffset,taskTotal,
    switching,shownTask,allMessages,historyEvents,resetHistory,cancelHistory,polling }
}
