<script setup lang="ts">
import AppIcon from '../AppIcon.vue'
import RecordPagination from '../RecordPagination.vue'
import type { Task } from '../../composables/dialogueTypes'

defineProps<{
  open: boolean
  taskId: string
  tasks?: Task[]
  drafting: boolean
  disabled: boolean
  autoAccept?: boolean
  autoAcceptAvailable?: boolean
  taskOffset: number
  taskTotal: number
  taskSearch: string
}>()
defineEmits<{
  select: [id: string]
  draft: []
  remove: []
  toggle: []
  page: [offset: number]
  search: [query: string]
}>()
const taskStatuses: Record<string,string> = { queued:'等待处理', running:'进行中', awaiting_acceptance:'等待接收', awaiting_input:'等待输入', paused:'已暂停', ended:'已结束', failed:'失败' }
</script>

<template>
<aside :inert="!open" :aria-hidden="!open" id="peer-task-navigation" class="peer-task-nav" aria-label="任务侧栏">
  <div class="peer-task-actions">
    <button type="button" class="button small" :aria-current="!taskId ? 'page' : undefined" @click="$emit('select', '')">主会话</button>
    <button type="button" class="button small primary" :disabled="drafting" @click="$emit('draft')">{{ drafting ? '正在打开…' : '＋ 创建任务' }}</button>
    <button type="button" class="button small danger peer-delete-records" aria-label="删除全部任务记录" title="删除全部任务记录" @click="$emit('remove')"><AppIcon name="trash" :size="14"/><span>删除记录</span></button>
  </div>

  <button type="button" class="button small peer-auto-accept" role="switch"
    aria-label="自动同意对方发来的任务" :aria-checked="!!autoAccept"
    :disabled="disabled || (!autoAccept && !autoAcceptAvailable)"
    @click="$emit('toggle')">
    <span>自动同意对方发来的任务</span>
    <small>{{ autoAccept ? '已开启' : '已关闭' }}</small>
  </button>
  <p v-if="autoAcceptAvailable === false" class="sub peer-auto-accept-hint">发送一条消息或任务后可开启。</p>

  <div class="peer-task-list">
    <button v-for="task in tasks || []" :key="task.id" type="button" :aria-current="task.id===taskId" :title="task.objective" @click="$emit('select', task.id)">
      <strong>{{ task.objective }}</strong><small>{{ task.rejected ? '已拒绝' : taskStatuses[task.status] || task.status }}</small>
    </button>
    <p v-if="tasks && !tasks.length" class="sub">暂无任务</p>
  </div>
  <RecordPagination class="peer-task-paging" :offset="taskOffset" :total="taskTotal" :search="taskSearch" label="任务分页" @update:offset="$emit('page', $event)" @update:search="$emit('search', $event)"/>
</aside>
</template>
