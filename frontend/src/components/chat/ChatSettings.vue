<script setup lang="ts">
import AppIcon from '../AppIcon.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { request, notifyError } from '../../api/client'
interface Learning {enabled:boolean;can_enable?:boolean;learned_at?:string;last?:{status:string;error?:string}|null}
const props=defineProps<{conversationId:string;busy:boolean;revision:number}>()
const learning=ref<Learning>({enabled:false}), saving=ref(false)
const endpoint=computed(()=>'/conversations/'+encodeURIComponent(props.conversationId))
let controller=new AbortController()
async function load() {
  controller.abort()
  controller=new AbortController()
  const signal=controller.signal, path=endpoint.value
  try {
    const status=await request<Learning>(path+'/style-learning','GET',undefined,signal)
    if(!signal.aborted) learning.value=status
  } catch(error) { if(!signal.aborted) notifyError(error) }
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
  learning.value={enabled:false}; saving.value=false
  if(props.conversationId) void load()
},{immediate:true})
watch(()=>props.revision,()=>{if(props.conversationId && !saving.value)void load()})
onUnmounted(()=>controller.abort())
</script>

<template>
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
