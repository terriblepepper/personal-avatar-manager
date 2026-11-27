<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import AppDialog from '../AppDialog.vue'
import { chatTextFits } from '../../utils/chatTextBudget'
import type { AgentQuestion, QuestionReply } from '../../types'
import { requestId } from '../../utils/requestId'

const props = defineProps<{ question?: AgentQuestion; scope: string; disabled?: boolean }>()
const emit = defineEmits<{ answer: [value: QuestionReply] }>()
const opened = ref(false), answer = ref('')
const trigger = ref<HTMLButtonElement>(), field = ref<HTMLTextAreaElement>()
const descriptionId = useId()
const fits = computed(() => chatTextFits(answer.value))
const options = computed(() => [...new Set(props.question?.options || [])])
let seen = '', currentDraft = '', previousReply: QuestionReply | undefined

const key = () => props.scope + '/' + (props.question?.id || '')
const preference = () => 'avatar:deferred-question:' + props.scope
function open() {
  if (!props.question || props.disabled) return
  seen = key()
  try { sessionStorage.removeItem(preference()) } catch { /* Session storage is optional. */ }
  opened.value = true
}
function autoOpen() {
  if (!props.question || props.disabled || seen === key() || document.hidden || !document.hasFocus()) return
  try { if (sessionStorage.getItem(preference()) === props.question.id) return } catch { /* Session storage is optional. */ }
  open()
}
function defer() {
  if (!props.question || !opened.value) return
  opened.value = false
  try { sessionStorage.setItem(preference(), props.question.id) } catch { /* seen also suppresses the current visit. */ }
  void nextTick(() => trigger.value?.focus({preventScroll:true}))
}
function submit() {
  const q = props.question, message = answer.value.trim()
  if (!q || !opened.value || props.disabled || !message || !fits.value) return
  if (previousReply?.question_id !== q.id || previousReply.message !== message)
    previousReply = { question_id:q.id, message, request_id:requestId() }
  emit('answer', previousReply)
}
function focusAnswer() {
  if (opened.value && document.hasFocus()) field.value?.focus({preventScroll:true})
}
watch([() => props.scope, () => props.question?.id, () => props.disabled], ([scope, id], [oldScope, oldId]) => {
  if (scope !== oldScope || id !== oldId) {
    opened.value = false
    seen = ''
    if (id && key() !== currentDraft) {
      currentDraft = key()
      answer.value = ''
      previousReply = undefined
    }
  }
  autoOpen()
}, {flush:'post'})
onMounted(() => {
  currentDraft = props.question ? key() : ''
  document.addEventListener('visibilitychange', autoOpen)
  window.addEventListener('focus', autoOpen)
  autoOpen()
})
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', autoOpen)
  window.removeEventListener('focus', autoOpen)
})
defineExpose({open})
</script>

<template>
  <div v-if="question" class="question-entry">
    <span class="question-preview">{{ question.question }}</span>
    <button ref="trigger" type="button" class="button small" :disabled="disabled" @click="open">回答分身提问</button>
  </div>
  <AppDialog v-if="question" :key="question.id" :model-value="opened" title="分身提问" width="min(30rem, calc(100vw - 1.5rem))"
    :close-on-click-modal="false" @open-auto-focus="focusAnswer" @close="defer">
    <form class="agent-question" @submit.prevent="submit">
      <p :id="descriptionId" class="question-text">{{ question.question }}</p>
      <div v-if="options.length" class="question-options" role="group" aria-label="建议回答">
        <button v-for="option in options" :key="option" type="button" class="button small" :disabled="disabled"
          :aria-pressed="answer === option" @click="answer = option">{{ option }}</button>
      </div>
      <label class="field">你的回答<textarea ref="field" v-model="answer" class="form-control" rows="3" placeholder=" "
        :aria-describedby="descriptionId" :aria-invalid="!fits" :disabled="disabled" @keydown.ctrl.enter.prevent="submit" @keydown.meta.enter.prevent="submit"/></label>
      <p v-if="!fits" class="notice error" role="alert">回答超过 65,536 字节，请缩短后提交。</p>
      <div class="question-actions">
        <button type="button" class="button" @click="defer">稍后回答</button>
        <button type="submit" class="button primary" :disabled="disabled || !answer.trim() || !fits">提交并继续</button>
      </div>
    </form>
  </AppDialog>
</template>

<style scoped>
.question-entry { display:flex; align-items:center; gap:.65rem; min-width:0; margin:.35rem 0; }
.question-entry button { flex:none; }
.question-preview { color:var(--text-secondary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.agent-question { display:grid; gap:.9rem; min-width:0; }
.question-text { margin:0; max-height:30vh; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text); }
.question-options,.question-actions { display:flex; flex-wrap:wrap; gap:.45rem; }
.question-options .button { max-width:100%; text-align:start; white-space:normal; overflow-wrap:anywhere; border-radius:1rem; }
.question-options .button[aria-pressed=true] { color:var(--accent); border-color:currentColor; }
.question-actions { justify-content:flex-end; }
</style>
