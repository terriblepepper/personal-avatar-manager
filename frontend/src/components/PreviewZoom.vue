<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ percent: number; fit: boolean; fitLabel?: string }>()
const emit = defineEmits<{ scale: [value: number | null] }>()
const input = ref<HTMLInputElement>()
function restore() { if (input.value) input.value.value = String(props.percent) }
function commit() {
  const field = input.value
  if (field && field.reportValidity() && Number.isFinite(field.valueAsNumber)) emit('scale', field.valueAsNumber / 100)
}
watch(() => props.percent, restore)
</script>

<template>
  <div class="preview-zoom" role="group" aria-label="预览缩放">
    <button type="button" class="button small" aria-label="缩小预览" title="缩小预览" :disabled="!fit && percent <= .01" @click="emit('scale', (percent - 25) / 100)">−</button>
    <label class="zoom-percent">
      <input placeholder=" " class="form-control" ref="input" type="number" :value="percent" min=".01" max="10000" step="any" required inputmode="decimal" aria-label="预览缩放比例"
        @change="commit" @keydown.enter.prevent="commit" @keydown.esc.stop.prevent="restore">
      <span>%</span>
    </label>
    <button type="button" class="button small" aria-label="放大预览" title="放大预览" :disabled="!fit && percent >= 10000" @click="emit('scale', (percent + 25) / 100)">＋</button>
    <button type="button" class="button small" :aria-pressed="fit" @click="emit('scale', null)">{{ fitLabel || '适应宽度' }}</button>
    <button type="button" class="button small" @click="emit('scale', 1)">100%</button>
  </div>
</template>

<style scoped>
.preview-zoom, .zoom-percent { display: inline-flex; align-items: center; gap: .375rem; }
.preview-zoom { flex-wrap: wrap; margin-inline-start: auto; }
.zoom-percent { margin: 0; gap: .2rem; }
.zoom-percent input { width: 6.5em; padding: .25rem .375rem; text-align: center; font-variant-numeric: tabular-nums; }
.preview-zoom [aria-pressed="true"] { color: var(--accent); background: var(--accent-subtle); }
</style>
