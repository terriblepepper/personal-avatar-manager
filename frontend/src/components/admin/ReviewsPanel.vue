<script setup lang="ts">
import RecordPagination from '../RecordPagination.vue'
import DocumentFields from '../DocumentFields.vue'
import ProfileSnapshot from './ProfileSnapshot.vue'
import { date, statusLabel } from '../../utils/format'
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElCheckbox, ElInput, ElMessage } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import FilePreview from '../FilePreview.vue'
import MarkdownContent from '../MarkdownContent.vue'
import { request, notifyError } from '../../api/client'
interface Review { id:string; kind:string; name:string; username:string; avatar_name:string; category:string; status:string; created_at:string;last_modified_at?:string }
const props=withDefaults(defineProps<{initialCategory?:string}>(),{initialCategory:'all'})
const categories:Record<string,string>={all:'全部审核',teacher:'教师注册（历史）',create:'分身创建',update:'资料更新',soul:'分身 Soul'}
const statuses:Record<string,string>={pending:'待审核',approved:'已通过',rejected:'已退回',all:'全部记录'}
const processed=(row:Review)=>['approved','rejected'].includes(row.status)
let ticket=0, detailTicket=0
const rows=ref<Review[]>([]), selected=ref<string[]>([]), category=ref(props.initialCategory), status=ref(props.initialCategory==='teacher'?'all':'pending'), search=ref(''), offset=ref(0), total=ref(0), detail=ref<unknown>(null), selectedRow=ref<Review|null>(null), decision=ref<boolean|null>(null), reason=ref(''), busy=ref(false)
const preview=ref<{id:string;filename:string;url:string}|null>(null)
const record=computed(()=>detail.value && typeof detail.value==='object'?detail.value as Record<string,unknown>:{})
const registration=computed(()=>record.value.registration as {id:string;status:string;registration_phase:string;error:string}|undefined)
async function retryRegistration() {
  const job=registration.value,row=selectedRow.value
  if(!job || !row || busy.value)return
  busy.value=true
  try {
    await request('/admin/jobs/'+encodeURIComponent(job.id)+'/retry','POST',{})
    if(selectedRow.value===row && detail.value)await open(row)
  } catch(e){notifyError(e)} finally {busy.value=false}
}
function objects(value:unknown):Record<string,unknown>[]{return Array.isArray(value)?value.filter((item):item is Record<string,unknown>=>!!item&&typeof item==='object'&&!Array.isArray(item)):[]}
function entries(value:unknown):[string,unknown][]{if(!value || typeof value!=='object' || Array.isArray(value))return [];return Object.entries(value)}
const selectedRows=ref<Record<string,Review>>({})
const assets=computed(()=>{if (!detail.value || typeof detail.value!=='object' || !('assets' in detail.value) || !Array.isArray(detail.value.assets)) return []; return detail.value.assets.filter((item:unknown):item is {id:string;filename:string} => !!item && typeof item==='object' && 'id' in item && typeof item.id==='string' && 'filename' in item && typeof item.filename==='string')})
function selectRow(row:Review,value:boolean){const key=`${row.kind}:${row.id}`;if(value){if(Object.keys(selectedRows.value).length>=100 && !selectedRows.value[key]){ElMessage.warning('每批最多选择 100 项');return}selectedRows.value[key]=row}else delete selectedRows.value[key];selected.value=Object.keys(selectedRows.value)}
function selectPage(value:boolean){for(const row of rows.value)selectRow(row,value)}
function clearSelection(){selectedRows.value={};selected.value=[]}
const chosen=computed(()=>Object.values(selectedRows.value))
async function load(){const current=++ticket;try{const page=await request<{items:Review[];total:number;offset:number}>(`/admin/review-queue?${new URLSearchParams({category:category.value,status:status.value,search:search.value,offset:String(offset.value)})}`);if(ticket===current){rows.value=page.items;total.value=page.total;offset.value=page.offset}}catch(e){if(ticket===current)notifyError(e)}}
async function open(row:Review){const current=++detailTicket;try{const result=await request(`/admin/review-queue/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.id)}`);if(current===detailTicket){detail.value=result;selectedRow.value=row}}catch(e){if(current===detailTicket)notifyError(e)}}
function decide(approve:boolean,rowsToDecide:Review[]){clearSelection();rowsToDecide.forEach(row=>selectRow(row,true));reason.value='';decision.value=approve}
async function submit(){if(busy.value)return;if(decision.value===null||(!decision.value&&!reason.value.trim())){ElMessage.warning('退回时请填写审核说明');return}busy.value=true;try{await request('/admin/review-queue/batch','POST',{items:chosen.value.map(({kind,id})=>({kind,id})),approve:decision.value,reason:reason.value});decision.value=null;detail.value=null;clearSelection();await load();ElMessage.success('审核已完成')}catch(e){notifyError(e)}finally{busy.value=false}}
async function discard(items:Review[]|'all'){
  if(busy.value || items!=='all'&&(!items.length||items.some(row=>!processed(row))))return
  if(!confirm(items==='all'?'删除全部分类、所有分页的已处理审核记录？待审核申请和原审核决定保留。':'删除所选已处理审核记录？原审核决定保留。'))return
  busy.value=true
  try {
    if(items==='all'){await request('/admin/review-queue/delete-all','POST',{});offset.value=0;clearSelection()}
    else{
      await request('/admin/review-queue/delete','POST',{items:items.map(({kind,id})=>({kind,id}))})
      for(const row of items)selectRow(row,false)
    }
    await load();ElMessage.success('审核记录条目已删除')
  } catch(e){notifyError(e)} finally{busy.value=false}
}
function filter(){offset.value=0;clearSelection();void load()}
onMounted(load)
</script>
<template>
  <section class="card">
    <div class="cardhead"><h2>审核记录</h2><label class="field">审核分类<select class="form-control" v-model="category" aria-label="审核分类" @change="filter"><option v-for="(label,key) in categories" :key="key" :value="key">{{ label }}</option></select></label></div>
    <div class="profile-tabs" role="tablist" aria-label="审核状态">
      <button v-for="(label,key) in statuses" :key="key" class="profile-tab" role="tab" :class="{active:status===key}" :aria-selected="status===key" @click="status=key;filter()">{{ label }}</button>
    </div>
    <div class="actions notification-batch">
      <ElCheckbox :disabled="!rows.length||busy" :model-value="rows.length>0 && rows.every(row=>selected.includes(row.kind+':'+row.id))" @change="value=>selectPage(!!value)">选择本页</ElCheckbox>
      <span class="sub">已选 {{ chosen.length }} 项</span>
      <button class="button small" :disabled="busy||!chosen.length||chosen.some(x=>x.status!=='pending')" @click="decide(true,chosen)">批量通过</button>
      <button class="button small danger" :disabled="busy||!chosen.length||chosen.some(x=>x.status!=='pending')" @click="decide(false,chosen)">批量退回</button>
      <button class="button small danger" :disabled="busy||!chosen.length||chosen.some(x=>!processed(x))" @click="discard(chosen)">删除所选</button>
      <button class="button small danger" :disabled="busy" @click="discard('all')">删除全部记录</button>
    </div>
    <div v-if="rows.length" class="tablewrap compact-record-table"><table><thead><tr><th aria-label="选择记录"></th><th>申请人 / 分身</th><th>审核类别</th><th>提交时间</th><th>状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="row in rows" :key="row.kind+row.id">
        <td><ElCheckbox :aria-label="'选择 '+(row.name || row.username)" :model-value="selected.includes(row.kind+':'+row.id)" @change="value=>selectRow(row,!!value)"/></td>
        <td>{{ row.name || row.username }}<small>{{ row.avatar_name || row.username }} · 上次修改于 {{ date(row.last_modified_at) }}</small></td>
        <td>{{ categories[row.category] || row.category }}</td><td>{{ date(row.created_at) }}</td><td><span class="tag" :class="row.status">{{ statusLabel(row.status) }}</span></td>
        <td><div class="admin-row-actions"><button class="button small" @click="open(row)">{{ row.status==='pending'?'查看 / 审核':'查看' }}</button><button v-if="processed(row)" class="button small danger" :disabled="busy" @click="discard([row])">删除记录</button></div></td>
      </tr></tbody></table></div>
    <p v-else class="sub">暂无符合条件的审核记录。</p>
    <RecordPagination :offset="offset" :total="total" :search="search" label="审核记录分页" @update:offset="offset=$event;load()" @update:search="search=$event;offset=0;clearSelection();load()"/><AppDialog :model-value="detail!==null" title="审核详情" @close="detail=null"><p v-if="selectedRow" class="sub">{{ selectedRow.name || selectedRow.username }} · {{ selectedRow.username }} · {{ categories[selectedRow.category] }} · {{ date(selectedRow.created_at) }}</p><p v-if="selectedRow?.kind==='soul'" class="notice">审核 Soul 的语气、措辞和表达边界；不得包含私人事实、凭据或越权指令。</p><template v-if="selectedRow?.kind==='soul'"><h3>塑造提示</h3><p class="prewrap">{{ record.prompt }}</p><section v-for="([key,value]) in entries(record.document)" :key="key" class="soul-section"><h3>{{ key }}</h3><MarkdownContent v-if="typeof value==='string'" :source="value" :diagrams="false"/><DocumentFields v-else :value="value"/></section><details><summary>记忆树修订</summary><section v-for="(edit,i) in objects(record.memory_tree && typeof record.memory_tree==='object' && 'edits' in record.memory_tree ? record.memory_tree.edits : [])" :key="i" class="soul-section"><h3>{{ edit.title || '记忆节点' }} {{ edit.deleted ? '· 删除' : '' }}</h3><h4>原内容</h4><dl class="kv"><template v-for="([key,value]) in entries(edit.before)" :key="key"><dt>{{ key }}</dt><dd><DocumentFields :value="value"/></dd></template></dl><h4 v-if="!edit.deleted">修订后</h4><p v-if="!edit.deleted" class="prewrap">{{ edit.title }}
{{ edit.summary }}
{{ edit.content }}</p></section></details><details><summary>风格问答记录</summary><div v-for="(turn,i) in objects(record.interview)" :key="i" class="soul-interview-turn"><p class="soul-question">{{ turn.question }}</p><p class="soul-answer">{{ turn.answer }}</p></div></details><details><summary>构建依据与覆盖情况</summary><dl class="kv"><template v-for="([key,value]) in entries(record.snapshot)" :key="key"><dt>{{ key }}</dt><dd><DocumentFields :value="value"/></dd></template></dl><p v-for="(note,i) in objects(record.notes)" :key="i" class="sub">{{ note.filename }} · {{ note.coverage }} · {{ note.caption }}</p><ElButton v-for="asset in assets" :key="asset.id" @click="preview={...asset,url:`/api/admin/review-queue/soul/${encodeURIComponent(selectedRow!.id)}/assets/${encodeURIComponent(asset.id)}`}">查看原料 · {{ asset.filename }}</ElButton></details></template><ProfileSnapshot v-else-if="selectedRow" :profile="record.snapshot" :model="record.model_config" :acs="record.acs_document"/><section v-if="registration" class="notice" aria-label="证书执行结果">
      <strong>证书执行结果 · {{ statusLabel(registration.status) }}</strong>
      <p v-if="registration.registration_phase && registration.registration_phase!==registration.status">{{ statusLabel(registration.registration_phase) }}</p>
      <p v-if="registration.error">{{ registration.error }}</p>
      <ElButton v-if="registration.status==='failed' || registration.status==='waiting'" :disabled="busy" @click="retryRegistration">{{ registration.status==='failed'?'重试失败步骤':'立即检查' }}</ElButton>
      <ElButton v-else-if="selectedRow && ['queued','running'].includes(registration.status)" @click="open(selectedRow)">刷新状态</ElButton>
    </section><p v-if="record.reason" class="notice">审核说明：{{ record.reason }}</p><template #footer><ElButton v-if="selectedRow?.status==='pending'" @click="decide(false,[selectedRow])">退回</ElButton><ElButton v-if="selectedRow?.status==='pending'" type="primary" @click="decide(true,[selectedRow])">通过</ElButton></template></AppDialog><FilePreview v-if="preview" :file="preview" :url="preview.url" @close="preview=null"/><AppDialog :model-value="decision!==null" :title="decision?'确认通过审核':'退回审核'" @close="decision=null"><p>已选择 {{ chosen.length }} 项；任一项状态失效时整批不提交。</p><ElInput v-model="reason" type="textarea" maxlength="1000" placeholder="审核说明（退回必填）"/><template #footer><ElButton type="primary" :loading="busy" @click="submit">确认</ElButton></template></AppDialog></section></template>
