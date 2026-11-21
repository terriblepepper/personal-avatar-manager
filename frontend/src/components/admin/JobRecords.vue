<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import { request, notifyError } from '../../api/client'
import { date, statusLabel } from '../../utils/format'
import RecordPagination from '../RecordPagination.vue'

interface Job {id:string; username:string; status:string; action?:string; registration_phase?:string; error?:string; attempts?:number; updated_at:string}
const props=defineProps<{collection:'identity_jobs'|'jobs'}>()
const identity=props.collection==='identity_jobs', title=identity?'身份维护记录':'创建任务'
const actions:Record<string,string>={register:'正式注册',renew:'更新证书',unregister:'注销正式身份'}
const rows=ref<Job[]>([]),selected=ref<string[]>([]),detail=ref<Job|null>(null),busy=ref(false)
const offset=ref(0),total=ref(0),search=ref('')
const deletable=(job:Job)=>job.status==='done'||identity&&job.status==='failed'
const eligible=computed(()=>rows.value.filter(deletable))
const selectedOnPage=computed(()=>eligible.value.filter(job=>selected.value.includes(job.id)).length)
let sequence=0
async function load() {
  const ticket=++sequence
  try {
    const result=await request<{items:Job[];total:number;offset:number}>(`/records/${props.collection}?`+new URLSearchParams({scope:'admin',offset:String(offset.value),search:search.value}))
    if(ticket!==sequence)return
    rows.value=result.items;offset.value=result.offset;total.value=result.total
    const ineligible=new Set(result.items.filter(job=>!deletable(job)).map(job=>job.id))
    selected.value=selected.value.filter(id=>!ineligible.has(id))
  } catch(e){if(ticket===sequence)notifyError(e)}
}
function select(ids:string[],checked:boolean) {
  const marks=new Set(selected.value)
  for(const id of ids){
    if(!checked)marks.delete(id)
    else if(!marks.has(id)){
      if(marks.size>=100){ElMessage.warning('每次最多选择 100 条记录');break}
      marks.add(id)
    }
  }
  selected.value=[...marks]
}
async function remove(ids:string[]|'all') {
  if(busy.value || ids!=='all'&&!ids.length)return
  const target=ids==='all'?'全部分页的'+(identity?'已完成或失败':'已完成')+'记录':ids.length+' 条记录'
  if(!confirm('确认删除'+target+'？任务执行结果保留。'))return
  busy.value=true
  try {
    await request(`/records/${props.collection}/delete`,'POST',ids==='all'?{all:true}:{ids})
    selected.value=ids==='all'?[]:selected.value.filter(id=>!ids.includes(id))
    await load()
    ElMessage.success('记录已删除')
  } catch(e){notifyError(e)} finally {busy.value=false}
}
async function retry(job:Job) {
  if(busy.value)return
  busy.value=true
  try {
    const id=encodeURIComponent(job.id)
    await request(identity?`/admin/agents/identity-jobs/${id}/refresh`:`/admin/jobs/${id}/retry`,'POST',{})
    await load();ElMessage.success('任务已安排')
  } catch(e){notifyError(e)} finally {busy.value=false}
}
onMounted(load)
</script>
<template>
  <section class="card">
    <div class="cardhead"><h2>{{ title }}</h2><button class="button small" :disabled="busy" @click="load">刷新</button></div>
    <p class="sub">{{ identity?'等待中的身份审核与证书每小时自动检查，也可立即检查。':'失败任务可重试，等待中的任务可立即检查。' }}</p>
    <div class="actions notification-batch">
      <label class="privacy"><input type="checkbox" :disabled="busy || !eligible.length" :checked="!!eligible.length && selectedOnPage===eligible.length" :indeterminate="selectedOnPage>0 && selectedOnPage<eligible.length" @change="select(eligible.map(job=>job.id),($event.target as HTMLInputElement).checked)">选择本页</label>
      <span class="sub">已选 {{ selected.length }} 条</span>
      <button class="button small danger" :disabled="busy || !selected.length" @click="remove(selected)">删除所选</button>
      <button class="button small danger" :disabled="busy" @click="remove('all')">删除全部记录</button>
    </div>
    <div v-if="rows.length" class="tablewrap compact-record-table">
      <table><thead><tr><th aria-label="选择记录"></th><th>用户 / 任务</th><th>状态</th><th>执行次数</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody><tr v-for="job in rows" :key="job.id">
          <td><input v-if="deletable(job)" type="checkbox" :checked="selected.includes(job.id)" :disabled="busy" :aria-label="'选择 '+job.username" @change="select([job.id],($event.target as HTMLInputElement).checked)"></td>
          <td>{{ job.username }}<span v-if="job.action"> · {{ actions[job.action] || job.action }}</span><small class="mono">{{ job.id }}</small></td>
          <td><span class="tag" :class="job.status">{{ statusLabel(job.status==='waiting' && job.registration_phase || job.status) }}</span><small v-if="job.error">{{ job.error }}</small></td>
          <td>{{ job.attempts ?? 0 }}</td><td>{{ date(job.updated_at) }}</td>
          <td><div class="admin-row-actions">
            <button v-if="identity" class="button small" @click="detail=job">详情</button>
            <button v-if="job.status==='waiting' || !identity && job.status==='failed'" class="button small" :disabled="busy" @click="retry(job)">{{ job.status==='waiting'?'立即检查':'重试任务' }}</button>
            <button v-if="deletable(job)" class="button small danger" :disabled="busy" @click="remove([job.id])">删除记录</button>
          </div></td>
        </tr></tbody></table>
    </div>
    <p v-else class="sub">暂无符合条件的记录。</p>
    <RecordPagination :offset="offset" :total="total" :search="search" :label="(identity?'身份维护':'创建任务')+'分页'" @update:offset="offset=$event;load()" @update:search="search=$event;offset=0;selected=[];load()"/>
  </section>
  <AppDialog :model-value="!!detail" title="身份维护详情" @close="detail=null">
    <dl v-if="detail" class="kv"><dt>任务 ID</dt><dd>{{ detail.id }}</dd><dt>账号</dt><dd>{{ detail.username }}</dd><dt>操作</dt><dd>{{ actions[detail.action || ''] || detail.action }}</dd><dt>状态</dt><dd>{{ statusLabel(detail.status) }}</dd><dt>更新时间</dt><dd>{{ date(detail.updated_at) }}</dd><template v-if="detail.error"><dt>原因</dt><dd>{{ detail.error }}</dd></template></dl>
  </AppDialog>
</template>
