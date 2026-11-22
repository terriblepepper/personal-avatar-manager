<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ input: Record<string, unknown>; output: Record<string, unknown>; debug: boolean }>()
const tools = computed(() => Array.isArray(props.input.tools) ? props.input.tools : [])
const calls = computed(() => Array.isArray(props.output.tool_calls) ? props.output.tool_calls : [])
const format = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
</script>

<template>
  <dl class="model-call-details">
    <template v-if="input.message"><dt>模型输入摘录</dt><dd><pre>{{ input.message }}</pre></dd></template>
    <template v-if="debug && Array.isArray(input.tools)">
      <dt>本轮提供的工具</dt><dd>{{ tools.join('、') || '无' }}</dd>
    </template>
    <template v-if="debug && typeof input.history_turns === 'number'"><dt>历史消息数</dt><dd>{{ input.history_turns }}</dd></template>
    <template v-if="output.text">
      <dt>模型输出摘录</dt>
      <dd>
        <details v-if="output.kind !== 'text'"><summary>{{ output.kind === 'artifact' ? '查看作品片段' : '查看返回文本' }}</summary><pre>{{ output.text }}</pre></details>
        <pre v-else>{{ output.text }}</pre>
      </dd>
    </template>
    <template v-if="calls.length">
      <dt>模型请求调用</dt>
      <dd v-for="(call, index) in calls" :key="index">
        <details v-if="debug && call?.arguments != null">
          <summary>{{ call.name }} · 调用参数</summary><pre>{{ format(call.arguments) }}</pre>
        </details>
        <template v-else>{{ call?.name }}</template>
      </dd>
    </template>
    <template v-if="output.reason"><dt>原因</dt><dd>{{ output.reason }}</dd></template>
    <template v-if="typeof output.text_chars === 'number' && output.text_chars > 0">
      <dt>已接收文本</dt><dd>{{ output.text_chars.toLocaleString() }} 字</dd>
    </template>
    <template v-if="typeof output.received_bytes === 'number' && output.received_bytes > 0">
      <dt>已接收调用参数</dt><dd>{{ output.received_bytes.toLocaleString() }} 字节</dd>
    </template>
    <template v-if="debug && (output.attempt || input.attempt)"><dt>尝试次数</dt><dd>{{ output.attempt || input.attempt }}</dd></template>
    <template v-if="debug && output.finish_reason"><dt>模型结束标记</dt><dd>{{ output.finish_reason }}</dd></template>
  </dl>
</template>

<style scoped>
.model-call-details { margin: 0; }
dt { color: var(--text); font-weight: 600; margin-top: .32rem; }
dd, pre { margin: 0; }
pre { font: inherit; white-space: pre-wrap; max-block-size: 14rem; overflow: auto; }
details pre { font-family: var(--font-code); }
summary { cursor: pointer; }
</style>
