<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ folder?: boolean; filename?: string }>()
const extension = computed(() => props.filename?.match(/\.([^.]+)$/)?.[1]?.toLowerCase() ?? '')
const image = computed(() => /^(png|jpe?g|gif|webp|bmp)$/.test(extension.value))
const colors: Record<string, string> = {
  pdf: '#b95f55', doc: '#487ab0', docx: '#487ab0',
  xls: '#418570', xlsx: '#418570', csv: '#418570', tsv: '#418570',
  ppt: '#bd8651', pptx: '#bd8651',
}
const abbreviations: Record<string, string> = { docx: 'DOC', xlsx: 'XLS', pptx: 'PPT' }
const color = computed(() => image.value ? '#8b76ac' : (colors[extension.value] ?? '#68867c'))
const label = computed(() => image.value ? 'IMG' : ((abbreviations[extension.value] ?? extension.value.toUpperCase()).slice(0, 4) || 'FILE'))
</script>

<template>
  <svg v-if="folder" class="material-type-icon" viewBox="0 0 40 36" aria-hidden="true">
    <path d="M3 8a3 3 0 0 1 3-3h10l4 4h14a3 3 0 0 1 3 3v17H3Z" fill="#d2ac58"/>
    <path d="M3 13h34v16a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3Z" fill="#f2d58b"/>
    <path d="M3 17h34v12a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3Z" fill="#f6dfa3"/>
    <path d="M8 26h8" stroke="#567b65" stroke-width="2.5" stroke-linecap="round"/>
  </svg>
  <svg v-else class="material-type-icon" viewBox="0 0 36 40" aria-hidden="true">
    <path d="M7 2h14l9 9v24a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V5a3 3 0 0 1 3-3Z" fill="#fff" stroke="#d3dfd8"/>
    <path d="M21 2v7a2 2 0 0 0 2 2h7" fill="#edf2ee" stroke="#d3dfd8"/>
    <template v-if="image">
      <circle cx="12" cy="17" r="2.5" :fill="color"/>
      <path d="m8 26 6-6 4 4 4-6 5 8" fill="none" :stroke="color" stroke-width="2" stroke-linejoin="round"/>
    </template>
    <path v-else d="M10 17h10m-10 5h13" :stroke="color" stroke-opacity=".45" stroke-width="2" stroke-linecap="round"/>
    <rect x="2" y="27" width="28" height="11" rx="3" :fill="color"/>
    <text x="16" y="35" text-anchor="middle" fill="white" font-family="system-ui,sans-serif" font-size="7.5" font-weight="700">{{ label }}</text>
  </svg>
</template>
