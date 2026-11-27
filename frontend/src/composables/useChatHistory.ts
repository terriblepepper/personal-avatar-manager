import { nextTick, onUnmounted, ref, type Ref } from 'vue'
import { notifyError, request } from '../api/client'
import type { ChatMessage } from '../types'
import { useHistoryScroll } from './useHistoryScroll'

export function useChatHistory(conversationId: Ref<string>, loading: Ref<boolean>) {
  const log = ref<HTMLElement>()
  const messages = ref<ChatMessage[]>([])
  const loadingOlder = ref(false), hasOlder = ref(false)
  let olderRequest: AbortController | undefined
  const {historyEvents, captureAnchor} = useHistoryScroll(log, older)

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

  async function load(id: string, signal: AbortSignal, beforeReplace: () => void) {
    const rows = await request<ChatMessage[]>('/chat?'+new URLSearchParams({personal:'true',channel:'assistant',limit:'20',conversation_id:id}),'GET',undefined,signal)
    if (signal.aborted || id !== conversationId.value) return
    const tail = new Set(rows.map(row=>row.id))
    const first = rows[0]
    const retained = first && messages.value.some(row=>tail.has(row.id))
      ? messages.value.filter(row=>row.id<first.id) : []
    const follow = !messages.value.length || nearBottom()
    const restore = follow ? undefined : captureAnchor()
    // Replace the live bubble and its persisted copy in the same render.
    beforeReplace()
    messages.value = [...retained,...rows]
    if (!retained.length) hasOlder.value = rows.length===20
    await nextTick()
    if (signal.aborted || id!==conversationId.value) return
    if (follow) await scrollBottom(true)
    else restore?.()
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

  function cancelOlder() {
    olderRequest?.abort()
    olderRequest = undefined
    loadingOlder.value = false
  }

  function reset() {
    cancelOlder()
    messages.value = []
    hasOlder.value = false
  }

  onUnmounted(cancelOlder)
  return { messages, log, loadingOlder, historyEvents, scrollBottom, load, cancelOlder, reset }
}
