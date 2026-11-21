<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request, notifyError } from '../api/client'
import { date, statusLabel } from '../utils/format'
import RecordPagination from '../components/RecordPagination.vue'
import MessageRecent from '../components/MessageRecent.vue'
interface Submission { id: string; status: string; submitted_at: string; last_modified_at: string; snapshot: { name?: string; avatar_name?: string } }
interface Overview { counts: { users?: number; agents?: number; pending?: number; failed?: number } }
const router = useRouter(), data = ref<Overview | null>(null)
const pending=ref<Submission[]>([]), total=ref(0), offset=ref(0), search=ref('')
let sequence=0, overviewSequence=0
async function loadPending() {
  const ticket=++sequence
  try {
    const query=new URLSearchParams({scope:'admin',status:'pending',search:search.value,offset:String(offset.value)})
    const result=await request<{items:Submission[];total:number;offset:number}>(`/records/submissions?${query}`)
    if(ticket===sequence){pending.value=result.items;total.value=result.total;offset.value=result.offset}
  } catch(e){if(ticket===sequence)notifyError(e)}
}
async function load() {
  const ticket=++overviewSequence
  void loadPending()
  try {
    const summary=await request<Overview>('/admin/overview')
    if(ticket===overviewSequence)data.value=summary
  } catch(e){if(ticket===overviewSequence)notifyError(e)}
}
onMounted(load)
onBeforeUnmount(()=>{sequence++;overviewSequence++})
</script>
<template>
  <div class="heading"><div><h1>分身管理概览</h1><p class="sub">管理注册、资料审核与分身运行状态。</p></div><div class="actions"><button type="button" class="button" @click="load">↻ 刷新数据</button></div></div>
  <section class="hero"><div><span class="eyebrow">BUILD THE OTHER YOU</span><h2>待办与运行状态</h2><p>审核申请，跟踪创建任务，维护分身身份与证书。</p><button type="button" class="button primary" @click="router.push('/admin/history')">处理待审核申请 ↗</button></div><div class="hero-art"><div class="orb"></div><span class="hero-number">IDENTITY / KNOWLEDGE / CONNECTION</span></div></section>
  <section v-if="data" class="stats">
    <div v-for="([label, value, hint]) in ([['注册用户', data.counts.users || 0, '已建立个人账户'], ['活跃分身', data.counts.agents || 0, '独立身份 · 自动生成'], ['待审核申请', data.counts.pending || 0, '等待你的确认'], ['失败任务', data.counts.failed || 0, '可在任务中心重试']] as [string, number, string][])" :key="label" class="stat"><div class="label">{{ label }}<span>↗</span></div><strong>{{ String(value).padStart(2, '0') }}</strong><small>{{ hint }}</small></div>
  </section>
  <div class="grid2">
    <section class="card">
      <div class="cardhead"><div><h2>待处理申请</h2><p>审核通过后将自动创建或同步分身</p></div><button type="button" class="button small" @click="router.push('/admin/history')">查看全部 →</button></div>
      <div v-if="pending.length" class="tablewrap"><table><thead><tr><th>申请人 / 分身</th><th>提交时间</th><th>状态</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in pending" :key="item.id"><td>{{ item.snapshot.name }}<small>{{ item.snapshot.avatar_name }} · 上次修改于 {{ date(item.last_modified_at) }}</small></td><td>{{ date(item.submitted_at) }}</td><td><span class="tag" :class="item.status">{{ statusLabel(item.status) }}</span></td><td><button type="button" class="button small" @click="router.push('/admin/history')">前往审核</button></td></tr>
      </tbody></table></div>
      <div v-else class="empty"><span class="symbol">◇</span>暂无符合条件的申请</div>
      <RecordPagination :offset="offset" :total="total" :search="search" label="待处理申请分页" @update:offset="offset=$event;loadPending()" @update:search="search=$event;offset=0;loadPending()"/>
    </section>
    <MessageRecent to="/admin/messages"/>
  </div>
</template>
