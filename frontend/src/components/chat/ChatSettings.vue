<script setup lang="ts">
import AppIcon from '../AppIcon.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { request, notifyError } from '../../api/client'
interface Skill {id:string;name:string;description?:string;enabled:boolean|number;installed_enabled?:boolean|number}
interface Learning {enabled:boolean;can_enable?:boolean;learned_at?:string;last?:{status:string;error?:string}|null}
const props=defineProps<{conversationId:string;busy:boolean;revision:number}>()
const emit=defineEmits<{catalog:[skills:Skill[]]}>()
const installed=ref<Skill[]>([]), authorized=ref<Skill[]>([])
const learning=ref<Learning>({enabled:false}), saving=ref(false)
const enabledCount=computed(()=>authorized.value.filter(row=>row.enabled && row.installed_enabled!==false && row.installed_enabled!==0).length)
const endpoint=computed(()=>'/conversations/'+encodeURIComponent(props.conversationId))
let controller=new AbortController()
function publishCatalog() {
  emit('catalog',installed.value.map(item=>({...item,enabled:!!item.enabled && !authorized.value.some(row=>row.id===item.id && !row.enabled)})))
}
async function load() {
  controller.abort()
  controller=new AbortController()
  const signal=controller.signal, path=endpoint.value
  try {
    const [catalog,session,status]=await Promise.all([
      request<Skill[]>('/me/skills','GET',undefined,signal),
      request<Skill[]>(path+'/skills','GET',undefined,signal),
      request<Learning>(path+'/style-learning','GET',undefined,signal),
    ])
    if(signal.aborted) return
    installed.value=catalog; authorized.value=session; learning.value=status
    publishCatalog()
  } catch(error) { if(!signal.aborted) notifyError(error) }
}
async function skill(item:Skill) {
  if(saving.value || props.busy) return
  const signal=controller.signal
  saving.value=true
  try {
    const rows=await request<Skill[]>(endpoint.value+'/skills/'+encodeURIComponent(item.id),'PATCH',{enabled:!item.enabled},signal)
    if(!signal.aborted) { authorized.value=rows; publishCatalog() }
  } catch(error) { if(!signal.aborted) notifyError(error) }
  finally { if(!signal.aborted) saving.value=false }
}
async function learn(event?:Event) {
  if(saving.value || props.busy || !learning.value.can_enable) return
  saving.value=true
  const signal=controller.signal, path=endpoint.value+'/style-learning'
  try {
    if(event?.target instanceof HTMLInputElement) {
      const enabled=event.target.checked
      event.target.checked=learning.value.enabled
      learning.value=await request<Learning>(path,'PATCH',{enabled},signal)
    } else {
      await request(path+'/learn','POST',{},signal)
      learning.value=await request<Learning>(path,'GET',undefined,signal)
      ElMessage.success('已安排风格学习')
    }
  } catch(error) { if(!signal.aborted) notifyError(error) }
  finally { if(!signal.aborted) saving.value=false }
}
watch(()=>props.conversationId,()=>{
  installed.value=[]; authorized.value=[]; learning.value={enabled:false}; saving.value=false; publishCatalog()
  if(props.conversationId) void load()
},{immediate:true})
watch(()=>props.revision,()=>{if(props.conversationId && !saving.value)void load()})
onUnmounted(()=>controller.abort())
</script>

<template>
  <details class="composer-menu composer-tool">
    <summary title="管理本会话技能"><span aria-hidden="true">✧</span><span>技能</span><small>{{ enabledCount }}</small></summary>
    <div class="composer-popover">
      <h3>本会话技能</h3>
      <p class="sub">输入 $ 快捷选择。已加载的技能可用于后续回答，每轮最多三个。</p>
      <p v-if="!authorized.length" class="sub">尚无已加载技能。</p>
      <div v-for="item in authorized" :key="item.id" class="session-skill">
        <div><strong>{{ item.name }}</strong> <span class="tag">{{ !item.enabled?'已移出本会话':item.installed_enabled===false||item.installed_enabled===0?'全局已停用':'持续使用中' }}</span></div>
        <button type="button" class="button small" :disabled="busy||saving||!item.enabled && (item.installed_enabled===false||item.installed_enabled===0)" @click="skill(item)">{{ item.enabled?'移出本会话':'恢复使用' }}</button>
      </div>
    </div>
  </details>
  <Teleport to="#personal-chat-style" defer>
    <details class="chat-menu-section">
      <summary class="chat-menu-item"><AppIcon name="settings" :size="16"/>表达风格学习<AppIcon name="chevron" :size="14"/></summary>
      <div class="conversation-learning chat-menu-subpanel">
        <label><input type="checkbox" :checked="learning.enabled" :disabled="busy||saving||!learning.can_enable" @change="learn">自动学习本人表达</label>
        <button type="button" class="button small" :disabled="busy||saving||!learning.can_enable" @click="learn()">立即学习</button>
        <small v-if="learning.can_enable===false" class="sub">请先创建 Soul 并通过审核，待生效后再学习。</small>
        <small v-if="learning.last" class="sub">{{ learning.last.status }} {{ learning.last.error }}</small>
      </div>
    </details>
  </Teleport>
</template>
