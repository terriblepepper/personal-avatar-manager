<script setup lang="ts">
import type { ChatFile, ChatMessage } from '../../types'
import ToolActivity from './ToolActivity.vue'
import ChatNotices from './ChatNotices.vue'
import ChatMarkdown from './ChatMarkdown.vue'
import ChatIntent from '../collaboration/ChatIntent.vue'
import TaskEventCard from '../collaboration/TaskEventCard.vue'

defineProps<{ message: ChatMessage; conversationId: string }>()
const emit = defineEmits<{ opened: [session: string, task?: string]; updated: []; preview: [file: ChatFile] }>()
</script>

<template>
  <article :data-chat-message="message.id" :data-history-message="message.id" class="bubble" data-copy-message tabindex="0"
    aria-haspopup="menu"
    :class="{user:message.role==='user','intent-message':!!message.intent,'task-event-bubble':!!message.task_event?.kind}">
    <ToolActivity :items="message.tool_events||[]">
      <template v-if="message.used_skills?.length" #default>
        <small class="stream-note">本轮技能：{{ message.used_skills.map(item=>item.name).join('、') }}</small>
      </template>
    </ToolActivity>
    <small v-if="message.responder_name" class="reply-source">{{ message.responder_name }}</small>
    <TaskEventCard v-if="message.task_event?.kind" :event="message.task_event"
      @opened="(id, task) => emit('opened', id, task)" />
    <ChatIntent v-else-if="message.intent" :intent="message.intent" @updated="emit('updated')"
      @opened="(id, task) => emit('opened', id, task)" />
    <ChatMarkdown v-else :artifact-reply="message.role !== 'user'" :source="message.content" :events="message.tool_events"
      :attachments="message.attachments" :conversation-id="conversationId" @preview="emit('preview', $event)" />
    <ChatNotices :items="message.tool_events" />
  </article>
</template>
