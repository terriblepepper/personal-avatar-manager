<script setup lang="ts">
import { ref } from 'vue'
import ContextMenu from '../ContextMenu.vue'
import { notifyError } from '../../api/client'

const menu=ref<InstanceType<typeof ContextMenu>>()
const controls='a,audio,video,button,input,textarea,select,summary,[contenteditable]:not([contenteditable=false]),.tool-activities,.answer-sources,.mermaid-card .actions,iframe'
function messageText(bubble:HTMLElement) {
  const body=bubble.querySelector('.markdown-content') || bubble
  const content=body.cloneNode(true) as HTMLElement
  for (const figure of content.querySelectorAll('.mermaid-card')) {
    const source=figure.querySelector('pre')
    if (source) figure.replaceWith(source)
  }
  content.querySelectorAll('button,summary,input,textarea,select,script,style,[hidden],[aria-hidden=true],.tool-activities,.answer-sources,.mermaid-card,.peer-speaker-name,.reply-source,.stream-note,.peer-message-status,.chat-notices').forEach(node=>node.remove())
  content.querySelectorAll('p,li,pre,blockquote,h1,h2,h3,h4,h5,h6,br,tr').forEach(node=>node.append('\n'))
  content.querySelectorAll('td:not(:last-child),th:not(:last-child)').forEach(node=>node.append('\t'))
  return content.textContent.trim()
}
async function copy(text:string, anchor:HTMLElement) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return
    }
  } catch { /* A denied clipboard permission can still allow a user-initiated copy. */ }
  const selection=window.getSelection()
  const ranges=Array.from({length:selection?.rangeCount || 0},(_,i)=>selection!.getRangeAt(i).cloneRange())
  const input=document.createElement('textarea')
  input.value=text
  input.readOnly=true
  Object.assign(input.style,{position:'fixed',left:'-10000px',top:'0'})
  const root=anchor.closest('dialog[open]') || document.body
  root.append(input)
  try {
    input.focus({preventScroll:true})
    input.select()
    if (!document.execCommand?.('copy')) throw Error('复制失败，请选中文字后按 Ctrl+C')
  } catch (error) { notifyError(error) }
  finally {
    input.remove()
    anchor.focus({preventScroll:true})
    selection?.removeAllRanges()
    ranges.forEach(range=>selection?.addRange(range))
  }
}
function open(event:MouseEvent|KeyboardEvent) {
  if (event.defaultPrevented || !(event.target instanceof Element) || event.target.closest(controls)) return
  if (event instanceof KeyboardEvent && !(event.key==='ContextMenu' || event.shiftKey && event.key==='F10')) return
  const bubble=event.target.closest<HTMLElement>('[data-copy-message]')
  if (!bubble) return
  const selected=window.getSelection()
  if (selected?.toString() && bubble.contains(selected.anchorNode) && bubble.contains(selected.focusNode)) return
  const text=messageText(bubble)
  if (text) void menu.value?.open(event,bubble,[{label:'复制文字',icon:'copy',run:()=>void copy(text,bubble)}])
}
defineExpose({open})
</script>
<template><ContextMenu ref="menu" label="消息文字"/></template>
