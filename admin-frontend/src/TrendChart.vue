<script setup lang="ts">
import { computed, ref } from 'vue'
import { timestamp } from './metrics'

const props = defineProps<{
  title: string
  unit: string
  rows: { time: string; value: number; sample_minutes: number }[]
  bucketMinutes: number
  format: (value: number) => string
}>()
const choice = ref<number | null>(null)
const times = computed(() => props.rows.map(row => Date.parse(row.time)))
const last = computed(() => props.rows.at(-1))
const peak = computed(() => Math.max(1, ...props.rows.map(row => row.value)))
const active = computed(() => props.rows[choice.value ?? props.rows.length - 1] || last.value)
const lines = computed(() => {
  if (!times.value.length) return []
  const first = times.value[0], span = Math.max(1, times.value.at(-1)! - first)
  const paths: string[][] = [[]]
  props.rows.forEach((row, i) => {
    if (i && times.value[i] - times.value[i - 1] > props.bucketMinutes * 60000) paths.push([])
    const x = 4 + 392 * (times.value[i] - first) / span
    const y = 116 - 108 * row.value / peak.value
    paths.at(-1)!.push(x.toFixed(2) + ',' + y.toFixed(2))
  })
  return paths
})
</script>

<template>
  <section class="panel trend">
    <div class="section-title">
      <h2>{{ title }}</h2>
      <span>{{ unit }} · 每 {{ bucketMinutes }} 分钟</span>
    </div>
    <template v-if="last">
      <div class="chart">
        <svg viewBox="0 0 400 120" preserveAspectRatio="none" role="img"
             :aria-label="title + '，最新值 ' + format(last.value)">
          <path class="grid" d="M0 116h400 M0 80h400 M0 44h400 M0 8h400"/>
          <template v-for="(line, i) in lines" :key="i">
            <polyline v-if="line.length > 1" :points="line.join(' ')" class="trace"/>
            <circle v-else :cx="line[0].split(',')[0]" :cy="line[0].split(',')[1]" r="2.4" class="dot"/>
          </template>
        </svg>
        <div class="chart-axis"><span>{{ timestamp(rows[0].time) }}</span><span>{{ timestamp(last.time) }}</span></div>
      </div>
      <div v-if="active" class="chart-value">
        <span>{{ timestamp(active.time) }}</span>
        <strong>{{ format(active.value) }}</strong>
        <span v-if="active.sample_minutes < bucketMinutes">已采样 {{ active.sample_minutes }} 分钟</span>
        <input v-if="rows.length > 1" type="range" min="0" :max="rows.length - 1"
               :value="choice ?? rows.length - 1" :aria-label="title + '的时间点'"
               @input="choice = Number(($event.target as HTMLInputElement).value)">
      </div>
    </template>
    <p v-else class="chart-empty">等待完整采样周期</p>
  </section>
</template>
