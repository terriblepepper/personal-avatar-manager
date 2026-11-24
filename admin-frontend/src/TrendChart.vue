<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { timestamp, type Metric, type TrendPoint } from './metrics'
import { fetchMinutes } from './useMonitor'
import TrendPlot from './TrendPlot.vue'

const props = defineProps<{
  title: string
  unit: string
  metric: Metric
  rows: TrendPoint[]
  bucketMinutes: number
  format: (value: number) => string
}>()
const dialog = ref<HTMLDialogElement>()
const open = ref(false)
const selection = ref('')
const selectedPeriod = ref('')
const detailSelection = ref('')
const minutes = ref<TrendPoint[]>([])
const loading = ref(false)
const failed = ref(false)
const group = computed(() => props.rows.find(row => row.time === selectedPeriod.value) ?? props.rows.at(-1))
const plotted = computed(() => props.bucketMinutes === 1 ? props.rows : minutes.value)
const titleId = computed(() => 'expanded-' + props.metric)

function expand() {
  selectedPeriod.value = selection.value
  detailSelection.value = props.bucketMinutes === 1 ? selection.value : ''
  dialog.value!.showModal()
  open.value = true
}

watch([open, () => group.value?.time, () => group.value?.sample_minutes, () => props.metric, () => props.bucketMinutes],
      async ([opened, period, , metric, count], previous, onCleanup) => {
  if (!opened || !period || count === 1) return
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  const changed = !previous[0] || period !== previous[1] || metric !== previous[3]
  if (changed) {
    minutes.value = []
    detailSelection.value = ''
    loading.value = true
  }
  failed.value = false
  try {
    const points = await fetchMinutes(period, count, controller.signal)
    if (!controller.signal.aborted) minutes.value = points.map(point => ({ ...point, value: point[metric] }))
  } catch {
    if (!controller.signal.aborted) failed.value = true
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
})
</script>

<template>
  <section class="panel trend">
    <div class="section-title">
      <h2>{{ title }}</h2>
      <div class="chart-actions">
        <span>{{ unit }} · 每 {{ bucketMinutes }} 分钟</span>
        <button v-if="rows.length" class="chart-zoom" type="button" :aria-label="'放大' + title" @click="expand">放大</button>
      </div>
    </div>
    <TrendPlot v-model:selected="selection" v-bind="{ title, unit, rows, bucketMinutes, format }"/>
    <dialog ref="dialog" class="chart-dialog" :aria-labelledby="titleId" @close="open = false"
            @click.self="dialog?.close()">
      <div class="chart-dialog-content">
        <div class="section-title">
          <h2 :id="titleId">{{ title }} · 每分钟</h2>
          <button class="button" type="button" autofocus @click="dialog?.close()">关闭</button>
        </div>
        <label v-if="bucketMinutes > 1 && group" class="chart-period">
          汇总时段
          <select :value="group.time" @change="selectedPeriod = ($event.target as HTMLSelectElement).value">
            <option v-for="row in rows" :key="row.time" :value="row.time">{{ timestamp(row.time) }}</option>
          </select>
        </label>
        <p v-if="bucketMinutes > 1 && loading" class="chart-empty" role="status">正在读取分钟数据…</p>
        <p v-else-if="bucketMinutes > 1 && failed" class="chart-empty" role="alert">暂时无法读取分钟数据。</p>
        <TrendPlot v-else v-model:selected="detailSelection" :title="title" :unit="unit"
                   :rows="plotted" :bucket-minutes="1" :format="format" expanded/>
      </div>
    </dialog>
  </section>
</template>
