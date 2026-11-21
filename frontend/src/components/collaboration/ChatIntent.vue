<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElButton, ElRadio, ElRadioGroup } from 'element-plus'
import { request, notifyError } from '../../api/client'
import IntentDialog from './IntentDialog.vue'
interface Intent {id:string;conversation_id:string;target_query:string;target_aic:string;objective:string;shared_content:string;delivery_mode?:string;file_ids?:string[]}
const props=defineProps<{intent:Record<string,unknown>}>(), emit=defineEmits<{updated:[];opened:[sessionId:string,taskId:string]}>()
const mode=ref<'message'|'negotiation'>('message'), editing=ref<Intent|null>(null),busy=ref(false)
const status=computed(()=>typeof props.intent.status==='string'?props.intent.status:'')
const identifier=computed(()=>typeof props.intent.id==='string'?props.intent.id:'')
function parse(value:unknown):Intent {
  if(!value||typeof value!=='object')throw Error('通信草稿无效')
  const data=value as Record<string,unknown>
  const field=(name:string)=>typeof data[name]==='string'?data[name] as string:''
  const result={id:field('id'),conversation_id:field('conversation_id'),target_query:field('target_query'),target_aic:field('target_aic'),objective:field('objective'),shared_content:field('shared_content'),delivery_mode:field('delivery_mode'),file_ids:Array.isArray(data.file_ids)?data.file_ids.filter((id:unknown):id is string=>typeof id==='string'):[]}
  if(!result.id||!result.conversation_id)throw Error('通信草稿缺少标识')
  return result
}
async function choose(){if(!identifier.value||busy.value)return;busy.value=true;try{editing.value=parse(await request(`/agent-intents/${encodeURIComponent(identifier.value)}/choose`,'POST',{delivery_mode:mode.value}));emit('updated')}catch(e){notifyError(e)}finally{busy.value=false}}
async function edit(){if(!identifier.value||busy.value)return;busy.value=true;try{editing.value=parse(await request(`/agent-intents/${encodeURIComponent(identifier.value)}`))}catch(e){notifyError(e)}finally{busy.value=false}}
async function cancel(){if(!identifier.value||busy.value)return;busy.value=true;try{await request(`/agent-intents/${encodeURIComponent(identifier.value)}/cancel`,'POST',{});emit('updated')}catch(e){notifyError(e)}finally{busy.value=false}}
async function open(){const id=typeof props.intent.task_id==='string'?props.intent.task_id:'';if(!id)return;try{const result=await request<{session_id:string}>(`/agent-dialogues/${encodeURIComponent(id)}`);emit('opened',result.session_id,id)}catch(e){notifyError(e)}}
</script>
<template><section class="avatar-intent-choice"><header class="intent-choice-heading">分身通信</header><template v-if="status==='pending'"><p>选择发送消息或任务，确认发送前不会联系对方。</p><ElRadioGroup v-model="mode"><ElRadio value="message">发送消息</ElRadio><ElRadio value="negotiation">任务</ElRadio></ElRadioGroup><div class="actions"><ElButton :loading="busy" @click="choose">确认并填写</ElButton><ElButton :disabled="busy" @click="cancel">取消</ElButton></div></template><template v-else-if="status==='editing'"><p>已选择{{ intent.delivery_mode==='message'?'发送消息':'任务' }}，尚未发送。</p><ElButton :loading="busy" @click="edit">继续填写</ElButton><ElButton :disabled="busy" @click="cancel">取消</ElButton></template><p v-else-if="status==='cancelled'">已取消，本次未发送。</p><template v-else-if="status==='confirmed'"><p>请求已提交，请查看实际送达状态。</p><ElButton @click="open">查看分身会话</ElButton></template><IntentDialog v-if="editing" :intent="editing" @closed="editing=null;emit('updated')" @sent="(session,task)=>{editing=null;emit('updated');emit('opened',session,task)}"/></section></template>
