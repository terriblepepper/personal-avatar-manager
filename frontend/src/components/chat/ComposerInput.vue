<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, useId, watch } from 'vue'
import { chatTextFits } from '../../chatTextBudget'
import { ElTooltip } from 'element-plus'
import { request } from '../../api/client'

const props = defineProps<{
  modelValue: string; label: string; placeholder: string;
  disabled?: boolean; submitOnEnter?: boolean; usageUrl?: string; usageRevision?: string | number;
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; submit: [] }>()
const field = ref<HTMLTextAreaElement>()
const compact = computed(() => /^\/compact(?:\s|$)/.test(props.modelValue))
const editable = computed(() => compact.value ? props.modelValue.slice(8).replace(/^ /,'') : props.modelValue)
function removeCompact() {
  emit('update:modelValue',editable.value)
  void nextTick(() => {field.value?.focus(); field.value?.setSelectionRange(0,0)})
}
const slashOpen = ref(false)
const usage = ref<number | '' | '…' | '占用未知'>('')
const usageText = computed(() => {
  const percent = usage.value
  if (typeof percent !== 'number') return percent
  return percent > 0 && percent < 0.1 ? '<0.1%' : Number(percent.toFixed(1))+'%'
})
const usageRing = computed(() => typeof usage.value === 'number'
  ? { '--usage-fill': Math.min(100, usage.value) + '%' } : undefined)
const usageTitle = '当前上下文占用估算，含基础提示、摘要与未压缩消息'
watch([() => (compact.value || slashOpen.value) ? props.usageUrl : '', () => props.usageRevision], async ([url], _, onCleanup) => {
  usage.value = ''
  if (typeof url !== 'string' || !url) return
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  usage.value = '…'
  try {
    const {used_tokens, context_tokens} = await request<{used_tokens:number;context_tokens:number}>(url,'GET',undefined,controller.signal)
    if (controller.signal.aborted) return
    if (!Number.isFinite(used_tokens) || used_tokens < 0 || !Number.isFinite(context_tokens) || context_tokens <= 0) throw new Error()
    usage.value = 100 * used_tokens / context_tokens
  } catch { if (!controller.signal.aborted) usage.value = '占用未知' }
}, {immediate:true})
const compactDescription = '压缩当前会话记忆，可指定保留重点'
const slashDescription = shallowRef<HTMLElement>()
const slashHelp = shallowRef(false)
function showSlashHelp() {
  const description = slashDescription.value
  slashHelp.value = !!description && description.scrollWidth > description.clientWidth
}
watch(slashOpen, open => { if (!open) slashHelp.value = false })
function chooseCompact() {
  slashOpen.value=false
  emit('update:modelValue','/compact')
  void nextTick(() => field.value?.focus())
}
function closePicker() { slashOpen.value=false }
const popup = useId()
function refresh() {
  const input = field.value
  if (!input || document.activeElement!==input) return
  slashOpen.value = !props.disabled && !compact.value && input.value.startsWith('/') && input.value !== '/compact' && '/compact'.startsWith(input.value)
}
function update(event: Event) {
  if (!(event.target instanceof HTMLTextAreaElement) || ('isComposing' in event && event.isComposing)) return
  emit('update:modelValue',compact.value ? '/compact '+event.target.value : event.target.value)
  refresh()
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode===229) return
  if (slashOpen.value && ['Escape','Tab','ArrowDown','ArrowUp','Enter'].includes(event.key) && !event.shiftKey) {
    event.preventDefault(); event.stopPropagation()
    if (event.key==='Escape') closePicker()
    if (event.key==='Enter' || event.key==='Tab') chooseCompact()
    if (event.key==='ArrowDown' || event.key==='ArrowUp') showSlashHelp()
    return
  }
  if (compact.value && event.key==='Backspace' && field.value?.selectionStart===0 && field.value.selectionEnd===0) {
    event.preventDefault(); removeCompact(); return
  }
  if ((props.submitOnEnter || compact.value) && event.key==='Enter' && !event.shiftKey) { event.preventDefault(); emit('submit') }
}
</script>

<template>
  <div class="composer-input" :class="{'has-compact-command':compact}">
    <button v-if="compact" class="composer-command-pill" type="button" :disabled="disabled" aria-label="移除压缩会话记忆命令" :title="usageTitle+' · 点击移除命令'" :aria-description="usageTitle+' '+usageText" @click="removeCompact">/compact <small v-if="usageText" class="context-usage" :class="{'has-value': usageRing}" :style="usageRing">{{ usageText }}</small></button>
    <textarea class="form-control" ref="field" :value="editable" rows="2" :aria-label="label" :placeholder="compact ? '可选：希望摘要保留的重点' : placeholder" :disabled="disabled" :maxlength="compact ? 1991 : undefined"
      :aria-invalid="!chatTextFits(modelValue)"
      aria-autocomplete="list" :aria-controls="slashOpen ? popup : undefined" :aria-activedescendant="slashOpen ? popup+'-compact' : undefined"
      @input="update($event)" @focus="refresh" @click="refresh" @compositionend="update($event)" @blur="closePicker" @keydown="keydown"/>
    <small v-if="!chatTextFits(modelValue)" role="alert">消息超过 64 KiB（UTF-8），请缩短后发送。</small>
    <Transition name="menu-pop"><div v-if="slashOpen" class="skill-picker slash-picker">
      <div :id="popup" role="listbox" aria-label="会话命令">
        <button :id="popup+'-compact'" type="button" class="skill-picker-option slash-option" role="option" aria-selected="true" tabindex="-1"
          @mouseenter="showSlashHelp" @mouseleave="slashHelp=false" @mousedown.prevent @click="chooseCompact">
          <strong :title="usageTitle">/compact <small v-if="usageText" class="context-usage" :class="{'has-value': usageRing}" :style="usageRing">{{ usageText }}</small></strong>
          <ElTooltip :visible="slashHelp && slashOpen" :content="compactDescription" placement="right" popper-class="slash-description-tooltip">
            <span ref="slashDescription">{{ compactDescription }}</span>
          </ElTooltip>
        </button>
      </div>
    </div></Transition>
  </div>
</template>

<style scoped>
.slash-picker { padding: .2rem; border-radius: .45rem; }
.slash-option {
  display: flex;
  align-items: center;
  gap: .55rem;
  min-block-size: 1.8rem;
  margin: 0;
  padding: .2rem .45rem;
  line-height: 1.4;
}
.slash-option strong { flex: none; }
.composer-command-pill, .slash-option strong {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
}
.composer-input .context-usage {
  display: inline-flex;
  align-items: center;
  gap: .2rem;
  margin: 0;
  font-size: .75rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  white-space: nowrap;
}
.context-usage::before {
  content: '';
  inline-size: .75rem;
  block-size: .75rem;
  border-radius: 50%;
  box-sizing: border-box;
  border: 1px dotted currentColor;
}
.context-usage.has-value::before {
  border: 0;
  background: conic-gradient(currentColor var(--usage-fill), color-mix(in srgb, currentColor 22%, transparent) 0);
  mask: radial-gradient(closest-side, transparent 62%, black 72%);
}
.slash-option > span { min-width: 0; margin: 0; }
:global(.slash-description-tooltip) { max-inline-size: min(15rem, calc(100vw - 1rem)); font-size: .75rem; }
@media (pointer: coarse) { .slash-option { min-block-size: 2.75rem; } }

.composer-input.has-compact-command {
  display: flex;
  align-items: flex-start;
  gap: .3rem;
  padding: .4rem .6rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
}
.has-compact-command:focus-within { outline: 2px solid var(--accent); }
.composer-input.has-compact-command > textarea {
  flex: 1;
  min-width: 0;
  padding: .15rem 0;
  margin: 0;
  border: 0;
  background: transparent;
  outline: 0;
  box-shadow: none;
}
.composer-command-pill {
  flex: none;
  padding: .14rem .65rem;
  border: 1px solid color-mix(in srgb,#378fff 55%,transparent);
  border-radius: 999px;
  background: color-mix(in srgb,#378fff 16%,var(--surface));
  color: color-mix(in srgb,#1677ed 65%,var(--text));
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
</style>
