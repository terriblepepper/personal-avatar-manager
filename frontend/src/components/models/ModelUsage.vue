<script setup lang="ts">
import { useConfirmation } from '../../composables/useConfirmation'
import RecordPagination from '../RecordPagination.vue'
import { computed, onMounted, ref } from 'vue'
import AppDialog from '../AppDialog.vue'
import { date } from '../../utils/format'
import { request, notifyError } from '../../api/client'
const confirmAction = useConfirmation()
interface Call {id:number;model:string;provider:string;purpose:string;created_at:string;usage:{input_tokens?:number;output_tokens?:number;total_tokens?:number}}
interface Page {items:Call[];total:number;offset:number}
const rows=ref<Call[]>([]), total=ref(0), offset=ref(0), search=ref(''), selected=ref<number[]>([]), detail=ref<Call|null>(null)
const allSelected = computed(() => !!rows.value.length && rows.value.every(row=>selected.value.includes(row.id)))
function togglePage() { selected.value = allSelected.value ? [] : rows.value.map(row=>row.id) }
let sequence=0
async function load(next=0){const ticket=++sequence;try{const data=await request<Page>('/records/model_calls?'+new URLSearchParams({offset:String(next),search:search.value}));if(ticket!==sequence)return;rows.value=data.items;total.value=data.total;offset.value=data.offset;selected.value=[]}catch(e){notifyError(e)}}
async function remove(all: boolean) {
  if (!all && !selected.value.length) return
  await confirmAction(all ? '删除所有分页的模型调用记录？' : '删除所选模型调用记录？', '确认删除',
    () => [offset.value, search.value, all ? null : selected.value], async () => {
      await request('/records/model_calls/delete', 'POST', { all, ids: selected.value.map(String) })
      await load(all ? 0 : offset.value)
    })
}
onMounted(()=>load())
</script>
<template>
  <section class="card">
    <div class="cardhead">
      <h2>最近模型调用</h2>
      <div class="admin-row-actions">
        <button type="button" class="button small danger" :disabled="!selected.length" @click="remove(false)">删除选中</button>
        <button type="button" class="button small danger" :disabled="!total" @click="remove(true)">删除全部记录</button>
      </div>
    </div>
    <div v-if="rows.length" class="tablewrap compact-record-table">
      <table class="model-usage-table">
        <thead><tr><th><input type="checkbox" :checked="allSelected" aria-label="选择本页模型调用" @change="togglePage"> 模型</th><th>调用时间</th><th>用途</th><th>总 Token</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td><label><input v-model="selected" type="checkbox" :value="row.id" :aria-label="'选择 '+row.model"><span><strong>{{ row.model }}</strong><small>{{ row.provider }}</small></span></label></td>
            <td>{{ date(row.created_at) }}</td><td>{{ row.purpose }}</td><td>{{ row.usage?.total_tokens ?? '未报告' }}</td>
            <td><button type="button" class="button small" @click="detail=row">详情</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="sub">{{ search?'没有匹配的模型调用。':'暂无模型调用。' }}</p>
    <RecordPagination :offset="offset" :total="total" :search="search" label="模型调用分页" @update:offset="load($event)" @update:search="search=$event;load()"/>
  </section>
  <AppDialog :model-value="!!detail" title="模型调用" append-to-body @close="detail=null">
    <dl v-if="detail" class="acs-deployment-fields">
      <dt>模型</dt><dd>{{ detail.model }}</dd><dt>平台</dt><dd>{{ detail.provider }}</dd>
      <dt>调用时间</dt><dd>{{ date(detail.created_at) }}</dd><dt>用途</dt><dd>{{ detail.purpose }}</dd>
      <dt>输入 Token</dt><dd>{{ detail.usage?.input_tokens ?? '未报告' }}</dd>
      <dt>输出 Token</dt><dd>{{ detail.usage?.output_tokens ?? '未报告' }}</dd>
      <dt>总 Token</dt><dd>{{ detail.usage?.total_tokens ?? '未报告' }}</dd>
    </dl>
  </AppDialog>
</template>
