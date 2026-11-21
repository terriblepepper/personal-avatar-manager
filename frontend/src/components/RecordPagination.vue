<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'

const props = withDefaults(defineProps<{
  offset: number; total: number; pageSize?: number; label?: string; unit?: string; search?: string;
}>(), { label: '分页', unit: '条', pageSize: 10 })
const emit = defineEmits<{ 'update:offset': [value: number]; 'update:search': [value: string, direction: 1 | -1] }>()
const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const current = computed(() => Math.min(pages.value, Math.floor(props.offset / props.pageSize) + 1))
const searchLabel = computed(() => '搜索' + (props.label.replace(/分页$/, '') || '记录'))
const jump = ref<number | string>(1)
const query = ref(props.search || ''), showSearch = ref(!!props.search)
const searchInput = ref<HTMLInputElement>(), searchTrigger = ref<HTMLButtonElement>()
watch(current, page => { jump.value = page }, { immediate: true })
watch(() => props.search, value => { query.value = value || '' })

function go(page: number) {
  const target = Math.max(1, Math.min(pages.value, Math.trunc(page) || 1))
  jump.value = target
  emit('update:offset', (target - 1) * props.pageSize)
}
async function toggleSearch() {
  showSearch.value = !showSearch.value
  await nextTick()
  if (showSearch.value) searchInput.value?.focus()
  else searchTrigger.value?.focus()
}
function applySearch(direction: 1 | -1 = 1) { emit('update:search', query.value.trim(), direction) }
function closeSearch() { query.value = ''; applySearch(); void toggleSearch() }
</script>

<template>
  <nav class="record-pagination" :aria-label="label">
    <span>共 {{ total }} {{ unit }} · 第 {{ current }} / {{ pages }} {{ unit === '章' ? '章' : '页' }}</span>
    <div class="record-controls">
      <div v-if="search!==undefined" class="record-search-anchor">
        <button ref="searchTrigger" type="button" class="button small icon-button"
        :aria-expanded="showSearch" :aria-label="searchLabel" :title="searchLabel" @click="toggleSearch"><AppIcon name="search" :size="14"/></button>
        <Transition name="pager-search">
          <input v-if="showSearch" ref="searchInput" v-model="query" class="form-control record-search"
        type="search" :aria-label="searchLabel+'内容'" placeholder="搜索…" maxlength="100"
        @keydown.enter.prevent="applySearch($event.shiftKey ? -1 : 1)" @search="applySearch()" @keydown.esc.stop.prevent="closeSearch">
        </Transition>
      </div>
      <button type="button" class="button small" :disabled="current <= 1" @click="go(current - 1)">{{ unit === '章' ? '上一章' : '上一页' }}</button>
      <input placeholder=" " v-model.number="jump" class="form-control record-jump" type="number" min="1" :max="pages" :aria-label="unit === '章' ? '跳转章节' : '跳转页数'" :title="unit === '章' ? '输入章节后按回车' : '输入页码后按回车'" @keydown.enter.prevent="go(Number(jump))">
      <button type="button" class="button small" :disabled="current >= pages" @click="go(current + 1)">{{ unit === '章' ? '下一章' : '下一页' }}</button>
    </div>
  </nav>
</template>
