<script setup lang="ts">
import { computed, inject, ref } from 'vue'
import ModelCallDetails from './ModelCallDetails.vue'
import AppIcon from '../AppIcon.vue'
import type { ToolEvent } from '../../types'
import { date } from '../../utils/format'
import { workspaceDebugKey } from '../../composables/useWorkspaceGuide'

const props = defineProps<{ items: ToolEvent[]; pending?: string; modelProgress?: boolean; awaitingInput?: boolean }>()
const debug = inject(workspaceDebugKey, null)
const oldPhases: Record<string, string> = {
  感知: 'perceive', 推理: 'reason', 行动: 'act', 观察: 'observe', 完成检查: 'check', 最终输出: 'final',
}
const phase = (event: ToolEvent) => event.phase || oldPhases[event.name]
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const actions: Record<string, string> = {
  deliver_material: '准备交付资料原件',
  find_material: '检索授权原件目录',
  select_materials: '核验所选原件',
  send_material: '准备所选原件',
  search_materials: '混合检索资料正文',
  read_material: '读取文件原文',
  find_public_material: '检索公开原件',
  send_public_material: '准备公开原件',
  create_html: '生成 HTML',
  list_avatars: '查询公开分身目录',
  contact_avatar: '准备分身联络草稿',
  continue_answer: '确认由当前分身回答',
  require_conversation_tool: '检查所需工具是否开启',
  python: '规划 Python 沙箱操作',
  ask_user: '确认需要补充的信息',
  wait_for_user: '等待任务参与者补充信息',
  end_task: '提交任务结果',
  send_message: '提交本轮任务发言',
}
function sessionFileLabel(name: unknown, args?: unknown) {
  if (name !== 'read_session_file') return undefined
  const { file_id, query, file_access } = record(args)
  if (file_access === 'content' || typeof file_id === 'string') return '读取会话原件'
  if (file_access === 'directory' || typeof query === 'string') return '查找会话文件'
  return '会话文件'
}
function meaningfulLegacy(event: ToolEvent) {
  if (event.group !== 'agent_trace') return ['sandbox', 'memory', 'animation', 'files', 'reading'].includes(event.group || '')
  const input = record(event.input), output = record(event.output)
  return event.status === 'error' || phase(event) === 'final'
    || (phase(event) === 'act' && !!input.tool)
    || (phase(event) === 'reason' && (!!input.stage || !!input.operation || !!input.tools || input.tool === 'python'))
    || (phase(event) === 'check' && !!output.decision && output.decision !== '完成')
}
function modelName(event: ToolEvent) {
  const output = record(event.output)
  if (typeof output.reason === 'string' && output.reason) return event.name + ' · ' + output.reason
  const calls = Array.isArray(output.tool_calls) ? output.tool_calls : []
  const names = calls.map(call => {
    const { name, arguments: args } = record(call)
    return sessionFileLabel(name, args) || name
  }).filter(name => typeof name === 'string')
  const parts = [event.name]
  if (names.length) parts.push(names.join('、'))
  if (!names.length && output.kind === 'text' && typeof output.text === 'string') {
    const line = output.text.trim().split('\n', 1)[0]
    if (line && !/[<>{}`]/.test(line) && !line.startsWith('[')) parts.push(line.slice(0, 120))
  }
  if (event.status === 'running') {
    if (typeof output.received_bytes === 'number' && output.received_bytes > 0)
      parts.push('已接收 ' + output.received_bytes.toLocaleString() + ' 字节参数')
    else if (parts.length === 1 && typeof output.text_chars === 'number' && output.text_chars > 0)
      parts.push('已接收 ' + output.text_chars.toLocaleString() + ' 字')
  }
  return parts.join(' · ')
}
function activityName(event: ToolEvent) {
  if (event.group === 'model_io') return modelName(event)
  if (event.group === 'tool_io') {
    const result = record(record(event.output).result)
    const label = sessionFileLabel(event.name, record(event.input).arguments) || actions[event.name] || event.name
    return typeof result.filename === 'string' ? label + ' · ' + result.filename : label
  }
  if (event.group !== 'agent_trace') return event.steps?.at(-1)?.message || event.name
  const input = record(event.input), output = record(event.output)
  if (phase(event) === 'final') return '整理回答与附件'
  if (phase(event) === 'act') {
    if (input.tool === 'find_material' && Array.isArray(input.queries)) return `检索 ${input.queries.length} 个资料目标`
    if (input.tool === 'select_materials' && Array.isArray(output.files)) return `核验 ${output.files.length} 份原件`
    const name = sessionFileLabel(input.tool, input) || actions[String(input.tool)] || String(input.tool || event.name)
    const detail = output.reason || output.issue || output.filename
    if (typeof detail === 'string') return name + ' · ' + detail
    return typeof input.query === 'string' && input.query ? `${name}「${input.query}」` : name
  }
  if (input.operation === 'check_file_receipt') {
    if (output.decision === 'html') return '核对后补做 HTML'
    if (output.decision === 'webpage') return '核对后补做网页'
    if (output.decision === '2d' || output.decision === '3d') return '核对后补做动画'
    return output.decision === 'plain_answer' ? '已确认本轮可用文字回答' : '核对本轮文件交付'
  }
  if (input.stage === 'route' || input.stage === 'resume_route') {
    if (output.action === 'deliver_material') return '识别索件意图并进入原件交付'
    if (output.mode === 'search') return '确认先检索正文再回答'
    if (output.mode === 'read') return '确认先读取原文再回答'
    return sessionFileLabel(output.action) || actions[String(output.action)] || (input.stage === 'resume_route' ? '判断如何继续当前任务' : '识别本轮目标与所需工具')
  }
  if (input.operation === 'select_materials' || Array.isArray(input.tools) && input.tools.includes('select_materials')) {
    if (typeof output.issue === 'string') return `核对原件选择 · ${output.issue}`
    if (Array.isArray(output.files)) return `从候选中选出 ${output.files.length} 份原件`
    if (output.tool === 'find_material') return '调整查询以补齐缺少的候选'
    if (output.tool === 'clarify_material') return '指出尚需明确的资料条件'
    return '比对文件说明、路径和正文证据'
  }
  if (phase(event) === 'reason' && typeof input.iteration === 'number') {
    const goal = typeof input.goal_no === 'number' ? `目标 ${input.goal_no}` : '本轮答复'
    if (event.status === 'error' && typeof output.reason === 'string') return `${goal} · ${output.reason}`
    if (input.mode === '结构化检索参数') return `为${goal}修正检索参数`
    if (input.mode === '计划中的检索参数') return `采用${goal}已规划的检索参数`
    if (event.status === 'running') return `正在为${goal}选择下一步`
  }
  if (input.tool === 'python') return actions.python
  if (Array.isArray(output.plan)) return output.plan.length ? `确定 ${output.plan.length} 项待完成目标` : '确认已有信息足以回答'
  if (output.decision === 'search_materials') return '确认需要检索授权资料'
  if (output.decision === 'inspect_originals') return '确认需要读取原文'
  if (output.decision === 'finish_reading') return '确认当前资料已足够'
  const tools = Array.isArray(output.tool_names) ? output.tool_names : []
  if (tools.length) return sessionFileLabel(tools[0]) || actions[String(tools[0])] || '选择本轮工具'
  if (output.finish_reason) return '组织已有信息并生成答复'
  if (Array.isArray(input.tools) && input.tools.includes('read_material')) return '确定需要读取的文件与范围'
  if (typeof output.planned_steps === 'number') return '已规划工具步骤 · ' + output.planned_steps + ' 项'
  const detail = output.issue || output.reason || output.decision || input.operation || input.mode
  return typeof detail === 'string' ? detail : event.name
}
const hasModel = computed(() => props.items.some(item => item.group === 'model_io'))
const activities = computed(() => {
  const hasLegacyTrace = !hasModel.value && debug?.value && props.items.some(row => row.group === 'agent_trace')
  const latest = new Map<string, ToolEvent>()
  for (const item of props.items) {
    if (item.group === 'notice' || item.group === 'agent_receipt') continue
    if (item.group === 'tool_io') {
      latest.set(item.id, item)
      continue
    }
    if (hasModel.value) {
      if (item.group === 'agent_trace') {
        if (phase(item) !== 'act' || !record(item.input).tool) continue
      } else if (!['model_io', 'sandbox', 'memory', 'files', 'reading'].includes(item.group || '')) continue
    } else if (hasLegacyTrace ? !meaningfulLegacy(item) : item.group === 'agent_trace') continue
    latest.set(item.id, item)
  }
  const items: ToolEvent[] = []
  for (const item of latest.values()) {
    const last = items.at(-1)
    // New receipts supersede the adjacent legacy acknowledgement of the same tool.
    if (item.group === 'tool_io' && last?.group === 'agent_trace' && phase(last) === 'act' &&
        last.status === 'completed' && record(last.input).tool === item.name) items.pop()
    items.push(item)
  }
  const last = items.at(-1)
  // Older waiting snapshots marked their active step interrupted or still running.
  if (props.awaitingInput && last && (last.status === 'running' || (!hasModel.value && last.status === 'interrupted')))
    items[items.length - 1] = { ...last, status: 'paused' }
  return items
})
const currentStatus = computed(() => {
  if (!props.pending || props.awaitingInput) return ''
  if (props.modelProgress && !hasModel.value) return props.pending
  const active = activities.value.filter(item => item.status === 'running').at(-1)
  return active ? activityName(active) : props.pending
})
const expanded = ref(new Set<string>())
const panelOpen = ref(!!props.pending)
function toggleItem(id: string, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) expanded.value.add(id)
  else expanded.value.delete(id)
}
const sandboxCode = (event: ToolEvent) => {
  const code = record(event.input).code
  return event.group === 'sandbox' && typeof code === 'string' ? code : ''
}
function inputDetails(event: ToolEvent) {
  if (!sandboxCode(event)) return event.input
  const { code, ...fields } = record(event.input)
  return fields
}
const states: Record<string, string> = {
  running: '进行中', paused: '已暂停', completed: '已完成', error: '失败', interrupted: '已中止', skipped: '已跳过',
}
function stateLabel(item: ToolEvent) {
  if (item.group === 'model_io' && item.status === 'completed' && record(item.output).finish_reason === 'output_limit') return '输出截断'
  return states[item.status] || item.status
}
const text = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
</script>

<template>
  <details v-if="activities.length || pending || $slots.default" class="tool-activities" :open="!!pending" @toggle.self="panelOpen = ($event.currentTarget as HTMLDetailsElement).open">
    <summary class="activity-row" :class="{ 'has-live-status': currentStatus }">
      <AppIcon name="chevron" :size="12"/>
      <span v-if="currentStatus" class="activity-name activity-live" :title="currentStatus">{{ currentStatus }}</span>
      <span class="activity-heading">执行过程<span v-if="activities.length"> · {{ activities.length }} 项</span></span>
    </summary>
    <p v-if="pending && (!activities.length || (modelProgress && !hasModel))" class="activity-row" data-status="running">
      <span class="activity-dot" aria-hidden="true"/>{{ pending }}
    </p>
    <details v-for="item in activities" :key="item.id" class="tool-activity" :data-status="item.status" @toggle.self="toggleItem(item.id, $event)">
      <summary class="activity-row">
        <AppIcon name="chevron" :size="12"/>
        <span class="activity-dot" aria-hidden="true"/>
        <span class="activity-name" :title="activityName(item)">{{ activityName(item) }}</span>
        <template v-if="item.group !== 'result' || item.status !== 'running'">
          <span class="activity-state">{{ stateLabel(item) }}</span>
        </template>
      </summary>
      <div v-if="!item.group || (panelOpen && expanded.has(item.id))" class="activity-content">
        <ModelCallDetails v-if="item.group === 'model_io'" :input="record(item.input)" :output="record(item.output)" :debug="!!debug"/>
        <template v-else-if="item.group === 'tool_io'">
          <template v-if="debug && record(item.input).arguments != null">
            <strong>工具输入</strong><pre>{{ text(record(item.input).arguments) }}</pre>
          </template>
          <template v-if="record(item.output).result != null">
            <strong>工具返回</strong><pre>{{ text(record(item.output).result) }}</pre>
          </template>
        </template>
        <template v-else>
          <template v-if="item.input != null && (item.group !== 'agent_trace' || debug)">
            <strong v-if="debug">输入</strong>
            <pre>{{ text(inputDetails(item)) }}</pre>
            <details v-if="sandboxCode(item)" class="sandbox-source">
              <summary>沙箱代码</summary>
              <pre><code>{{ sandboxCode(item) }}</code></pre>
            </details>
          </template>
          <ol v-if="item.steps?.length">
            <li v-for="(step, index) in item.steps" :key="index">
              <time v-if="step.time" :datetime="step.time">{{ date(step.time) }}</time> {{ step.message }}
            </li>
          </ol>
          <template v-if="item.output != null && (item.group !== 'agent_trace' || debug)">
            <strong v-if="debug">输出</strong>
            <pre>{{ text(item.output) }}</pre>
          </template>
        </template>
      </div>
    </details>
    <slot/>
  </details>
</template>

<style scoped>
.tool-activities { font-size: .75rem; line-height: 1.4; color: var(--text-secondary); white-space: normal; }
.tool-activity { min-width: 0; }
.activity-row {
  display: flex;
  align-items: center;
  gap: .35rem;
  min-block-size: 1.4rem;
  padding: .08rem .18rem;
  margin: 0;
  list-style: none;
  border-radius: .25rem;
}
summary.activity-row { cursor: pointer; }
summary.activity-row:hover, summary.activity-row:focus-visible { background: var(--ui-hover); }
.activity-row::-webkit-details-marker { display: none; }
.activity-row .app-icon { flex: none; }
.tool-activities[open] > summary .app-icon, .tool-activity[open] > summary .app-icon { transform: rotate(90deg); }
.activity-dot { inline-size: .32rem; block-size: .32rem; flex: none; border-radius: 50%; background: currentColor; }
[data-status=running] .activity-dot { color: var(--accent); }
[data-status=error] .activity-dot, [data-status=interrupted] .activity-dot { color: var(--danger); }
.activity-name { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.activity-name { color: var(--text); }
.activity-state { flex: none; }
.tool-activities[open] > summary .activity-live,
.tool-activities:not([open]) > .has-live-status .activity-heading { display: none; }
.activity-content {
  border-inline-start: 1px solid var(--border);
  margin: .1rem 0 .25rem .54rem;
  padding-inline: .65rem .25rem;
  overflow-wrap: anywhere;
}
.activity-content ol { margin: .2rem 0; padding-inline-start: 1.1rem; }
.activity-content pre { margin: .2rem 0; font: inherit; white-space: pre-wrap; max-block-size: 12rem; overflow: auto; }
.activity-content time { font-variant-numeric: tabular-nums; }
@media (pointer: coarse) { .activity-row { min-block-size: 2rem; } }
</style>
