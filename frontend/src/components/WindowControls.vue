<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'

const props = defineProps<{ initiallyFullscreen?: boolean; anchorBottomRight?: boolean }>()
const fullscreen = ref(false)
const controls = ref<HTMLElement>()
let motion: Animation | undefined
let drag: { x: number; y: number; box: DOMRect } | undefined

function surface() {
  return controls.value?.closest<HTMLElement>('.avatar-window, .el-dialog')
}

function toggle() {
  const node = surface()
  if (!node) return
  const before = node.getBoundingClientRect()
  motion?.cancel()
  fullscreen.value = !fullscreen.value
  node.classList.toggle('avatar-fullscreen', fullscreen.value)
  node.classList.toggle('vue-window-fullscreen', fullscreen.value)
  const after = node.getBoundingClientRect()
  const resized = () => {
    node.dispatchEvent(new Event('resize'))
    window.dispatchEvent(new Event('resize'))
  }
  if (!after.width || !after.height || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    resized()
    return
  }
  const from = `translate(${before.left-after.left}px,${before.top-after.top}px) scale(${before.width/after.width},${before.height/after.height})`
  motion = node.animate([
    { transform: from, transformOrigin: 'top left' },
    { transform: 'none', transformOrigin: 'top left' },
  ], { duration: 400, easing: 'cubic-bezier(.2,.75,.25,1)' })
  motion.onfinish = resized
}

function resize(box: DOMRect, dx: number, dy: number) {
  const node = surface()
  if (!node || fullscreen.value) return
  const anchored = props.anchorBottomRight
  const scale = anchored ? 1 : 2
  const maxWidth = anchored ? box.right - 8 : innerWidth - 16
  const maxHeight = anchored ? box.bottom - 8 : innerHeight - 16
  if (!anchored) {
    Object.assign(node.style, { position: 'fixed', inset: '0 calc(100% - 100vw) calc(100% - 100dvh) 0', margin: 'auto', transform: 'none' })
  }
  node.style.width = Math.min(maxWidth, Math.max(256, box.width - scale * dx)) + 'px'
  node.style.height = Math.min(maxHeight, Math.max(176, box.height - scale * dy)) + 'px'
  window.dispatchEvent(new Event('resize'))
}

function start(event: PointerEvent) {
  const node = surface()
  if (!node || event.button !== 0 || !(event.currentTarget instanceof HTMLElement)) return
  drag = { x: event.clientX, y: event.clientY, box: node.getBoundingClientRect() }
  event.currentTarget.setPointerCapture(event.pointerId)
  event.preventDefault()
}

function move(event: PointerEvent) {
  if (drag) resize(drag.box, event.clientX - drag.x, event.clientY - drag.y)
}

function stop() { drag = undefined }

function keyboard(event: KeyboardEvent) {
  const offsets: Record<string, [number, number]> = {
    ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
  }
  const offset = offsets[event.key]
  const node = surface()
  if (!offset || !node) return
  event.preventDefault()
  const step = event.shiftKey ? 50 : 10
  resize(node.getBoundingClientRect(), offset[0] * step, offset[1] * step)
}

onMounted(() => {
  if (props.initiallyFullscreen) {
    fullscreen.value = true
    surface()?.classList.add('avatar-fullscreen', 'vue-window-fullscreen')
  }
})
onBeforeUnmount(() => {
  motion?.cancel()
  surface()?.classList.remove('avatar-fullscreen', 'vue-window-fullscreen')
})
</script>

<template>
  <span ref="controls" class="vue-window-controls">
    <button type="button" class="window-caption-button" :aria-pressed="fullscreen"
      :aria-label="fullscreen ? '还原小窗' : '全屏'" :title="fullscreen ? '还原小窗' : '全屏'" @click="toggle">
      <AppIcon :name="fullscreen ? 'restore' : 'maximize'" :size="22"/>
    </button>
    <button v-if="!fullscreen" type="button" class="window-resize-handle"
      aria-label="从左上角调整窗口大小" title="拖动左上角或使用方向键"
      @pointerdown="start" @pointermove="move" @pointerup="stop" @pointercancel="stop"
      @lostpointercapture="stop" @keydown="keyboard">↖</button>
  </span>
</template>
