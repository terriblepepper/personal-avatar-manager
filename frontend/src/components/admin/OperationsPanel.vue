<script setup lang="ts">
import { useConfirmation } from '../../composables/useConfirmation'
import RecordPagination from '../RecordPagination.vue'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import JobRecords from './JobRecords.vue'
import ProfileSnapshot from './ProfileSnapshot.vue'
import { ElMessage } from 'element-plus'
import ProfileDialog from './ProfileDialog.vue'
import { date, fileSizeMB } from '../../utils/format'
import { request, notifyError } from '../../api/client'
interface Health {message:string;ok?:boolean;checked_at?:string}
interface Agent {
  user_id:string; username:string; aic:string; identity_status:string; last_modified_at?:string;
  profile:Record<string,unknown> & {avatar_name?:string};
  certificate?:{id?:string;mode?:string;expires_at?:string;status?:string;issuer?:string};
  protocol?:{endpoint?:string;health?:Health};
  staged_identity?:{aic?:string}; acs_document?:{version?:string;entityMeta?:{environment?:string}};
  identity_job?:Job|null;
  model_config?:Record<string,unknown>; attachments?:{visibility?:string;size?:number}[];
}
interface Job {id:string;status:string;action?:string;error?:string}
interface AgentPage {items:Agent[];total:number;offset:number;mode?:string;entity_registration_enabled:boolean;personal_registration_mode:string;entity_port_pool:{error?:string;host?:string;start?:number;end?:number;allocated?:number;registered?:number;online?:number}}
const confirmAction = useConfirmation()
const props=defineProps<{page:string}>(), data=ref<AgentPage|null>(null), agentDetail=ref<Agent|null>(null), busy=ref(false)
const processing=ref(''), feedback=ref<Record<string,string>>({}), checks=ref<Record<string,Health & {aic:string}>>({})
const search=ref(''), offset=ref(0), detailTitle=ref(''), detailError=ref('')
let listSequence=0, detailSequence=0, disposed=false
async function load(){
  if(disposed)return
  const ticket=++listSequence
  const query=new URLSearchParams({search:search.value,offset:String(offset.value)})
  try{
    const page=await request<AgentPage>(`/admin/agents/operations?${query}`)
    if(ticket!==listSequence)return
    data.value=page;offset.value=page.offset
    for(const agent of page.items){
      const current=checks.value[agent.user_id], saved=agent.protocol?.health
      if(!current)continue
      const newer=saved?.checked_at && current.checked_at && Date.parse(saved.checked_at)>=Date.parse(current.checked_at)
      if(agent.aic!==current.aic || newer || !current.checked_at && saved?.message===current.message)delete checks.value[agent.user_id]
    }
  }catch(e){if(ticket===listSequence)notifyError(e)}
}
async function showDetail(agent:Agent){
  const ticket=++detailSequence
  detailTitle.value=agent.profile.avatar_name || '分身详情'
  detailError.value='';agentDetail.value=null
  try{
    const detail=await request<Agent>(`/admin/agents/${encodeURIComponent(agent.user_id)}/detail`)
    if(ticket===detailSequence)agentDetail.value=detail
  }catch(error){
    if(ticket===detailSequence)detailError.value=error instanceof Error?error.message:'分身详情读取失败'
  }
}
function closeDetail(){
  detailSequence++
  detailTitle.value='';agentDetail.value=null
}
async function action(path:string,confirmation?:string,method='POST',body:unknown={},key=''): Promise<void> {
  if (busy.value) return
  if (confirmation) {
    return confirmAction(confirmation, '确认身份操作', () => {
      const agent = data.value?.items.find(row => row.user_id === key)
      return [busy.value, data.value?.entity_registration_enabled, agent?.aic, agent?.identity_status,
        agent?.certificate?.id, agent?.identity_job?.id, agent?.identity_job?.status]
    }, () => action(path, undefined, method, body, key))
  }
  busy.value=true; processing.value=key
  try {
    const result=await request<{message?:string}>(path,method,body)
    if(key) feedback.value={...feedback.value,[key]:result?.message || '请求已提交，请查看身份维护记录。'}
    else ElMessage.success(result?.message || '操作已完成')
    await load()
  } catch(error) {
    if(key) feedback.value={...feedback.value,[key]:error instanceof Error?error.message:'操作失败'}
    notifyError(error)
  } finally { busy.value=false; processing.value='' }
}
const identityActions:Record<string,string>={register:'正式注册',renew:'更新证书',unregister:'注销正式身份'}
const jobStatuses:Record<string,string>={queued:'排队中',running:'执行中',waiting:'等待恢复',failed:'未完成',done:'已完成'}
const isDemo=(agent:Agent)=>agent.aic.startsWith('DEMO-')
const retired=(agent:Agent)=>['unregistered','retired'].includes(agent.identity_status)
function deployed(agent:Agent){return !isDemo(agent)&&agent.identity_status==='active'&&!!agent.certificate?.id}
function agentBusy(agent:Agent){return busy.value || ['queued','running','waiting'].includes(agent.identity_job?.status||'')}
function identity(agent:Agent,operation:string) {
  const confirmation=operation==='unregister'?'确认注销正式身份？协议端点将关闭，证书和 Registry 记录将被撤销，不能通过续期恢复。'
    :operation==='register'?'确认将 Demo 分身注册为正式派生身份？':'确认更新正式证书？'
  void action(`/admin/agents/${encodeURIComponent(agent.user_id)}/identity/${operation}`,confirmation,'POST',{},agent.user_id)
}
async function health(agent:Agent) {
  if(busy.value)return
  busy.value=true; processing.value=agent.user_id
  delete feedback.value[agent.user_id]
  try {
    const result=await request<Health>(`/admin/agents/${encodeURIComponent(agent.user_id)}/health`,'POST',{})
    checks.value[agent.user_id]={...result,aic:agent.aic}
    await load()
  } catch(error) {
    notifyError(error)
  } finally {busy.value=false;processing.value=''}
}
function latestHealth(agent:Agent){return checks.value[agent.user_id] || agent.protocol?.health}
const modes:Record<string,string>={'acps-direct':'ACPS 独立实体',demo:'演示身份'}
function statusLabel(agent:Agent) { return retired(agent)?'已注销':isDemo(agent)?'演示身份':deployed(agent)?'已部署':'未部署' }
function operationLabel(agent:Agent,key:string) {
  const job=agent.identity_job, text=key==='unregister'&&isDemo(agent)?'注销正式申请':identityActions[key]
  return job?.status==='failed'&&job.action===key?'重试'+text:text
}
onMounted(()=>{if(props.page==='agents')void load()})
onBeforeUnmount(()=>{disposed=true;listSequence++;detailSequence++})
</script>
<template><template v-if="page==='agents'">
  <p v-if="data" class="notice">{{ data.mode==='acps-direct'?'新分身按独立实体申请，经平台与 Registry 审核、证书核验后上线。':'当前使用本地演示注册器，Demo 身份和证书仅用于流程演示。' }}</p>
  <section v-if="data?.entity_registration_enabled && data.entity_port_pool" class="card info-card">
    <h2>个人分身固定端口</h2>
    <p v-if="data.entity_port_pool.error" role="alert">{{ data.entity_port_pool.error }}</p>
    <p v-else class="info-value">{{ data.entity_port_pool.host }} · 端口 {{ data.entity_port_pool.start }}–{{ data.entity_port_pool.end }} · 已分配 {{ data.entity_port_pool.allocated ?? 0 }} · 正式身份 {{ data.entity_port_pool.registered ?? 0 }} · 已监听 {{ data.entity_port_pool.online ?? 0 }}</p>
  </section>
  <section class="card">
    <div class="cardhead"><h2>分身管理</h2><button type="button" class="button small" :disabled="busy" @click="load">刷新</button></div>
    <div v-if="data?.items.length" class="tablewrap compact-record-table">
      <table class="admin-agents-table">
        <thead><tr><th>分身 / 所有人</th><th>AIC</th><th>证书到期 / ACS 版本</th><th>部署状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="agent in data.items" :key="agent.user_id">
            <td><strong>{{ agent.profile?.avatar_name || agent.username }}</strong><small>{{ agent.username }}<template v-if="agent.last_modified_at"> · {{ date(agent.last_modified_at) }}</template></small></td>
            <td class="mono">{{ agent.aic }}</td>
            <td>{{ date(agent.certificate?.expires_at) }}<small>ACS {{ agent.acs_document?.version || '未声明' }} · 运行环境 {{ agent.acs_document?.entityMeta?.environment || '未声明' }} · {{ modes[agent.certificate?.mode || ''] || '待确认' }}</small></td>
            <td>
              <span class="tag" :class="{ approved: deployed(agent) }">{{ statusLabel(agent) }}</span>
              <small
                v-if="latestHealth(agent)"
                :role="latestHealth(agent)?.ok === false ? 'alert' : 'status'"
              >{{ latestHealth(agent)?.message }}<template v-if="latestHealth(agent)?.checked_at"> · {{ date(latestHealth(agent)?.checked_at) }}</template>
              </small>
              <small v-else-if="deployed(agent)">尚未手动 Health 检查</small>
            </td>
            <td>
              <div class="admin-row-actions">
                <button type="button" class="button small" @click="showDetail(agent)">详情</button>
                <button v-if="data&&isDemo(agent)&&!retired(agent)&&!data.entity_registration_enabled" type="button" class="button small" :disabled="busy" @click="action(`/admin/agents/${encodeURIComponent(agent.user_id)}/renew`,undefined,'POST',{},agent.user_id)">更新演示证书</button>
                <template v-if="data?.entity_registration_enabled">
                  <button v-if="agent.identity_job?.status==='waiting'" type="button" class="button small" :disabled="busy" @click="action(`/admin/agents/identity-jobs/${encodeURIComponent(agent.identity_job!.id)}/refresh`,undefined,'POST',{},agent.user_id)">立即检查</button>
                  <button v-if="!isDemo(agent)&&agent.identity_status==='active'" type="button" class="button small" :disabled="agentBusy(agent)" @click="health(agent)">{{ processing===agent.user_id?'处理中…':'Health 检查' }}</button>
                  <button v-if="isDemo(agent)&&!retired(agent)&&data.personal_registration_mode!=='acps-direct'" type="button" class="button small" :disabled="agentBusy(agent)" @click="identity(agent,'register')">{{ operationLabel(agent,'register') }}</button>
                  <button v-if="!isDemo(agent)&&agent.identity_status==='active'" type="button" class="button small" :disabled="agentBusy(agent)" @click="identity(agent,'renew')">{{ operationLabel(agent,'renew') }}</button>
                  <button v-if="!retired(agent)&&(!isDemo(agent)||agent.staged_identity?.aic)" type="button" class="button small danger" :disabled="agentBusy(agent)" @click="identity(agent,'unregister')">{{ operationLabel(agent,'unregister') }}</button>
                </template>
              </div>
              <small v-if="isDemo(agent)&&agent.staged_identity?.aic" class="mono">已分配正式 AIC：{{ agent.staged_identity.aic }}</small>
              <small v-if="agent.protocol?.endpoint" class="mono">{{ agent.protocol.endpoint }}</small>
              <small v-if="agent.identity_job" class="sub">{{ identityActions[agent.identity_job!.action || ''] || '身份维护' }} · {{ jobStatuses[agent.identity_job!.status] || agent.identity_job!.status }}<template v-if="agent.identity_job?.error"> · {{ agent.identity_job!.error }}</template></small>
              <small v-if="feedback[agent.user_id]" class="sub" role="status">{{ feedback[agent.user_id] }}</small>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="sub">暂无符合条件的分身。</p>
    <RecordPagination :offset="offset" :total="data?.total || 0" :search="search" label="分身分页" @update:offset="offset=$event;load()" @update:search="search=$event;offset=0;load()"/>
  </section>
  <ProfileDialog body-label="分身详情正文" :model-value="!!detailTitle" :title="detailTitle" @close="closeDetail">
    <p v-if="detailError" class="notice error" role="alert">{{ detailError }}</p>
    <p v-else-if="!agentDetail" class="sub" role="status">正在读取分身详情…</p>
    <div v-else class="admin-agent-detail">
      <h3>部署信息</h3>
      <dl class="account-profile-fields">
        <dt>账户</dt><dd>{{ agentDetail.username }}</dd>
        <dt>AIC</dt><dd class="mono">{{ agentDetail.aic }}</dd>
        <dt>部署状态</dt><dd>{{ statusLabel(agentDetail) }}</dd>
        <dt>ACS 版本</dt><dd>{{ agentDetail.acs_document?.version || '—' }}</dd>
        <dt>运行环境</dt><dd>{{ agentDetail.acs_document?.entityMeta?.environment || '—' }}</dd>
        <dt>通信端点</dt><dd>{{ agentDetail.protocol?.endpoint || '未配置' }}</dd>
        <dt>证书到期</dt><dd>{{ date(agentDetail.certificate?.expires_at) }}</dd>
        <dt>资料文件</dt><dd>共 {{ agentDetail.attachments?.length || 0 }} 个文件 · 公开 {{ (agentDetail.attachments || []).filter(file=>file.visibility==='public').length }} 个 · 总大小 {{ fileSizeMB((agentDetail.attachments || []).reduce((sum,file)=>sum+(file.size || 0),0)) }}</dd>
      </dl>
      <ProfileSnapshot fields-class="account-profile-fields" :profile="agentDetail.profile" :model="agentDetail.model_config" :acs="agentDetail.acs_document"/>
    </div>
  </ProfileDialog>
</template>
<template v-if="page==='jobs'"><JobRecords collection="identity_jobs"/><JobRecords collection="jobs"/></template>
</template>
