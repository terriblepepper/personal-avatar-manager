<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { clockTime, timestamp, type TrendPoint } from './metrics'

const props = defineProps<{
  title: string
  unit: string
  rows: TrendPoint[]
  bucketMinutes: number
  format: (value: number) => string
  expanded?: boolean
}>()
const selected = defineModel<string>('selected', { required: true })
const svg = ref<SVGSVGElement>()
const width = ref(380)
const hovering = ref(false)

watch(svg, (element, _, onCleanup) => {
  if (!element) return
  const observer = new ResizeObserver(([entry]) => {
    if (entry.contentRect.width) width.value = Math.round(entry.contentRect.width)
  })
  observer.observe(element)
  onCleanup(() => observer.disconnect())
}, { flush: 'post' })

const times = computed(() => props.rows.map(row => Date.parse(row.time)))
const height = computed(() => props.expanded ? 310 : 174)
const topValue = computed(() => {
  const largest = Math.max(1, ...props.rows.map(row => row.value))
  const scale = 10 ** Math.floor(Math.log10(largest / 3))
  return Math.max(1, Math.ceil(largest / (3 * scale)) * scale) * 3
})
const left = computed(() => Math.max(45, props.format(topValue.value).length * 7 + 12))
const right = computed(() => width.value - 8)
const bottom = computed(() => height.value - 36)
const start = computed(() => times.value[0] ?? 0)
const span = computed(() => (times.value.at(-1) ?? 0) - start.value)
const points = computed(() => props.rows.map((row, i) => ({
  ...row,
  x: span.value ? left.value + (right.value - left.value) * (times.value[i] - start.value) / span.value :
    (left.value + right.value) / 2,
  y: bottom.value - (bottom.value - 13) * row.value / topValue.value,
})))
const lines = computed(() => {
  const segments: typeof points.value[] = []
  points.value.forEach((point, i) => {
    if (!i || times.value[i] - times.value[i - 1] > props.bucketMinutes * 60000) segments.push([])
    segments.at(-1)!.push(point)
  })
  return segments
})
const activeIndex = computed(() => {
  const index = props.rows.findIndex(row => row.time === selected.value)
  return index < 0 ? props.rows.length - 1 : index
})
const active = computed(() => points.value[activeIndex.value])
const ticksY = computed(() => Array.from({ length: 4 }, (_, index) => ({
  y: bottom.value - (bottom.value - 13) * index / 3,
  label: props.format(topValue.value * index / 3),
})))
const spansDay = computed(() => Math.floor((start.value + 28800000) / 86400000) !==
  Math.floor((start.value + span.value + 28800000) / 86400000))
const ticksX = computed(() => {
  if (points.value.length <= 1) return points.value
  const space = right.value - left.value
  const fit = Math.max(2, Math.floor(space / (spansDay.value ? 110 : 65)))
  const count = Math.min(points.value.length, fit, 5)
  return Array.from({ length: count }, (_, index) => {
    const fraction = index / (count - 1)
    return { x: left.value + space * fraction, time: new Date(start.value + span.value * fraction).toISOString() }
  })
})
const tooltipX = computed(() => Math.min(width.value - 147, Math.max(4, (active.value?.x ?? 0) + 9)))
const tooltipY = computed(() => Math.max(3, (active.value?.y ?? 0) - 49))

function choose(event: PointerEvent) {
  const bounds = svg.value!.getBoundingClientRect()
  const x = (event.clientX - bounds.left) * width.value / bounds.width
  const y = (event.clientY - bounds.top) * height.value / bounds.height
  hovering.value = x >= left.value && x <= right.value && y >= 8 && y <= bottom.value
  if (!hovering.value || !points.value.length) return
  const nearest = points.value.reduce((best, point) =>
    Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best)
  selected.value = nearest.time
}
</script>

<template>
  <div v-if="active" class="trend-plot">
    <svg ref="svg" :viewBox="'0 0 ' + width + ' ' + height" role="img"
         :aria-label="title + '趋势，纵轴 ' + unit + '，横轴北京时间'"
         @pointermove="choose" @pointerdown="choose" @pointerleave="hovering = false">
      <g v-for="tick in ticksY" :key="tick.y" class="axis-tick">
        <path :d="'M' + left + ' ' + tick.y + 'H' + right" class="grid"/>
        <text :x="left - 6" :y="tick.y" text-anchor="end" dominant-baseline="middle">{{ tick.label }}</text>
      </g>
      <path :d="'M' + left + ' 9V' + bottom + 'H' + right" class="plot-axis"/>
      <g v-for="(tick, index) in ticksX" :key="tick.time" class="axis-tick">
        <path :d="'M' + tick.x + ' ' + bottom + 'v5'" class="plot-axis"/>
        <text :x="tick.x" :y="bottom + 17"
              :text-anchor="ticksX.length === 1 ? 'middle' : index === 0 ? 'start' : index === ticksX.length - 1 ? 'end' : 'middle'">
          {{ spansDay ? timestamp(tick.time) : clockTime(tick.time) }}
        </text>
      </g>
      <text class="axis-label" :x="(left + right) / 2" :y="height - 3" text-anchor="middle">北京时间</text>
      <template v-for="segment in lines" :key="segment[0].time">
        <polyline v-if="segment.length > 1" :points="segment.map(point => point.x + ',' + point.y).join(' ')" class="trace"/>
        <circle v-else :cx="segment[0].x" :cy="segment[0].y" r="2.5" class="dot"/>
      </template>
      <line :x1="active.x" :x2="active.x" y1="13" :y2="bottom" class="crosshair"/>
      <circle :cx="active.x" :cy="active.y" r="4" class="point-focus"/>
      <g v-if="hovering" :transform="'translate(' + tooltipX + ',' + tooltipY + ')'" class="plot-tooltip" aria-hidden="true">
        <rect width="143" height="42" rx="6"/>
        <text x="9" y="15">{{ timestamp(active.time) }}</text>
        <text x="9" y="32">{{ format(active.value) }}</text>
      </g>
    </svg>
    <div class="chart-value">
      <span>{{ timestamp(active.time) }}</span>
      <strong>{{ format(active.value) }}</strong>
      <span v-if="active.sample_minutes < bucketMinutes">已采样 {{ active.sample_minutes }} 分钟</span>
      <input v-if="rows.length > 1" type="range" min="0" :max="rows.length - 1"
             :value="activeIndex" :aria-label="title + '的时间点'"
             :aria-valuetext="timestamp(active.time) + ' ' + format(active.value)"
             @input="selected = rows[Number(($event.target as HTMLInputElement).value)].time">
    </div>
  </div>
  <p v-else class="chart-empty">等待完整采样周期</p>
</template>
