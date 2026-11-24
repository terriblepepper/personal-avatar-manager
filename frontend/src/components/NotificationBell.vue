<script setup lang="ts">
import { defineAsyncComponent, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { request } from '../api/client'
import { useNotificationInbox } from '../composables/useNotificationInbox'
import { useLivePolling } from '../composables/usePlatformUpdates'
import WindowControls from './WindowControls.vue'
import WindowClose from './WindowClose.vue'
const MessagesView = defineAsyncComponent(() => import('../views/MessagesView.vue'))
const inbox = useNotificationInbox(), unread = inbox.unread
const open = ref(false), route = useRoute()
const trigger = ref<HTMLButtonElement>(), panel = ref<HTMLElement>()
function close(restore = true) {
  open.value = false
  if (restore) trigger.value?.focus()
}
async function toggle() {
  if (open.value) { close(); return }
  open.value = true
  await nextTick()
  panel.value?.focus()
}
useLivePolling('inbox', async signal => {
  const update = inbox.readUnread()
  try {
    const value = await request<{unread:number}>('/notifications/unread-count', 'GET', undefined, signal)
    if (!signal.aborted) update(value.unread)
  } catch (error) {
    if (!signal.aborted && error instanceof Error && 'status' in error && [401,403].includes(Number(error.status))) update(0)
  }
}, () => 30000, {enabled: () => !open.value && inbox.lists.size === 0})

watch(() => route.fullPath, () => close(false))
</script>
<template>
  <button ref="trigger" id="message-hub-ball" class="message-hub-ball" type="button" aria-label="查看消息" :aria-expanded="open" @click="toggle">
    <span aria-hidden="true">✉</span><span>消息</span><span id="message-unread-badge" class="message-unread-badge" :hidden="!unread">{{ unread > 99 ? '99+' : unread }}</span>
  </button>
  <Transition name="avatar-pop"><section v-if="open" ref="panel" id="message-hub-window" class="message-hub-window avatar-window" role="dialog" aria-label="消息通知" tabindex="-1" @keydown.esc.stop.prevent="close()">
    <div class="cardhead window-header"><h2>消息</h2><div class="window-controls"><WindowControls anchor-bottom-right/><WindowClose label="关闭消息" @close="close()"/></div></div>
    <div id="message-hub-content"><MessagesView :management="false" @opened="close(false)"/></div>
  </section></Transition>
</template>
