<script setup lang="ts">
import { computed } from 'vue'
import type { ToolEvent } from '../../types'
import AppIcon from '../AppIcon.vue'

const props = defineProps<{ items?: ToolEvent[]; message?: string; tone?: string }>()
const notices = computed(() => {
  const rows = (props.items || []).filter(item => item.group === 'notice')
  const result = new Map(rows.map(item => [item.name, item.status]))
  if (props.message && !result.has(props.message)) result.set(props.message, props.tone || 'info')
  return result
})
</script>

<template>
  <div v-if="notices.size" class="chat-notices" role="status" aria-label="平台提示">
    <span v-for="[text, tone] in notices" :key="text" class="chat-notice-pill" :data-tone="tone">
      <AppIcon name="info" :size="13"/>{{ text }}
    </span>
  </div>
</template>

<style scoped>
.chat-notices { display:flex; flex-wrap:wrap; gap:.35rem; margin-block:.4rem .15rem; }
.chat-notice-pill { display:inline-flex; align-items:flex-start; gap:.35rem; padding:.22rem .55rem; border:1px solid var(--border); border-radius:.85rem; color:var(--text-secondary); background:var(--surface-alt); font-size:.75rem; line-height:1.4; white-space:normal; overflow-wrap:anywhere; }
.chat-notice-pill .app-icon { flex:none; }
.chat-notice-pill[data-tone=required] { color:var(--text); border-color:var(--accent); }
.chat-notice-pill[data-tone=error] { color:var(--danger); border-color:currentColor; }
</style>
