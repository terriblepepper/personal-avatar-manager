<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, shallowRef, useId } from 'vue'

const id = useId()
const owner = shallowRef<HTMLSelectElement>()
const menu = ref<HTMLElement>(), index = ref(-1), label = ref('')
const rows = ref<{ index: number; label: string; group: string; disabled: boolean }[]>([])
const position = ref({ left:'0px', top:'0px', width:'0px', maxHeight:'18rem' })
const listeners = new AbortController()
let observer: MutationObserver | undefined
let prefix = '', lastTyped = 0

function place() {
  const select = owner.value
  if (!select) return
  const box = select.getBoundingClientRect(), below = innerHeight - box.bottom - 8, above = box.top - 8
  const height = Math.min(300, Math.max(above,below)), width = Math.min(innerWidth - 16, Math.max(160,box.width))
  const desired = Math.min(menu.value?.scrollHeight || 300,300)
  const toTop = below < desired && above > below
  position.value = {left:Math.max(8, Math.min(box.left, innerWidth-width-8))+'px',
    top:(toTop ? Math.max(8,box.top-Math.min(desired,height)-3) : box.bottom+3)+'px',
    width:width+'px', maxHeight:Math.max(1,Math.min(height,toTop?above:below)-3)+'px'}
}
function readOptions(select: HTMLSelectElement) {
  rows.value = Array.from(select.options).flatMap((option,i) => {
    const group = option.parentElement instanceof HTMLOptGroupElement ? option.parentElement : undefined
    return option.hidden || group?.hidden ? [] : [{index:i,label:option.label,group:group?.label || '',disabled:option.disabled || !!group?.disabled}]
  })
}
function close() {
  const select = owner.value
  if (select) for (const key of ['aria-expanded','aria-controls','aria-activedescendant']) select.removeAttribute(key)
  owner.value = undefined
  observer?.disconnect()
  prefix = ''
}
function highlight(i: number) {
  if (!rows.value.some(row => row.index === i && !row.disabled)) return
  index.value = i
  owner.value?.setAttribute('aria-activedescendant',id+'-'+i)
  void nextTick(() => menu.value?.querySelector('[data-active="true"]')?.scrollIntoView({block:'nearest'}))
}
function open(select: HTMLSelectElement) {
  if (owner.value === select) return
  close()
  owner.value = select
  select.focus({preventScroll:true})
  label.value = select.getAttribute('aria-label') || select.labels?.[0]?.firstChild?.textContent?.trim() || select.name || '选择选项'
  select.setAttribute('aria-expanded','true'); select.setAttribute('aria-controls',id)
  readOptions(select)
  index.value = -1
  highlight(select.selectedIndex)
  place(); void nextTick(place)
  observer = new MutationObserver(records => {
    if (!select.isConnected || select.disabled || !select.getClientRects().length) { close(); return }
    if (records.some(record => select.contains(record.target))) readOptions(select)
  })
  observer.observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['disabled','hidden','open','label','value']})
}
function choose(i: number) {
  const select = owner.value
  if (!select || !rows.value.some(row => row.index === i && !row.disabled)) return
  close()
  if (select.selectedIndex !== i) {
    select.selectedIndex = i
    select.dispatchEvent(new Event('input',{bubbles:true}))
    select.dispatchEvent(new Event('change',{bubbles:true}))
  }
}
function candidate(target: EventTarget | null) {
  return target instanceof HTMLSelectElement && !target.matches(':disabled') && !target.multiple && target.size <= 1 ? target : null
}
function pointer(event: PointerEvent) {
  if (event.button !== 0) return
  const select = candidate(event.target)
  if (select) { event.preventDefault(); if (select === owner.value) close(); else open(select) }
  else if (!menu.value?.contains(event.target as Node)) close()
}
function key(event: KeyboardEvent) {
  const select = candidate(event.target)
  if (!select) return
  const active = owner.value === select
  if (event.key === 'Tab') { close(); return }
  if (event.ctrlKey || event.metaKey || event.isComposing) return
  const action = event.key
  if (!['Escape','Enter',' ','ArrowDown','ArrowUp','Home','End'].includes(action) && action.length !== 1) return
  if (action === 'Escape' && !active) return
  event.preventDefault(); event.stopPropagation()
  if (action === 'Escape') { close(); return }
  if (action === 'Enter' || action === ' ') {
    if (active) choose(index.value); else open(select)
    return
  }
  open(select)
  const enabled = rows.value.filter(row => !row.disabled)
  let next
  if (action === 'Home') next=enabled[0]
  else if (action === 'End') next=enabled.at(-1)
  else if (action === 'ArrowUp' || action === 'ArrowDown') {
    const current=enabled.findIndex(row => row.index === index.value)
    next=enabled[Math.max(0,Math.min(enabled.length-1,current+(action === 'ArrowUp' ? -1 : 1)))]
  } else {
    const now = performance.now()
    prefix = (now-lastTyped<700 ? prefix : '')+action.toLocaleLowerCase(); lastTyped=now
    next=enabled.find(row=>row.label.toLocaleLowerCase().startsWith(prefix))
  }
  if (next) highlight(next.index)
}
onMounted(() => {
  const options = {capture:true,signal:listeners.signal}
  document.addEventListener('pointerdown',pointer,options)
  document.addEventListener('click',event => {
    const select=candidate(event.target)
    if (select) { event.preventDefault(); if (event.detail===0) open(select) }
  },options)
  document.addEventListener('keydown',key,options)
  document.addEventListener('focusin',event => { if (owner.value && event.target !== owner.value && !menu.value?.contains(event.target as Node)) close() },options)
  document.addEventListener('scroll',event => { if (owner.value && !menu.value?.contains(event.target as Node)) place() },options)
  window.addEventListener('resize',place,options)
})
onBeforeUnmount(() => { listeners.abort(); close() })
</script>

<template>
  <Teleport to="body"><Transition name="menu-pop">
    <div v-if="owner" :id="id" ref="menu" class="ui-select-menu" :style="position" role="listbox" :aria-label="label" @pointerdown.prevent>
      <template v-for="(row,n) in rows" :key="row.index">
        <small v-if="row.group && row.group !== rows[n-1]?.group">{{ row.group }}</small>
        <div :id="id+'-'+row.index" role="option" :aria-selected="owner.selectedIndex===row.index" :aria-disabled="row.disabled"
          :data-active="index===row.index" @pointermove="highlight(row.index)" @click="choose(row.index)">{{ row.label }}</div>
      </template>
    </div>
  </Transition></Teleport>
</template>

<style scoped>
.ui-select-menu { position: fixed; z-index: 2147483646; overflow: auto; overscroll-behavior: contain; padding: .25rem; }
.ui-select-menu [role=option], .ui-select-menu small { display: block; padding: .36rem .6rem; overflow-wrap: anywhere; }
.ui-select-menu [role=option] { border-radius: .25rem; cursor: pointer; }
.ui-select-menu [data-active=true] { background: var(--accent-subtle); }
.ui-select-menu [aria-selected=true] { font-weight: 600; }
.ui-select-menu [aria-disabled=true] { opacity: .5; cursor: default; }
</style>
