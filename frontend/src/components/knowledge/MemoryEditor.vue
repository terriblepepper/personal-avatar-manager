<script setup lang="ts">
import RecordPagination from '../RecordPagination.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElButton, ElCheckbox, ElInput, ElMessage } from 'element-plus'
import { request, notifyError } from '../../api/client'
import type { Build, MemoryNode, MemoryTree } from './types'
const props = defineProps<{ build: Build }>()
const emit = defineEmits<{ saved: [] }>()
const tree = ref<MemoryTree | null>(null)
const originals = ref<MemoryNode[]>([])
const search=ref(''),offset=ref(0)
const filtered=computed(()=>tree.value?.nodes.filter(node=>`${node.title} ${node.summary} ${node.content}`.toLowerCase().includes(search.value.toLowerCase()))||[])
const loading = ref(false)
const snapshot = ref<{id:string;revision:string;document:Record<string,string>}|null>(null)
const opened = ref(false), reloadVersion = ref(0)
const stale = computed(() => snapshot.value !== null && snapshot.value.revision !== props.build.updated_at)
let disposed = false
onBeforeUnmount(() => { disposed = true })
function discard() {
  tree.value = null
  snapshot.value = null
  originals.value = []
  offset.value = 0
}
function reload() { discard(); reloadVersion.value++ }
watch([opened, () => props.build.id, () => props.build.updated_at, reloadVersion], async ([open,id,revision], _, onCleanup) => {
  if (snapshot.value && snapshot.value.id !== id) discard()
  if (!open || tree.value) return
  const controller = new AbortController(), document = {...props.build.document}
  onCleanup(() => controller.abort())
  try {
    const value = await request<MemoryTree>(`/me/knowledge/builds/${encodeURIComponent(id)}/memory-tree`, 'GET', undefined, controller.signal)
    if (controller.signal.aborted || id !== props.build.id || revision !== props.build.updated_at) return
    tree.value = value
    originals.value = value.nodes.map(node => ({...node}))
    snapshot.value = {id,revision,document}
  } catch (error) { if (!controller.signal.aborted) notifyError(error) }
})
function reset(id:string){const original=originals.value.find(node=>node.id===id),index=tree.value?.nodes.findIndex(node=>node.id===id) ?? -1;if(original && tree.value && index>=0)tree.value.nodes[index]={...original}}
async function save() {
  const base = snapshot.value
  if (!tree.value || !base || base.id !== props.build.id || stale.value || loading.value) return
  const edits = tree.value.nodes.filter((node, i) => JSON.stringify(node) !== JSON.stringify(originals.value[i])).map(({id,title,summary,content,deleted}) => ({id,title,summary,content,deleted:!!deleted}))
  if (!edits.length) { ElMessage.info('尚未修改记忆节点'); return }
  loading.value = true
  try {
    await request(`/me/knowledge/builds/${encodeURIComponent(base.id)}/refine`, 'POST', {
      revision:base.revision, document:base.document, memory_edits:edits, memory_revision:tree.value.revision,
    })
    if (disposed) return
    if (snapshot.value === base) discard()
    emit('saved')
    ElMessage.success('记忆修订已加入后台完善')
  } catch (error) { if (!disposed) notifyError(error) } finally { loading.value = false }
}
</script>
<template><details class="soul-memory-tree" @toggle="opened = ($event.currentTarget as HTMLDetailsElement).open"><summary class="button small">管理记忆树</summary><p class="sub">修改随 Soul 修订审核后生效。</p><details v-for="node in filtered.slice(offset,offset+10)" :key="node.id" class="soul-memory-node"><summary>{{ node.group }} · {{ node.title }}</summary><label>标题<ElInput placeholder=" " v-model="node.title" maxlength="200" /></label><label>摘要<ElInput placeholder=" " v-model="node.summary" type="textarea" maxlength="2000" /></label><label>内容<ElInput placeholder=" " v-model="node.content" type="textarea" maxlength="8000" /></label><ElCheckbox v-model="node.deleted">删除此记忆</ElCheckbox><ElButton @click="reset(node.id)">撤销本项修改</ElButton></details><RecordPagination v-if="tree" v-model:offset="offset" :total="filtered.length" :search="search" label="记忆分页" @update:search="search=$event;offset=0"/><p v-if="stale" role="status">构建已更新。请保留需要的修改，再重新载入。</p><ElButton v-if="stale" @click="reload">重新载入记忆树</ElButton><ElButton v-if="tree" :disabled="stale" :loading="loading" @click="save">保存记忆修订</ElButton></details></template>
