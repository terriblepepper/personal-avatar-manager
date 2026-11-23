<script setup lang="ts">
import { SKILLS_PAUSED } from '../../skillPolicy'
import RecordPagination from '../RecordPagination.vue'
import { onMounted, ref } from 'vue'
import { ElButton, ElInput, ElOption, ElSelect } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import { request, notifyError } from '../../api/client'
import MarkdownContent from '../MarkdownContent.vue'
import ChatFilesManager from '../chat/ChatFilesManager.vue'
import ComposerToolbar from '../chat/ComposerToolbar.vue'
import ConversationTools from '../chat/ConversationTools.vue'
interface Intent {id:string;conversation_id:string;target_query:string;target_aic:string;objective:string;shared_content:string;delivery_mode?:string;file_ids?:string[];origin?:string}
interface Target {aic:string;name:string}
interface Skill {id:string;name:string;enabled:boolean}
const props=defineProps<{intent:Intent}>(),emit=defineEmits<{closed:[];sent:[sessionId:string,taskId:string]}>()
const directMessage = props.intent.delivery_mode === 'message'
const toolsSaving = ref(false)
const skills=ref<Skill[]>([]), sourceView=ref(true),resourceBusy=ref(false)
const search=ref(props.intent.target_query),target=ref(props.intent.target_aic),objective=ref(props.intent.objective+(props.intent.shared_content?'\n\n'+props.intent.shared_content:'')),items=ref<Target[]>([]),offset=ref(0),total=ref(0),files=ref<string[]>([...(props.intent.file_ids || [])]),busy=ref(false)
let seq=0
async function find(){const ticket=++seq;target.value='';try{const value=await request<{items:Target[];offset:number;total:number}>(`/agent-intents/${encodeURIComponent(props.intent.id)}/targets?${new URLSearchParams({search:search.value,offset:String(offset.value)})}`);if(ticket!==seq)return;items.value=value.items;offset.value=value.offset;total.value=value.total;if(value.items.some(item=>item.aic===props.intent.target_aic))target.value=props.intent.target_aic;else if(value.total===1)target.value=value.items[0]?.aic||''}catch(e){notifyError(e)}}
async function cancel(){if(busy.value||resourceBusy.value)return;busy.value=true;try{await request(`/agent-intents/${encodeURIComponent(props.intent.id)}/cancel`,'POST',{});emit('closed')}catch(e){notifyError(e)}finally{busy.value=false}}
async function send() {
  if (busy.value || resourceBusy.value || toolsSaving.value || !target.value || !objective.value.trim()) return
  busy.value = true
  try {
    const characters = Array.from(objective.value)
    const result = await request<{ session_id: string; id: string }>(
      `/agent-intents/${encodeURIComponent(props.intent.id)}/confirm`, 'POST', {
        target_aic: target.value,
        objective: characters.slice(0, 2000).join(''),
        share_content: characters.length > 2000,
        shared_content: characters.slice(2000).join(''),
        file_ids: files.value,
      },
    )
    emit('sent', result.session_id, result.id)
  } catch (error) {
    notifyError(error)
  } finally {
    busy.value = false
  }
}
onMounted(()=>{void find();if(!SKILLS_PAUSED&&!directMessage)void request<Skill[]>('/me/skills').then(value=>{skills.value=value}).catch(notifyError)})
</script>
<template><AppDialog :model-value="true" append-to-body class="agent-intent-dialog" :title="intent.delivery_mode==='message'?'发送分身消息':'发起分身任务'" width="min(46rem, calc(100vw - 2rem))" :close-on-click-modal="false" :before-close="()=>cancel()"><ElSelect v-model="target" placeholder="选择目标分身"><ElOption v-for="item in items" :key="item.aic" :value="item.aic" :label="item.name+' · '+item.aic"/></ElSelect><RecordPagination :offset="offset" :total="total" :search="search" label="目标分身分页" @update:offset="offset=$event;find()" @update:search="search=$event;offset=0;find()"/><label class="field" for="peer-intent-objective">任务或消息内容</label><div class="actions"><ElButton @click="sourceView=false">预览</ElButton><ElButton @click="sourceView=true">源码编辑</ElButton></div><ElInput v-if="sourceView" id="peer-intent-objective" v-model="objective" type="textarea" placeholder="填写目标、要求和期望结果…" maxlength="62000" :rows="6"/><MarkdownContent v-else :source="objective"/><ComposerToolbar class="intent-composer-toolbar">
  <details v-if="!SKILLS_PAUSED && !directMessage" class="composer-menu composer-tool">
    <summary title="选择本人的已安装技能"><span aria-hidden="true">✧</span><span>技能</span><small>{{ skills.length }}</small></summary>
    <div class="composer-popover">
      <h3>已安装技能</h3>
      <p v-if="!skills.length" class="sub">尚未安装技能。</p>
      <div v-for="skill in skills" :key="skill.id" class="session-skill">
        <span>{{ skill.name }} <small v-if="!skill.enabled" class="sub">已停用</small></span>
        <button type="button" class="button small" :disabled="!skill.enabled || busy" @click="objective+=' $'+JSON.stringify(skill.name)+' ';sourceView=true">加入任务</button>
      </div>
    </div>
  </details>
  <ChatFilesManager :conversation-id="intent.conversation_id" :selected-ids="intent.file_ids || []" temporary include-conversation-files :busy="busy" @selected="files=$event" @uploading="resourceBusy=$event"/>
  <ConversationTools v-if="!directMessage" :conversation-id="intent.conversation_id" @saving="toolsSaving=$event" :busy="busy || resourceBusy"/>
</ComposerToolbar><p class="sub">确认后原样发送正文，所选附件以副本交付。<template v-if="!directMessage">任务由接收方同意后开始执行。</template></p><ElButton :disabled="busy||resourceBusy" @click="cancel">取消</ElButton><ElButton type="primary" :loading="busy" :disabled="resourceBusy||toolsSaving||!target||!objective.trim()" @click="send">确认发送</ElButton></AppDialog></template>
