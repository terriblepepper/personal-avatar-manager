<script setup lang="ts">
import RecordPagination from '../RecordPagination.vue'
import { onMounted, ref } from 'vue'
import { ElButton, ElCheckbox, ElInput, ElMessage } from 'element-plus'
import { request, notifyError } from '../../api/client'
interface User {id:string;username:string;name:string;phone:string;is_root:boolean;migration_status:string;identity?:{subject:string};previous_identity?:{subject:string};aic?:string}
interface Summary {account?:{username:string};aic?:string;submissions?:number;files?:number;messages?:number;conversations?:number;skills?:number;chat_files?:number;dialogues?:number;model_settings?:number}
interface Preview {preview_token:string;issuer:string;subject:string;target:Summary;empty_account?:Summary;oidc_enabled:boolean}
const statuses:Record<string,string>={local_root:'根管理员 · 保持本地登录',unlinked:'待迁移',pending:'已关联 · 待本人登录',verified:'已验证统一身份'}
const users=ref<User[]>([]), selected=ref<User|null>(null), preview=ref<Preview|null>(null), subject=ref(''), allow=ref(false), verified=ref(false), reason=ref(''), search=ref(''), offset=ref(0), total=ref(0), issuer=ref(''), enabled=ref(false), busy=ref(false)
let sequence=0
async function load(){
  const ticket=++sequence
  try{
    const value=await request<{items:User[];total:number;offset:number;issuer:string;oidc_enabled:boolean}>(`/admin/identity-migration?${new URLSearchParams({search:search.value,offset:String(offset.value)})}`)
    if(ticket!==sequence)return
    users.value=value.items;total.value=value.total;offset.value=value.offset;issuer.value=value.issuer;enabled.value=value.oidc_enabled
  }catch(e){if(ticket===sequence)notifyError(e)}
}
function resetPreview(){preview.value=null;verified.value=false;reason.value=''}
function select(user:User){selected.value=user;subject.value=user.identity?.subject||'';allow.value=false;resetPreview()}
function page(next:number,query:string){
  selected.value=null;resetPreview();offset.value=next;search.value=query;void load()
}
async function inspect(){
  const user=selected.value
  if(!user||!subject.value.trim()||busy.value)return
  busy.value=true
  try{
    const result=await request<Preview>('/admin/identity-migration/preview','POST',{user_id:user.id,subject:subject.value.trim(),allow_empty_transfer:allow.value})
    if(selected.value?.id===user.id)preview.value=result
  }catch(e){notifyError(e)}finally{busy.value=false}
}
async function commit(){
  if(busy.value)return
  if(!preview.value||!verified.value||reason.value.trim().length<3){ElMessage.warning('请确认归属并填写至少 3 字的核验备注');return}
  busy.value=true
  try{
    const result=await request<{message:string}>('/admin/identity-migration/commit','POST',{preview_token:preview.value.preview_token,identity_verified:true,reason:reason.value.trim()})
    resetPreview();selected.value=null;await load();ElMessage.success(result.message)
  }catch(e){notifyError(e)}finally{busy.value=false}
}
onMounted(load)
</script>
<template>
  <section class="card">
    <h2>身份迁移</h2><p class="notice">关联前请核验 Keycloak 身份与原账号归属。原分身、资料、模型 Key、文件、审核及聊天历史保留。根管理员继续使用本地登录。</p>
    <p>统一认证：{{ enabled?'已启用，迁移后须统一登录':'准备阶段，尚未启用' }} · 身份源：{{ issuer }}</p>
    <p class="sub">手机号仅供核验参考。</p>
    <div class="tablewrap compact-record-table"><table><thead><tr><th>姓名 / 旧账号</th><th>原登记手机</th><th>迁移状态 / User ID</th><th>操作</th></tr></thead>
      <tbody><tr v-for="user in users" :key="user.id">
        <td>{{ user.name || '未填姓名' }}<small>{{ user.username }}</small></td><td>{{ user.phone || '未登记' }}</td>
        <td>{{ statuses[user.migration_status] || user.migration_status }}<small v-if="user.identity || user.previous_identity">{{ user.identity?.subject || '旧 ID：'+user.previous_identity?.subject }}</small></td>
        <td><ElButton :disabled="user.is_root || busy" @click="select(user)">{{ user.identity?'查看关联':user.previous_identity?'绑定小镇 ID':'配置迁移' }}</ElButton></td>
      </tr></tbody></table></div>
    <RecordPagination :offset="offset" :total="total" :search="search" label="迁移账户分页" @update:offset="page($event,search)" @update:search="page(0,$event)"/>
    <section v-if="selected"><h3>迁移 {{ selected.username }}</h3><p>保留分身：{{ selected.aic || '尚无分身' }}</p>
      <div v-if="!preview">
        <label class="field">Keycloak User ID / 已登录用户名<ElInput placeholder=" " v-model="subject" maxlength="255" autocomplete="off" :disabled="busy"/></label>
        <p class="sub">已登录的用户名可解析为 User ID。首次登录前，请填写 Keycloak 用户详情中的 User ID。</p>
        <ElCheckbox v-model="allow" :disabled="busy">允许转移已确认的空占位账号</ElCheckbox>
        <ElButton :disabled="!subject.trim()" :loading="busy" @click="inspect">预览迁移</ElButton>
      </div>
      <div v-else>
        <h3>确认迁移预览</h3><p>目标身份源：{{ preview.issuer }}</p><p>保留账号：{{ preview.target.account?.username || selected.username }} · Keycloak User ID：{{ preview.subject }}</p>
        <p>保留分身：{{ preview.target.aic || '尚未创建' }} ·
          {{ preview.target.submissions || 0 }} 条申请 · {{ preview.target.files || 0 }} 个文件 ·
          {{ preview.target.messages || 0 }} 条消息 · {{ preview.target.conversations || 0 }} 个会话
          · {{ preview.target.skills || 0 }} 个技能 ·
          {{ preview.target.chat_files || 0 }} 个会话附件 ·
          {{ preview.target.dialogues || 0 }} 项任务 ·
          {{ preview.target.model_settings ? '已配置模型' : '未配置模型' }}</p>
        <p v-if="preview.empty_account" class="notice">将清理空占位账号 {{ preview.empty_account.account?.username }}，服务器提交时再次核验。</p>
        <p>{{ preview.oidc_enabled?'迁移后原登录会话失效，请重新统一登录。':'当前仅提前关联，启用统一认证前仍可用旧密码登录。' }}预览有效 5 分钟。</p>
        <ElCheckbox v-model="verified">我已在 Keycloak 核验该 User ID 属于原账号本人</ElCheckbox>
        <label class="field">核验备注<ElInput v-model="reason" type="textarea" maxlength="500" placeholder="填写核验依据，不填写密码或 Key"/></label>
        <div class="actions"><ElButton :disabled="busy" @click="resetPreview">返回修改</ElButton><ElButton type="primary" :loading="busy" @click="commit">确认迁移</ElButton></div>
      </div>
    </section>
  </section>
</template>
