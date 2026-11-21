<script setup lang="ts">
import RecordPagination from '../components/RecordPagination.vue'
import DocumentFields from '../components/DocumentFields.vue'
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import AppDialog from '../components/AppDialog.vue'
import { request, notifyError } from '../api/client'
import { date, fileSizeMB, statusLabel } from '../utils/format'
interface Row {id:string;status:string;last_modified_at:string;submitted_at:string;reviewer_name?:string;reason?:string;snapshot:Record<string,unknown>;acs_document?:unknown;attachments?:unknown}
interface Field {key:string;label:string;audience:string;visibility:string;fixed_public:boolean}
interface Attachment {id:string;filename:string;visibility:string;size:number}
interface Page {items:Row[];total:number;offset:number;limit:number}
const fields=ref<Field[]>([])
const rows=ref<Row[]>([]), total=ref(0), offset=ref(0), search=ref(''), selected=ref<string[]>([]), detail=ref<Row|null>(null)
const selectable=computed(()=>rows.value.filter(row=>['approved','rejected'].includes(row.status)))
const allSelected=computed(()=>selectable.value.length>0 && selectable.value.every(row=>selected.value.includes(row.id)))
function togglePage(){ selected.value = allSelected.value ? [] : selectable.value.map(row=>row.id) }
let sequence=0
const loading = ref(false), failed = ref(false)
async function load(next = 0) {
  const ticket = ++sequence
  loading.value = true
  failed.value = false
  try {
    const query = new URLSearchParams({ offset: String(next), search: search.value })
    const page = await request<Page>('/records/history?' + query)
    if (ticket !== sequence) return
    rows.value = page.items
    total.value = page.total
    offset.value = page.offset
    selected.value = []
  } catch (error) {
    if (ticket === sequence) { failed.value = true; notifyError(error) }
  } finally {
    if (ticket === sequence) loading.value = false
  }
}
async function remove(all=false,ids=selected.value){try{await ElMessageBox.confirm(all?'删除所有分页中已完成的记录？待审核申请保留。':'删除选中的已完成记录？','确认删除');await request('/records/history/delete','POST',{all,ids});await load(all?0:offset.value)}catch(e){if(e!=='cancel'&&e!=='close')notifyError(e)}}
function visibleFields(row:Row){return fields.value.filter(f=>f.audience==='both'||f.audience===row.snapshot.person_type)}
function fileSummary(value:unknown):Attachment[]{return Array.isArray(value)?value.filter((item):item is Attachment=>typeof item==='object'&&item!==null&&typeof item.id==='string'&&typeof item.filename==='string'&&typeof item.size==='number'&&typeof item.visibility==='string'):[]}
onMounted(()=>{void load();void request<Field[]>('/profile-fields').then(value=>fields.value=value).catch(notifyError)})
</script>
<template>
  <div class="pagehead">
    <h1 data-guide-target="history">申请记录</h1>
    <p class="sub">查看资料提交、审核结果与处理说明。</p>
  </div>
  <section class="card">
    <div class="actions notification-batch">
      <label class="privacy">
        <input type="checkbox" :checked="allSelected" :indeterminate="selected.length > 0 && !allSelected" :disabled="loading || failed || !selectable.length" @change="togglePage"> 选择本页</label>
      <span class="sub">已选 {{ selected.length }} 条</span>
      <button type="button" class="button small danger" :disabled="loading || failed || !selected.length" @click="remove()">删除所选</button>
      <button type="button" class="button small danger" :disabled="loading || failed || (!total && !search)" @click="remove(true)">删除全部记录</button>
    </div>
    <p v-if="loading" role="status">正在加载申请记录…</p>
    <div v-else-if="failed" class="empty">申请记录加载失败 <button class="button small" @click="load(offset)">重试</button>
    </div>
    <div v-else-if="rows.length" class="tablewrap">
      <table>
        <thead>
          <tr>
            <th aria-label="选择已审核申请">
            </th>
            <th>上次修改于</th>
            <th>提交时间</th>
            <th>审核状态</th>
            <th>审核人员 / 原因</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>
              <input v-if="['approved','rejected'].includes(row.status)" v-model="selected" type="checkbox" :value="row.id" :aria-label="'选择：'+date(row.submitted_at)">
            </td>
            <td>{{ date(row.last_modified_at) }}</td>
            <td>{{ date(row.submitted_at) }}</td>
            <td>
              <span class="tag" :class="row.status">{{ statusLabel(row.status) }}</span>
            </td>
            <td>{{ row.reviewer_name || '待处理' }}<small>{{ row.reason || '—' }}</small>
            </td>
            <td>
              <div class="admin-row-actions">
                <button type="button" class="button small" @click="detail=row">查看资料</button>
                <template v-if="['approved','rejected'].includes(row.status)">
                  <button type="button" class="button small danger" @click="remove(false,[row.id])">删除记录</button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty">
      <span class="symbol">◇</span>{{ search ? '没有匹配的申请记录' : '还没有申请记录' }}</div>
    <RecordPagination :offset="offset" :total="total" :search="search" label="申请记录分页" @update:offset="load($event)" @update:search="search=$event;load()"/>
  </section>
  <AppDialog :model-value="!!detail" title="已提交资料" @close="detail=null">
    <dl v-if="detail" class="kv">
      <template v-for="field in visibleFields(detail)" :key="field.key">
        <dt>{{ field.label }} <small>{{ field.fixed_public?'公开（固定）':(typeof detail.snapshot.visibility==='object' && detail.snapshot.visibility!==null && field.key in detail.snapshot.visibility && detail.snapshot.visibility[field.key as keyof typeof detail.snapshot.visibility]==='private')?'私密':'公开' }}</small>
        </dt>
        <dd style="white-space:pre-wrap">
          <DocumentFields :value="detail.snapshot[field.key]"/>
        </dd>
      </template>
    </dl>
    <details>
      <summary>ACS 能力描述</summary>
      <DocumentFields :value="detail?.acs_document"/>
    </details>
    <section v-if="detail">
      <h3>资料文件</h3>
      <p>共 {{ fileSummary(detail.attachments).length }} 个文件</p>
      <ul>
        <li v-for="file in fileSummary(detail.attachments)" :key="file.id">{{ file.filename }} · {{ file.visibility==='public'?'公开':'私密' }} · {{ fileSizeMB(file.size) }}</li>
      </ul>
    </section>
  </AppDialog>
</template>
