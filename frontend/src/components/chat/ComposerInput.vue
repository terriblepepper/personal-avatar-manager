<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, useId, watch } from 'vue'
import { chatTextFits } from '../../chatTextBudget'
import { ElMessage, ElTooltip } from 'element-plus'
import { request } from '../../api/client'

interface Skill { id: string; name: string; description?: string; enabled: boolean | number }
const props = defineProps<{
  modelValue: string; catalog: Skill[]; session: string; label: string; placeholder: string;
  disabled?: boolean; submitOnEnter?: boolean; usageUrl?: string; usageRevision?: string | number;
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; submit: [] }>()
const field = ref<HTMLTextAreaElement>(), token = ref<{start:number; query:string}>()
const highlight = ref(0), hidden = ref<string[]>([])
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
function closePicker() { token.value=undefined; slashOpen.value=false }
const popup = useId()
const storageKey = computed(() => 'avatar.hidden-skill-shortcuts.' + props.session)
watch(storageKey, key => {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || '[]')
    hidden.value = Array.isArray(value) ? value.filter((item): item is string => typeof item==='string') : []
  } catch { hidden.value = [] }
  closePicker()
}, { immediate: true })
const matches = computed(() => token.value ? props.catalog
  .filter(skill => !hidden.value.includes(skill.id) && (skill.name+' '+(skill.description||'')).toLocaleLowerCase().includes(token.value!.query)) : [])
const open = computed(() => !!token.value && !props.disabled)
const command = (name: string) => '$' + JSON.stringify(name)
function refresh() {
  const input = field.value
  if (!input || document.activeElement!==input) return
  slashOpen.value = !props.disabled && !compact.value && input.value.startsWith('/') && input.value !== '/compact' && '/compact'.startsWith(input.value)
  if (compact.value || slashOpen.value) {token.value=undefined; return}
  const prefix = input.value.slice(0,input.selectionStart ?? input.value.length)
  const match = /(^|\s)\$("(?:\\.|[^"\\])*|[^\s$()]*)$/u.exec(prefix)
  token.value = match ? {start:match.index+match[1].length,query:match[2].replace(/^"/,'').toLocaleLowerCase()} : undefined
  highlight.value = matches.value.findIndex(skill => !!skill.enabled)
}
function update(event: Event) {
  if (!(event.target instanceof HTMLTextAreaElement) || ('isComposing' in event && event.isComposing)) return
  emit('update:modelValue',compact.value ? '/compact '+event.target.value : event.target.value)
  refresh()
}
async function choose(skill:Skill) {
  const input = field.value, entry = token.value
  if (!input || !entry || !skill.enabled) return
  const rest = input.value.slice(entry.start)
  const full = /^\$(?:"(?:\\.|[^"\\])*"?|[^\s$()]*)/u.exec(rest)?.[0] || '$'
  const suffix = rest.slice(full.length)
  const insert = command(skill.name) + (/^\s/.test(suffix) ? '' : ' ')
  const value = input.value.slice(0,entry.start) + insert + suffix
  if (!chatTextFits(value)) { ElMessage.warning('消息最多 64 KiB（UTF-8）'); return }
  emit('update:modelValue', value)
  token.value = undefined
  await nextTick()
  const caret = entry.start + insert.length
  input.focus()
  input.setSelectionRange(caret,caret)
}
function saveHidden(ids:string[]) {
  hidden.value = ids
  try { localStorage.setItem(storageKey.value,JSON.stringify(ids)) } catch { /* optional local preference */ }
  highlight.value = matches.value.findIndex(skill => !!skill.enabled)
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
  if (open.value && event.key==='Escape') {
    token.value = undefined; event.stopPropagation(); event.preventDefault(); return
  }
  if (open.value && (event.key==='ArrowDown' || event.key==='ArrowUp')) {
    const enabled = matches.value.flatMap((skill,index) => skill.enabled?[index]:[])
    if (!enabled.length) return
    event.preventDefault()
    const delta=event.key==='ArrowDown'?1:-1
    highlight.value=enabled[(enabled.indexOf(highlight.value)+delta+enabled.length)%enabled.length]
    return
  }
  if (open.value && (event.key==='Tab' || event.key==='Enter' && !event.shiftKey)) {
    const active=matches.value[highlight.value]
    if (active?.enabled) { event.preventDefault(); void choose(active); return }
    if (event.key==='Enter') { event.preventDefault(); return }
  }
  if ((props.submitOnEnter || compact.value) && event.key==='Enter' && !event.shiftKey) { event.preventDefault(); emit('submit') }
}
defineExpose({focus:()=>field.value?.focus()})
</script>

<template>
  <div class="composer-input" :class="{'has-compact-command':compact}">
    <button v-if="compact" class="composer-command-pill" type="button" :disabled="disabled" aria-label="移除压缩会话记忆命令" :title="usageTitle+' · 点击移除命令'" :aria-description="usageTitle+' '+usageText" @click="removeCompact">/compact <small v-if="usageText" class="context-usage" :class="{'has-value': usageRing}" :style="usageRing">{{ usageText }}</small></button>
    <textarea class="form-control" ref="field" :value="editable" rows="2" :aria-label="label" :placeholder="compact ? '可选：希望摘要保留的重点' : placeholder" :disabled="disabled" :maxlength="compact ? 1991 : undefined"
      :aria-invalid="!chatTextFits(modelValue)"
      aria-autocomplete="list" :aria-controls="open || slashOpen ? popup : undefined" :aria-activedescendant="slashOpen ? popup+'-compact' : open && highlight>=0 ? popup+'-'+highlight : undefined"
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
    <Transition name="menu-pop"><div v-if="open" class="skill-picker">
      <div class="skill-picker-heading">选择技能 <span>↑ ↓ 选择 · Enter / Tab 填入</span></div>
      <div :id="popup" role="listbox" aria-label="快捷选择技能">
        <div v-for="(skill,index) in matches" :key="skill.id" class="skill-picker-choice">
          <button :id="popup+'-'+index" type="button" role="option" tabindex="-1" class="skill-picker-option" :aria-selected="index===highlight" :disabled="!skill.enabled" @mousedown.prevent @click="choose(skill)">
            <strong>{{ skill.name }}</strong><span v-if="skill.description">{{ skill.description }}</span>
          </button>
          <button type="button" class="button small" :aria-label="'从快捷选择移除 '+skill.name" title="仅从快捷选择移除" @mousedown.prevent @click="saveHidden([...hidden,skill.id])">×</button>
        </div>
      </div>
      <p v-if="!matches.length" class="skill-picker-empty">没有匹配的技能。</p>
      <button v-if="hidden.length" type="button" class="button small" @mousedown.prevent @click="saveHidden([])">恢复已隐藏的快捷选项</button>
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
