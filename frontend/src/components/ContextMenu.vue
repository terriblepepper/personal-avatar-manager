<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, shallowRef } from 'vue'
import AppIcon from './AppIcon.vue'

type Action = { label:string; icon:string; run:()=>void }
const props=defineProps<{ label:string }>()
const actions=shallowRef<Action[]>([]), panel=ref<HTMLElement>(), target=shallowRef<Element>(document.body)
const position=ref({left:'0px',top:'0px'})
let trigger:HTMLElement|null=null

function close(restore=false) {
  if (panel.value?.hidePopover && panel.value.matches(':popover-open')) panel.value.hidePopover()
  actions.value=[]
  document.removeEventListener('pointerdown', outside, true)
  document.removeEventListener('focusin', outside, true)
  if (restore && trigger?.isConnected) trigger.focus({preventScroll:true})
  trigger=null
}
function outside(event:Event) {
  if (event.target instanceof Node && !panel.value?.contains(event.target)) close()
}
async function open(event:MouseEvent|KeyboardEvent, anchor:HTMLElement, items:Action[]) {
  if (event.defaultPrevented || !items.length) return
  if (event instanceof KeyboardEvent && !(event.key==='ContextMenu' || event.shiftKey && event.key==='F10')) return
  event.preventDefault()
  event.stopPropagation()
  const bounds=anchor.getBoundingClientRect()
  const x=event instanceof MouseEvent && (event.clientX || event.clientY) ? event.clientX : bounds.left
  const y=event instanceof MouseEvent && (event.clientX || event.clientY) ? event.clientY : bounds.bottom
  trigger=anchor
  target.value=anchor.closest('[popover], dialog[open]') || document.body
  actions.value=items
  await nextTick()
  if (!panel.value || trigger!==anchor) return
  if (panel.value.showPopover && !panel.value.matches(':popover-open')) panel.value.showPopover()
  position.value={
    left:Math.max(8,Math.min(x,innerWidth-panel.value.offsetWidth-8))+'px',
    top:Math.max(8,Math.min(y,innerHeight-panel.value.offsetHeight-8))+'px'
  }
  panel.value.querySelector('button')?.focus({preventScroll:true})
  document.addEventListener('pointerdown', outside, true)
  document.addEventListener('focusin', outside, true)
}
function navigate(event:KeyboardEvent) {
  if (event.key==='Escape') {
    event.preventDefault()
    event.stopPropagation()
    close(true)
  } else if (event.key==='ArrowDown' || event.key==='ArrowUp') {
    event.preventDefault()
    const buttons=Array.from(panel.value?.querySelectorAll('button') || [])
    const current=buttons.indexOf(document.activeElement as HTMLButtonElement)
    buttons[(current+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus()
  }
}
function choose(action:Action) {
  close(true)
  action.run()
}
onBeforeUnmount(()=>close())
defineExpose({open,close})
</script>

<template>
  <Teleport :to="target"><Transition name="menu-pop">
    <div v-if="actions.length" ref="panel" popover="manual" class="chat-material-menu file-context-menu" :style="position" role="menu" :aria-label="props.label" @keydown="navigate">
      <button v-for="action in actions" :key="action.label" type="button" role="menuitem" tabindex="-1" @click="choose(action)"><AppIcon :name="action.icon" :size="16"/>{{ action.label }}</button>
    </div>
  </Transition></Teleport>
</template>

<style scoped>
.file-context-menu { inset: auto; margin: 0; }
</style>
