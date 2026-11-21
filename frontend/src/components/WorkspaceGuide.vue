<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  phase: 'welcome' | 'tour'
  steps: { title: string; detail: string; path: string; target: string }[]
  limited: boolean
  busy: boolean
  error: string
}>()
const emit = defineEmits<{ advance: [stage: 'tour' | 'done']; menu: [open: boolean]; reloadPage: [] }>()
const router = useRouter(), route = useRoute()
const shownPhase = ref(props.phase), index = ref(0), moving = ref(false), navigationError = ref('')
const current = computed(() => props.steps[index.value])
const dialog = ref<HTMLDialogElement>(), card = ref<HTMLElement>(), heading = ref<HTMLElement>()
const spotlight = ref<Record<string,string>>(), placement = ref<Record<string,string>>()
let target: HTMLElement | null = null
let sizes: ResizeObserver, changes: MutationObserver
let animation: Animation | undefined
let resolveTarget: ((found: boolean) => void) | undefined
const loadingTarget = ref(false)
const listeners = new AbortController()
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')

function measure() {
  if (!target || !card.value || !dialog.value) return
  const r = target.getBoundingClientRect()
  const width = dialog.value.clientWidth, height = dialog.value.clientHeight
  const cw = card.value.offsetWidth, ch = card.value.offsetHeight, gap = 14, margin = 12
  if (!r.width || !r.height || r.bottom <= 0 || r.top >= height || r.right <= 0 || r.left >= width) {
    spotlight.value = placement.value = undefined
    return
  }
  let x = r.left, y = r.top
  if (r.right + gap + cw <= width - margin) x = r.right + gap
  else if (r.left - gap - cw >= margin) x = r.left - gap - cw
  else y = r.bottom + gap + ch <= height - margin ? r.bottom + gap : r.top - gap - ch
  placement.value = { left: Math.max(margin, Math.min(x, width - cw - margin)) + 'px',
    top: Math.max(margin, Math.min(y, height - ch - margin)) + 'px' }
  spotlight.value = { left: r.left - 3 + 'px', top: r.top - 3 + 'px', width: r.width + 6 + 'px', height: r.height + 6 + 'px' }
}
function bindTarget() {
  const { path, target: key } = current.value
  const scope = key === 'help' ? document.querySelector('#sidebar-extra-actions') : document.querySelector('main[data-guide-page="' + path + '"]')
  const node = scope?.querySelector<HTMLElement>('[data-guide-target="' + key + '"]')
  const found = node?.getClientRects().length && getComputedStyle(node).visibility === 'visible' ? node : null
  if (found !== target) {
    if (target) sizes.unobserve(target)
    target = found
    if (found) {
      found.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' })
      sizes.observe(found)
    }
  }
  if (found) measure()
  else spotlight.value = placement.value = undefined
  return !!found
}
function waitForTarget(scope: Element) {
  changes.observe(scope, { childList:true, subtree:true })
  if (bindTarget()) return true
  loadingTarget.value = true
  return new Promise<boolean>(resolve => {
    const timeout = window.setTimeout(() => resolveTarget?.(false), 6000)
    resolveTarget = found => {
      clearTimeout(timeout)
      resolveTarget = undefined
      loadingTarget.value = false
      resolve(found)
    }
  })
}
async function fade(frames: Keyframe[], duration: number) {
  if (!card.value || reducedMotion.matches) return
  animation = card.value.animate(frames, { duration, easing: 'ease-out', fill: 'both' })
  await animation.finished.catch(() => {})
}
async function showStep(next: number, animate = true) {
  if (moving.value || listeners.signal.aborted) return
  moving.value = true
  changes.disconnect()
  const direction = next < index.value ? -1 : 1
  const step = props.steps[next]
  try {
    if (animate) await fade([{ opacity:1, translate:'0 0' }, { opacity:0, translate:(-8 * direction) + 'px 0' }], 110)
    if (listeners.signal.aborted) return
    spotlight.value = undefined
    if (route.path !== step.path) await router.replace(step.path)
    else if (navigationError.value && next === index.value) emit('reloadPage')
    if (listeners.signal.aborted) return
    index.value = next
    shownPhase.value = 'tour'
    emit('menu', step.target === 'help')
    await nextTick()
    const scope = document.querySelector(step.target === 'help' ? '#console-sidebar' : 'main')
    if (route.path !== step.path || !scope || !(await waitForTarget(scope))) throw new Error('模块定位点未就绪')
    navigationError.value = ''
  } catch {
    if (listeners.signal.aborted) return
    index.value = next
    shownPhase.value = 'tour'
    changes.disconnect()
    if (target) sizes.unobserve(target)
    target = null
    spotlight.value = placement.value = undefined
    navigationError.value = '未能打开对应模块。请重试，或跳过导览。'
  } finally {
    animation?.cancel()
    if (!listeners.signal.aborted) {
      await nextTick()
      measure()
      heading.value?.focus({ preventScroll:true })
      if (animate) await fade([{ opacity:0, translate:(8 * direction) + 'px 0' }, { opacity:1, translate:'0 0' }], 160)
      animation?.cancel()
      moving.value = false
    }
  }
}
watch(() => props.phase, stage => { if (stage === 'tour') void showStep(0) })
watch(() => route.path, path => {
  if (shownPhase.value === 'tour' && !moving.value && path !== current.value.path && !path.startsWith('/admin')) void showStep(index.value)
})
onMounted(() => {
  dialog.value?.showModal()
  sizes = new ResizeObserver(measure)
  changes = new MutationObserver(() => {
    if (bindTarget()) resolveTarget?.(true)
  })
  if (card.value) sizes.observe(card.value)
  window.addEventListener('resize', measure, { signal:listeners.signal })
  document.addEventListener('scroll', measure, { capture:true, signal:listeners.signal })
  if (props.phase === 'tour') void showStep(0, false)
  else heading.value?.focus({ preventScroll:true })
})
onBeforeUnmount(() => { listeners.abort(); resolveTarget?.(false); changes.disconnect(); sizes.disconnect(); animation?.cancel() })
</script>

<template>
  <dialog ref="dialog" class="workspace-guide" aria-labelledby="guide-title" aria-describedby="guide-detail"
    @cancel.prevent="!busy && emit('advance', 'done')">
    <div v-if="spotlight" class="guide-spotlight" aria-hidden="true" :style="spotlight"/>
    <div v-else-if="shownPhase === 'welcome'" class="guide-shade" aria-hidden="true"/>
    <div v-if="loadingTarget" class="guide-loading" role="status">正在加载{{ current.title }}… <button type="button" class="button small" :disabled="busy" @click="emit('advance','done')">跳过引导</button></div>
    <section ref="card" class="guide-card" :inert="loadingTarget" :class="{ welcome: shownPhase === 'welcome', placed: placement }" :style="placement">
      <template v-if="shownPhase === 'welcome'">
        <div class="guide-brand"><span class="mark"><AppIcon name="agent" :size="24"/></span>镜像 · 个人分身工作台</div>
        <h2 id="guide-title" ref="heading" tabindex="-1">欢迎来到分身平台</h2>
        <p id="guide-detail">{{ limited ? '你正在使用资料管理入口。先完善个人资料，再准备分身需要的文件和模型。' : '在这里，你可以把自己的资料、知识和表达习惯交给个人分身，再与它对话。' }}</p>
        <ol class="guide-start">
          <li><strong>填写个人资料</strong><span>选择身份，保存草稿，准备好后提交分身申请。</span></li>
          <li><strong>准备资料与模型</strong><span>上传可引用的文件，配置回答问题所用的模型。</span></li>
          <li v-if="!limited"><strong>开始第一次对话</strong><span>让分身根据资料回答问题，也可以用 Soul 调整它的表达方式。</span></li>
        </ol>
      </template>
      <template v-else>
        <small class="guide-count">模块导览 · {{ index + 1 }} / {{ steps.length }}</small>
        <h2 id="guide-title" ref="heading" tabindex="-1">{{ navigationError ? '模块暂未就绪' : current.title }}</h2>
        <p id="guide-detail">{{ navigationError || current.detail }}</p>
        <progress :value="index + 1" :max="steps.length" aria-label="导览进度"/>
      </template>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <div class="guide-actions">
        <button type="button" class="button ghost" :disabled="busy || moving" @click="emit('advance','done')">跳过引导</button>
        <button v-if="shownPhase === 'tour' && index > 0" type="button" class="button" :disabled="busy || moving" @click="showStep(index - 1)">上一步</button>
        <button v-if="shownPhase === 'welcome'" type="button" class="button primary" :disabled="busy || moving" @click="emit('advance','tour')">认识工作台</button>
        <button v-else-if="navigationError" type="button" class="button primary" :disabled="moving" @click="showStep(index)">重试</button>
        <button v-else-if="index + 1 < steps.length" type="button" class="button primary" :disabled="busy || moving" @click="showStep(index + 1)">下一步</button>
        <button v-else type="button" class="button primary" :disabled="busy || moving" @click="emit('advance','done')">完成导览</button>
      </div>
    </section>
  </dialog>
</template>

<style scoped>
.workspace-guide[open] { position:fixed; inset:0; margin:0; padding:0; width:100%; height:100%; max-width:none; max-height:none; border:0; background:transparent; color:var(--text); overflow:hidden; }
.workspace-guide[open],.workspace-guide::backdrop { background:transparent; -webkit-backdrop-filter:none; backdrop-filter:none; border-radius:0; box-shadow:none; }
.guide-loading { position:absolute; top:12px; left:50%; transform:translateX(-50%); padding:8px 12px; border-radius:8px; background:var(--surface); box-shadow:0 2px 12px #0002; white-space:nowrap; z-index:1; }
.guide-shade,.guide-spotlight { position:absolute; pointer-events:none; }
.guide-shade { inset:0; background:rgb(8 17 31 / 12%); }
.guide-spotlight { border-radius:8px; outline:2px solid var(--accent); box-shadow:0 0 0 5px color-mix(in srgb,var(--accent) 14%,transparent); }
.guide-card { box-sizing:border-box; position:absolute; inset:50% auto auto 50%; transform:translate(-50%,-50%); width:min(23rem,calc(100% - 24px)); max-height:calc(100% - 24px); overflow:auto; padding:22px; border:1px solid var(--border); border-radius:14px; background:var(--surface); box-shadow:0 12px 48px #0003; }
@supports (backdrop-filter:blur(1px)) {
  .guide-card { background:color-mix(in srgb,var(--surface) 92%,transparent); backdrop-filter:blur(16px); }
}
.guide-card[inert] { visibility:hidden; }
.guide-card.placed { transform:none; }
.guide-card.welcome { width:min(32rem,calc(100% - 24px)); padding:clamp(20px,4vw,36px); }
.guide-card h2 { margin:10px 0 8px; font-size:1.4rem; line-height:1.3; }
.guide-card h2:focus { outline:none; }
.welcome h2 { font-size:clamp(1.65rem,4vw,2rem); }
.guide-card p,.guide-start span { line-height:1.65; color:var(--text-secondary); }
.guide-brand { display:flex; gap:10px; align-items:center; margin-bottom:22px; font-weight:600; }
.guide-start { padding:16px 16px 16px 40px; background:var(--surface-alt); border-radius:8px; }
.guide-start li + li { margin-top:12px; }
.guide-start li::marker,.guide-count { color:var(--accent); font-weight:600; }
.guide-start span { display:block; font-size:.875rem; }
.guide-card progress { width:100%; height:4px; accent-color:var(--accent); }
.guide-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:20px; }
.guide-actions > :first-child { margin-inline-end:auto; }
.guide-actions button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
</style>
