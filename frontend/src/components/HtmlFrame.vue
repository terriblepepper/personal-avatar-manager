<script setup lang="ts">
import { ref, shallowRef, watch } from 'vue'

const props = defineProps<{source: string; filename: string}>()
const markup = shallowRef<Blob>()
const error = ref('')
const revision = ref(0)
watch(() => props.source, async (source, _, cleanup) => {
  let stale = false
  cleanup(() => { stale = true })
  markup.value = undefined
  error.value = ''
  revision.value++
  try {
    const imports = /['"]three['"]/.test(source)
      ? '<script type="importmap">' + (await import('../utils/threePreview')).threeImports + '<' + '/script>'
      : ''
    if (!stale) {
      markup.value = new Blob(['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">', imports, source], {type:'text/html;charset=utf-8'})
    }
  } catch {
    if (!stale) error.value = 'HTML 预览组件加载失败'
  }
}, {immediate: true})

function render(event: Event) {
  const frame = event.currentTarget as HTMLIFrameElement
  frame.contentWindow?.postMessage({html: markup.value}, '*')
}
</script>

<template>
  <p v-if="error" role="alert">{{ error }}</p>
  <iframe v-else-if="markup" :key="revision" class="html-frame" src="/static/preview/html.html?v=3"
    sandbox="allow-scripts" referrerpolicy="no-referrer" :title="filename" @load.once="render"/>
  <p v-else role="status">正在准备 HTML 预览…</p>
</template>

<style scoped>
.html-frame {
  display: block; inline-size: 100%; block-size: clamp(230px, 49dvh, 480px);
  border: 1px solid var(--ui-border); border-radius: .5rem; background: white;
}
</style>
