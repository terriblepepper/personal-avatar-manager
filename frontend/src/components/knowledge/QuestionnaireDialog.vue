<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { ElButton, ElInput, ElRadio, ElRadioGroup, ElMessage } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import { request, notifyError } from '../../api/client'
import type { Answer, Build } from './types'
const props = defineProps<{ build: Build | null }>()
const emit = defineEmits<{ close: []; submitted: [] }>()
const answers = ref<Record<string,{choice:string;other:string}>>({})
const revision = ref('')
const saving = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let queue = Promise.resolve(true)
let generation = 0
let finished = false
watch(() => props.build, item => { if (item) { generation++; finished = false; revision.value = item.updated_at; answers.value = Object.fromEntries(Object.entries(item.bank_answers || {}).map(([id,value])=>[id,{...value}])) } }, { immediate: true })
function save(submit: boolean) {
  clearTimeout(timer)
  const item = props.build
  if (!item) return Promise.resolve(false)
  const current = generation
  const payload: Answer[] = Object.entries(answers.value).filter(([,v]) => v.choice).map(([id,v]) => ({ id, choice:v.choice, other:v.choice === 'E' ? v.other : '' }))
  queue = queue.then(async () => {
    if (current !== generation) return false
    saving.value = true
    try {
      const result = await request<{revision:string}>(`/me/knowledge/builds/${encodeURIComponent(item.id)}/questionnaire`, 'POST', { answers:payload, revision:revision.value, submit })
      if (current !== generation) return false
      revision.value = result.revision
      if (submit) { finished = true; ElMessage.success('题库已完成'); emit('submitted'); emit('close') }
      return true
    } catch (error) { notifyError(error); return false } finally { saving.value = false }
  })
  return queue
}
function changed() { clearTimeout(timer); timer = setTimeout(() => { void save(false) }, 800) }
async function close() { if (await save(false)) { finished=true; emit('close') } }
async function submit() {
  if (!props.build || props.build.question_plan.some(q => !answers.value[q.id]?.choice || (answers.value[q.id]?.choice === 'E' && !answers.value[q.id]?.other?.trim()))) { ElMessage.warning('请完成所有题目'); return }
  await save(true)
}
onBeforeUnmount(() => { clearTimeout(timer); if (props.build && !finished) void save(false) })
</script>
<template>
  <AppDialog :model-value="!!build" title="分身风格题库" :close-on-click-modal="false" :before-close="close">
    <p class="sub">按实际情况选择，草稿自动保存。</p>
    <fieldset v-for="q in build?.question_plan || []" :key="q.id" class="soul-choice-question">
      <legend>{{ q.category_name }} · {{ q.category_index }}. {{ q.question }}</legend>
      <ElRadioGroup :model-value="answers[q.id]?.choice" @update:model-value="value => { answers[q.id] = { choice: String(value), other: answers[q.id]?.other || '' }; changed() }">
        <ElRadio v-for="(label, code) in {A:'完全符合',B:'比较符合',C:'比较不符合',D:'完全不符合',E:'其他'}" :key="code" :value="code">{{ code }}：{{ label }}</ElRadio>
      </ElRadioGroup>
      <ElInput placeholder=" " v-if="answers[q.id]?.choice === 'E'" v-model="answers[q.id].other" type="textarea" maxlength="4000" @input="changed" />
    </fieldset>
    <template #footer><ElButton :loading="saving" @click="close">暂存并关闭</ElButton><ElButton type="primary" :loading="saving" @click="submit">提交全部答案</ElButton></template>
  </AppDialog>
</template>
