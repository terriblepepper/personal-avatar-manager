<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { request, notifyError } from '../../api/client'
import { usePolling } from '../../composables/usePolling'
import { useSessionStore } from '../../stores/session'
import { date } from '../../utils/format'
interface ResourceState {policy:Record<string,number|boolean>;enforcement:{mode:string;error?:string};effective_cpu_cores:number;effective_memory_bytes:number;active_jobs:number;waiting:number;pressure:boolean}
interface Environment {registration_adapter?:string;registration_mode?:string;communication?:{name:string;value:string|null}[];framework_versions?:Record<string,string|null>;manual_path?:string}
interface PlatformCert {certificate:{id:string;aic:string;generation:number;issuer:string;expires_at:string}|null;renewal:{state?:string;updated_at?:string}}
const cert=ref<PlatformCert|null>(null), certError=ref(''), certBusy=ref(false)
const certStates:Record<string,string>={queued:'已排队',running:'正在续签',done:'新证书已核验',changed:'证书已更换',unconfirmed:'续签结果待核对，请先刷新证书详情'}
interface Registration {enabled:boolean;phase:string;registration_id?:string;aic?:string;aip?:{endpoint?:string;serving?:boolean};updated_at?:string;message?:string;reason?:string;personal_registration_mode?:string}
const session=useSessionStore(), registration=ref<Registration|null>(null), registrationError=ref(''), checking=ref(false)
const resources=ref<ResourceState|null>(null),policy=ref<Record<string,number|boolean>>({}),settings=ref<Record<string,string>>({}),environment=ref<Environment>({}),mode=ref(''),busy=ref(false)
const phases:Record<string,string>={initial:'尚未提交',pending_review:'等待 Registry 人工审核',approved:'审核通过，正在签发证书',active:'身份与证书就绪',blocked:'流程受阻',rejected:'申请被拒绝'}
const enforcement:Record<string,string>={soft:'软预算（未设置系统硬限制）',systemd:'systemd 硬限制已生效',unavailable:'系统硬限制未生效',unconfirmed:'系统硬限制结果未确认'}
const fields:[string,string,number,number,number][]=[
  ['max_cpu_cores','CPU 上限（逻辑核预算）',.1,1024,.1],['max_memory_gib','平台内存预算（GiB）',1,4096,.5],
  ['cache_gib','缓存池上限（GiB）',.01,2048,.01],['reserve_memory_gib','主机保留内存（GiB）',0,4096,.5],
  ['host_cpu_target','主机 CPU 目标（%）',10,95,1],['max_jobs','全局活跃工作上限',2,512,1],
  ['query_reserved','查询保留额度',1,64,1],['soul_concurrency','并行 Soul 构建上限',1,64,1],['max_index_per_user','每账户索引上限',1,10,1],
  ['query_concurrency','查询阶段上限',1,128,1],['embedding_concurrency','嵌入阶段上限',1,128,1],['vision_concurrency','图片解析阶段上限',1,64,1],
  ['rerank_concurrency','重排序阶段上限',1,64,1],['parse_concurrency','文件解析阶段上限',1,128,1],['queue_limit','资源等待队列上限',1,100000,1],
  ['query_wait_seconds','查询等待上限（秒）',.1,120,.1],['sample_seconds','资源采样间隔（秒）',.25,30,.25],
]
const urls:Record<string,string>={registry_url:'Registry 注册中心 URL',certificate_authority_url:'CA 证书颁发机构 URL',registry_entity_mtls_url:'实体注册 mTLS URL（可选）',public_url:'系统对外访问 URL'}
let disposed=false,resourceSequence=0
async function reg(manual=false, signal?: AbortSignal){
  if(!session.user?.is_root||checking.value)return
  checking.value=true
  try {
    const value=await request<Registration>('/admin/system/registration'+(manual?'/refresh':''),manual?'POST':'GET', undefined, signal)
    if(!disposed && !signal?.aborted){registration.value=value;registrationError.value=''}
  }catch(e){if(!disposed && !signal?.aborted)registrationError.value=e instanceof Error?e.message:'注册状态读取失败'}finally{checking.value=false}
}
async function certificate(renew=false, signal?: AbortSignal){
  if(certBusy.value)return
  certBusy.value=true
  try{
    if(renew && cert.value?.certificate){
      await request('/admin/system/certificate/'+encodeURIComponent(cert.value.certificate.id)+'/renew','POST')
    }
    const value=await request<PlatformCert>('/admin/system/certificate','GET',undefined,signal)
    if(!disposed && !signal?.aborted){cert.value=value;certError.value=''}
  }catch(e){if(!disposed && !signal?.aborted)certError.value=e instanceof Error?e.message:'平台证书操作失败'}
  finally{if(!disposed)certBusy.value=false}
}
async function loadResources(initial=false){
  const ticket=++resourceSequence
  try{
    const value=await request<ResourceState>('/admin/rag-resources')
    if(disposed||ticket!==resourceSequence)return
    resources.value=value
    if(initial)policy.value={...value.policy}
  }catch(e){if(!disposed&&ticket===resourceSequence)notifyError(e)}
}
async function loadSettings(){
  try{
    const data=await request<{settings:Record<string,string>;environment?:Environment;mode?:string}>('/admin/settings')
    if(disposed)return
    settings.value=Object.fromEntries(Object.keys(urls).map(key=>[key,data.settings[key]||'']))
    environment.value=data.environment||{};mode.value=environment.value.registration_mode||data.mode||'未配置'
  }catch(e){if(!disposed)notifyError(e)}
}
async function save(resource:boolean){
  if(busy.value)return
  busy.value=true
  try{
    if(resource){
      resourceSequence++
      const saved=await request<ResourceState>('/admin/rag-resources','PUT',policy.value)
      if(!disposed){resources.value=saved;policy.value={...saved.policy}}
    }else await request('/admin/settings','PUT',settings.value)
    if(!disposed)ElMessage.success('配置已保存')
  }catch(e){if(!disposed)notifyError(e)}finally{busy.value=false}
}
onMounted(()=>{
  void loadSettings()
  void loadResources(true)
})
usePolling(async signal => {
  await Promise.all([
    reg(false, signal),
    !cert.value || ['queued','running'].includes(cert.value.renewal?.state || '')
      ? certificate(false, signal) : undefined,
  ])
}, () => 5000, { enabled: () => !!session.user?.is_root || !cert.value || ['queued','running'].includes(cert.value.renewal?.state || '') })
onBeforeUnmount(() => { disposed = true })
</script>
<template>
  <section v-if="session.user?.is_root" class="card"><div class="cardhead"><h2>管理系统与个人实体注册</h2><button class="button small" :disabled="checking" @click="reg(true)">立即检查</button></div>
    <p class="sub">注册审核每小时检查一次，正式证书每 30 分钟检查一次。管理系统提供协议端点，个人分身独立维护身份与证书。</p>
    <p v-if="registrationError" class="notice error" role="alert">{{ registrationError }}</p>
    <div v-if="registration" role="status">
      <section>
        <h3>管理系统服务</h3>
        <dl class="kv"><dt>自动注册</dt><dd>{{ registration.enabled?'已启用':'未启用' }}</dd><dt>注册阶段</dt><dd>{{ phases[registration.phase] || registration.phase }}</dd>
          <dt>注册记录</dt><dd>{{ registration.registration_id || '尚未取得' }}</dd><dt>系统 AIC</dt><dd>{{ registration.aic || '由 Registry 分配' }}</dd>
          <dt>管理平台端点</dt><dd>{{ registration.aip?.endpoint || '尚未配置' }}</dd><dt>AIP 接入</dt><dd>{{ registration.aip?.serving?'正在监听':'尚未接入' }}</dd>
          <dt>最近检查</dt><dd>{{ registration.updated_at?date(registration.updated_at):'尚未运行' }}</dd></dl>
        <p v-if="registration.message" class="sub">{{ registration.message }}</p><p v-if="registration.reason" class="notice">审核原因：{{ registration.reason }}</p>
      </section>
      <p class="sub">{{ registration.personal_registration_mode==='acps-direct'?'个人实体由本人填写技能，平台审核后提交 Registry，审核通过后签发证书。':'当前使用 Demo 注册器。' }}</p>
      <a class="button small" href="/api/system/acs" target="_blank" rel="noopener">管理系统 ACS</a>
    </div>
  </section>
  <section class="card"><div class="cardhead"><h2>检索与索引资源</h2><button class="button small" :disabled="busy" @click="loadResources(!resources)">刷新状态</button></div>
    <p class="sub">平台根据主机压力调整任务与缓存。系统硬限制影响整个平台及子进程。</p>
    <template v-if="resources"><p role="status">有效 CPU {{ resources.effective_cpu_cores }} 核 · 内存预算 {{ (resources.effective_memory_bytes/1024**3).toFixed(1) }} GiB · 活跃 {{ resources.active_jobs }} · 等待 {{ resources.waiting }} · {{ resources.pressure?'检测到资源压力':'按预算运行' }} · {{ enforcement[resources.enforcement.mode] || '限制状态未知' }} {{ resources.enforcement.error }}</p>
      <form @submit.prevent="save(true)"><div class="formgrid">
        <label v-for="[key,label,min,max,step] in fields" :key="key" class="field">{{ label }}<input placeholder=" " class="form-control" v-model.number="policy[key]" type="number" :min="min" :max="max" :step="step" required></label>
      </div><label class="privacy"><input v-model="policy.enforce_systemd" type="checkbox">同时应用 systemd 硬限制</label>
        <button class="button primary" :disabled="busy">保存资源配置</button>
      </form>
    </template>
  </section>
  <section class="card"><div class="cardhead"><h2>注册与证书服务</h2><span class="tag">{{ mode }}</span></div>
    <p class="notice">此处 URL 用于业务设置。正式 ATR 身份配置由服务器管理。</p>
    <form @submit.prevent="save(false)"><div class="formgrid"><label v-for="(label,key) in urls" :key="key" class="field">{{ label }}<input placeholder=" " class="form-control" v-model="settings[key]" type="url" maxlength="500"></label></div><button class="button primary" :disabled="busy || !mode">保存配置</button></form>
  </section>
  <section class="card"><h2>环境信息</h2><dl class="kv">
    <dt>注册适配器</dt><dd>{{ environment.registration_adapter || '未提供' }}（{{ mode || '未配置' }}）</dd>
    <dt>通信模式</dt><dd><div v-for="item in environment.communication" :key="item.name">{{ item.name }}：{{ item.value || '未启用' }}</div><span v-if="!environment.communication?.length">未提供</span></dd>
    <dt>身份框架</dt><dd>{{ Object.entries(environment.framework_versions || {}).map(([name,v])=>name+' '+(v || '版本信息不可用')).join(' / ') || '未提供' }}</dd>
    <dt>操作手册</dt><dd>{{ environment.manual_path || '当前部署未提供' }}</dd>
  </dl>
    <div class="cardhead"><h3>平台证书</h3><div>
      <button class="button small" :disabled="certBusy" @click="certificate()">刷新状态</button>
      <button class="button small" :disabled="certBusy || !cert?.certificate || ['queued','running'].includes(cert?.renewal?.state||'')" @click="certificate(true)">立即续签</button>
    </div></div>
    <p v-if="certError" class="notice error" role="alert">{{ certError }}</p>
    <dl v-if="cert?.certificate" class="kv">
      <dt>系统 AIC</dt><dd>{{ cert.certificate.aic }}</dd><dt>签发者</dt><dd>{{ cert.certificate.issuer }}</dd>
      <dt>客户端指纹</dt><dd>{{ cert.certificate.id }}</dd><dt>证书代次</dt><dd>{{ cert.certificate.generation }}</dd>
      <dt>最早到期时间</dt><dd>{{ date(cert.certificate.expires_at) }}</dd>
    </dl><p v-else-if="cert" class="sub">暂无可核验的平台证书。</p>
    <p v-if="cert?.renewal?.state" class="sub" role="status">{{ certStates[cert.renewal.state] }} · {{ date(cert.renewal.updated_at || '') }}</p>
  </section>
</template>
