<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowReactive, watch } from 'vue'
import type { ChatFile } from '../../types'
import { prepareAnimation } from '../../utils/animation'
import { chatFileURL } from '../../utils/chatFileURL'
import { readAnimation } from '../../utils/readAnimation'
import AnimationDiagram from '../AnimationDiagram.vue'
import MaterialTypeIcon from '../MaterialTypeIcon.vue'

const props = defineProps<{ files: ChatFile[]; inlineKeys: Set<string>; conversationId?: string }>()
const emit = defineEmits<{ preview: [file: ChatFile] }>()
const host = ref<HTMLElement>(), near = ref(false)
let observer: IntersectionObserver | undefined
onMounted(() => {
  if (!host.value || !('IntersectionObserver' in window)) { near.value = true; return }
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) { near.value = true; observer?.disconnect() }
  }, { rootMargin: '160px' })
  observer.observe(host.value)
})
onUnmounted(() => observer?.disconnect())

const items = computed(() => [...new Map(props.files.map(file => [file.id, file])).values()]
  .map(file => ({ file, url: chatFileURL(file, props.conversationId) })))
const loaded = shallowReactive(new Map<string, { source: string; key: string } | null>())
watch(() => near.value ? JSON.stringify(items.value.map(item => item.url)) : '', (signature, _, cleanup) => {
  const controller = new AbortController()
  cleanup(() => controller.abort())
  const urls = new Set(items.value.map(item => item.url))
  for (const key of loaded.keys()) if (!urls.has(key)) loaded.delete(key)
  if (!signature) return
  // Read one original at a time so several large attachments do not compete for memory.
  void (async () => {
    for (const url of urls) {
      if (controller.signal.aborted) return
      if (!url || loaded.has(url)) continue
      try {
        const source = await readAnimation(url, controller.signal)
        if (controller.signal.aborted) return
        const prepared = prepareAnimation(source)
        loaded.set(url, {source:prepared.source, key:JSON.stringify(prepared.scene)})
      } catch { if (!controller.signal.aborted) loaded.set(url, null) }
    }
  })()
}, { immediate: true })
const cards = computed(() => {
  const seen = new Set(props.inlineKeys)
  return items.value.flatMap(({ file, url }) => {
    const result = url ? loaded.get(url) : null
    if (result && seen.has(result.key)) return []
    if (result) seen.add(result.key)
    return [{ file, url, result }]
  })
})
</script>

<template>
  <div ref="host">
    <template v-for="{ file, url, result } in cards" :key="file.id">
      <AnimationDiagram v-if="result" :source="result.source" :conversation-id="file.origin === 'material' ? undefined : conversationId" :filename="file.filename"/>
      <div v-else class="animation-fallback">
        <button type="button" class="button animation-file" :disabled="!url || result === undefined"
          @click="emit('preview',file)"><MaterialTypeIcon :filename="file.filename"/><span>{{ file.filename }}</span>
          <small>{{ result === undefined ? '正在加载' : '文件预览' }}</small></button>
        <a v-if="url && result === null" :href="url" :download="file.filename">下载原件</a>
      </div>
    </template>
  </div>
</template>

<style scoped>
.animation-fallback { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
.animation-file { display: inline-flex; gap: .5rem; align-items: center; max-width: 100%; margin-block: .25rem; }
.animation-file svg { flex: none; width: 1.4rem; height: 1.6rem; }
.animation-file span { overflow-wrap: anywhere; }
</style>
