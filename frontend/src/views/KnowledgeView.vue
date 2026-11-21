<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElButton, ElCheckbox, ElInput, ElOption, ElSelect } from 'element-plus'
import AppDialog from '../components/AppDialog.vue'
import { ApiError, request, notifyError } from '../api/client'
import AppIcon from '../components/AppIcon.vue'
import RecordPagination from '../components/RecordPagination.vue'
import { date } from '../utils/format'
import MarkdownContent from '../components/MarkdownContent.vue'
import AssetLibrary from '../components/knowledge/AssetLibrary.vue'
import { supportsSoulAsset } from '../utils/soulFormats'
import QuestionnaireDialog from '../components/knowledge/QuestionnaireDialog.vue'
import MemoryEditor from '../components/knowledge/MemoryEditor.vue'
import { usePolling } from '../composables/usePolling'
import type { Build, Knowledge } from '../components/knowledge/types'
const data = ref<Knowledge | null>(null), questionnaire = ref<Build | null>(null), editing = ref<Build | null>(null)
const prompt = ref(''), rounds = ref(3), selected = ref<string[]>([]), chats = ref<string[]>([]), rebuilding = ref(false), busy = ref(false)
const conversations = ref<{id:string;title:string;parent_id?:string;message_count:number}[]>([])
const chatSearch=ref(''),chatOffset=ref(0),buildSearch=ref(''),buildOffset=ref(0)
const filteredChats=computed(()=>conversations.value.filter(item=>item.title.toLowerCase().includes(chatSearch.value.toLowerCase())))
const buildStatuses: Record<string,string> = {
  queued:'排队中',running:'构建中',waiting_user:'等待作答',ready:'草案待确认',
  pending:'待审核',approved:'审核通过',rejected:'已退回',failed:'构建失败',
  canceled:'已取消',cancelled:'已取消',
}
function buildStatus(value:string) { return buildStatuses[value] || value }
const filteredBuilds=computed(()=>(data.value?.builds||[]).filter(item=>item.origin!=='learning'
  && `${buildStatus(item.status)} ${item.stage} ${item.prompt}`.toLowerCase().includes(buildSearch.value.toLowerCase())))
watch(()=>filteredBuilds.value.length,total=>{buildOffset.value=Math.min(buildOffset.value,Math.max(0,Math.ceil(total/10)-1)*10)})
const responses = ref<Record<string,{answer:string;revision:string}>>({}), draft = ref<Record<string,string>>({})
const labels:Record<string,string> = {voice:'语气与声音表达',personality:'互动风格',expression:'措辞与表达习惯',appearance:'形象呈现偏好',boundaries:'表达边界',examples:'表达示例',uncertainties:'待确认事项'}
const active = computed(() => data.value?.builds.find(item => item.id === data.value?.active_build_id))
const editable = computed(() => data.value?.builds.find(item => ['ready','pending'].includes(item.status)) || active.value)
let knownAssets = new Set<string>(), conversationsLoaded = false, disposed = false
const autoShown = new Set<string>()
const isRunning = (status?: string) => status === 'queued' || status === 'running'
function pollDelay() {
  const value = data.value
  return value?.builds.some(build => isRunning(build.status)) || isRunning(value?.learning?.status) || value?.folders?.pending ? 3000 : 30000
}
async function readWorkspace(signal:AbortSignal) {
  try {
    const result = await request<Knowledge>('/me/knowledge', 'GET', undefined, signal)
    if (signal.aborted) return
    data.value = result
    const ids = result.assets.map(asset => asset.id)
    selected.value = [...selected.value.filter(id => ids.includes(id)), ...ids.filter(id => !knownAssets.has(id))]
    knownAssets = new Set(ids)
    const waiting = result.builds.find(item => item.status === 'waiting_user' && item.question_plan?.length && item.phase !== 'followup' && !autoShown.has(item.id))
    if (waiting && !questionnaire.value && !editing.value) {
      autoShown.add(waiting.id)
      questionnaire.value = waiting
    }
    if (!conversationsLoaded) {
      const page = await request<{items:typeof conversations.value}>('/conversations?channel=assistant&personal=true', 'GET', undefined, signal)
      if (!signal.aborted) {
        conversations.value = page.items.filter(item => !item.parent_id)
        conversationsLoaded = true
      }
    }
  } catch (error) {
    if (signal.aborted) return
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      data.value = questionnaire.value = editing.value = null
      selected.value = chats.value = []
      conversations.value = []
      conversationsLoaded = false
      knownAssets.clear()
      autoShown.clear()
      responses.value = {}; draft.value = {}; prompt.value = ''
    }
    notifyError(error)
  }
}
const { restart: reload } = usePolling(readWorkspace, pollDelay)
async function act(path:string, method='POST', body:unknown = {}) {
  if (busy.value) return false
  busy.value = true
  try { await request(path, method, body); await reload(); return !disposed }
  catch (error) { if (!disposed) notifyError(error); return false }
  finally { busy.value = false }
}
function editAnswer(item:Build, answer:string) {
  responses.value[item.id] = {answer, revision:responses.value[item.id]?.revision ?? item.updated_at}
}
function staleAnswer(item:Build) {
  const response = responses.value[item.id]
  return !!response && response.revision !== item.updated_at
}
async function submitAnswer(item:Build) {
  const response = responses.value[item.id]
  if (!response?.answer.trim() || staleAnswer(item)) return
  if (await act(`/me/knowledge/builds/${encodeURIComponent(item.id)}/answer`, 'POST', response) && responses.value[item.id] === response) delete responses.value[item.id]
}
async function build() { if (editable.value && !rebuilding.value) { editing.value = editable.value; draft.value = {...editable.value.document}; prompt.value = editable.value.prompt; return } if (!confirm('分析和问答将使用当前模型额度，确认开始？')) return; if (await act('/me/knowledge/builds','POST',{prompt:prompt.value,asset_ids:selected.value,interview_rounds:rounds.value,conversation_ids:chats.value,rebuild:rebuilding.value})) rebuilding.value=false }
function rebuild(item:Build) { prompt.value=item.prompt; rounds.value=item.interview_rounds===5?5:3; selected.value=item.asset_ids.filter(id=>data.value?.assets.some(asset=>asset.id===id && supportsSoulAsset(asset.filename))); rebuilding.value=true }
async function refine() { if (!editing.value) return; if (await act(`/me/knowledge/builds/${encodeURIComponent(editing.value.id)}/refine`,'POST',{revision:editing.value.updated_at,document:draft.value,prompt:prompt.value,asset_ids:data.value?.assets.filter(a=>supportsSoulAsset(a.filename)).map(a=>a.id) || []})) editing.value=null }
function retryLearning(){if(confirm('继续学习尚未处理的本人气泡？后续调用使用当前模型额度。'))void act('/me/knowledge/learning/retry')}
function remove(path:string, message:string, method='DELETE') { if (confirm(message)) void act(path,method) }
onBeforeUnmount(() => { disposed = true })
</script>
<template>
<section class="card" id="knowledge-workspace"><div class="cardhead"><h2>Soul</h2><span class="tag">{{ active ? '当前 Soul 已生效' : '尚未启用 Soul' }}</span></div>
<p class="sub">使用素材和问答完善表达。手动修订需审核；分析使用当前模型额度。</p>
<p v-if="data?.folders?.error" class="notice">{{ data.folders.error }}</p><p v-else-if="data?.folders?.pending" class="sub">新增素材已排队，将在当前构建完成后增量完善。</p>
<p v-if="data?.learning">风格学习：{{ data.learning.status }} {{ data.learning.error }}</p><ElButton v-if="data?.learning?.status === 'failed'" @click="retryLearning">重试风格学习</ElButton>
<section class="soul-persona"><h3 data-guide-target="knowledge">个人形象概览</h3><div v-if="active" class="soul-document"><section v-for="(label,key) in labels" :key="key" class="soul-section"><h3>{{ label }}</h3><MarkdownContent :source="active.document[key] || '尚未形成明确偏好。'" :diagrams="false"/></section></div><p v-else class="sub">完成构建后展示个人形象。</p>
<MemoryEditor v-if="editable" :build="editable" @saved="reload" />
<div v-if="!active || rebuilding"><label class="field">塑造提示词<ElInput placeholder=" " v-model="prompt" type="textarea" maxlength="20000" /></label><label class="field">了解程度<ElSelect v-model="rounds"><ElOption :value="3" label="基础了解 · 每类 3 道"/><ElOption :value="5" label="深入了解 · 每类 5 道"/></ElSelect></label><details><summary>选择本人聊天记录</summary><ElCheckbox v-for="chat in filteredChats.slice(chatOffset,chatOffset+10)" :key="chat.id" :model-value="chats.includes(chat.id)" @change="value => { chats=value ? [...chats,chat.id] : chats.filter(id=>id!==chat.id) }">{{ chat.title }} · {{ chat.message_count }} 条</ElCheckbox><RecordPagination v-model:offset="chatOffset" :total="filteredChats.length" :search="chatSearch" label="本人聊天分页" @update:search="chatSearch=$event;chatOffset=0"/></details><p class="sub">当前模型：{{ data?.model?.model || '未配置' }}</p></div>
<div class="actions"><ElButton type="primary" :loading="busy" @click="build">{{ editable && !rebuilding ? '继续完善 Soul' : '开始构建 Soul' }}</ElButton><ElButton v-if="active" @click="rebuilding=!rebuilding">{{ rebuilding ? '取消重新创建' : '重新创建 Soul' }}</ElButton><ElButton v-if="active" @click="remove('/me/knowledge/active','停用当前 Soul？')">停用当前 Soul</ElButton></div></section>
<AssetLibrary v-if="data" v-model:selected="selected" :assets="data.assets" :quota="data.quota" @refresh="reload"/>
<section class="soul-build-history">
  <div class="cardhead"><h3>构建与修订记录</h3><button type="button" class="button small danger" @click="remove('/me/knowledge/builds/delete-all','删除所有可删除的构建记录？','POST')">删除全部记录</button></div>
  <p v-if="!filteredBuilds.length" class="sub">{{ buildSearch ? '没有匹配的构建记录。' : '暂无构建记录。' }}</p>
  <article v-for="item in filteredBuilds.slice(buildOffset,buildOffset+10)" :key="item.id" class="soul-build soul-build-record">
    <div class="soul-build-row">
      <div class="soul-build-status">
        <span class="tag" :class="item.status">{{ buildStatus(item.status) }}</span>
        <span v-if="item.id===data?.active_build_id" class="tag">当前生效</span>
        <time class="sub">{{ date(item.created_at) }}</time>
      </div>
      <div class="soul-build-progress">
        <progress v-if="item.progress < 100" max="100" :value="item.progress" aria-label="构建进度"/>
        <span class="sub">{{ item.stage && item.stage!==buildStatus(item.status) ? item.stage+' · ' : '' }}{{ item.progress }}%</span>
      </div>
      <div class="soul-build-actions">
        <button v-if="item.status==='waiting_user' && item.question_plan?.length && item.phase!=='followup'" type="button" class="button small primary" @click="questionnaire=item">打开题库作答</button>
        <button v-if="item.status==='ready'" type="button" class="button small" @click="act(`/me/knowledge/builds/${encodeURIComponent(item.id)}/submit`)">确认草案并提交审核</button>
        <button v-if="['queued','running','waiting_user','ready','pending'].includes(item.status)" type="button" class="button small" @click="act(`/me/knowledge/builds/${encodeURIComponent(item.id)}/cancel`)">{{ item.status==='pending' ? '撤回审核' : '取消构建' }}</button>
        <button v-if="item.status==='failed' && item.origin!=='learning'" type="button" class="button small" @click="act(`/me/knowledge/builds/${encodeURIComponent(item.id)}/retry`)">重试构建</button>
        <button v-else-if="['failed','rejected','canceled','cancelled'].includes(item.status)" type="button" class="button small" @click="rebuild(item)">用此输入重新构建</button>
        <button v-if="!['queued','running','waiting_user','ready','pending'].includes(item.status) && item.id!==data?.active_build_id" type="button" class="button small danger" @click="remove(`/me/knowledge/builds/${encodeURIComponent(item.id)}`,'删除构建记录？')">删除记录</button>
      </div>
    </div>
    <p v-if="item.error || item.reason" class="notice">{{ item.error || item.reason }}</p>
    <details class="compact-disclosure soul-build-details">
      <summary><AppIcon name="chevron" :size="14"/>查看草案与问答</summary>
      <div class="soul-document">
        <section v-for="(label,key) in labels" v-show="item.document?.[key]" :key="key" class="soul-section"><h4>{{ label }}</h4><MarkdownContent :source="item.document?.[key] || ''" :diagrams="false"/></section>
      </div>
      <div v-for="(turn,i) in item.interview" :key="i" class="soul-interview-turn"><p class="soul-question">{{ turn.question }}</p><p class="soul-answer">{{ turn.answer }}</p></div>
    </details>
    <div v-if="item.status==='waiting_user' && (!item.question_plan?.length || item.phase==='followup')">
      <ElInput :model-value="responses[item.id]?.answer || ''" type="textarea" maxlength="4000" placeholder="用平时聊天的语气回答" @update:model-value="editAnswer(item,$event)"/>
      <template v-if="staleAnswer(item)"><p role="status">问题已更新，旧回答保留在输入框中。</p><ElButton size="small" @click="delete responses[item.id]">重新作答</ElButton></template>
      <ElButton size="small" :disabled="busy || staleAnswer(item) || !responses[item.id]?.answer.trim()" @click="submitAnswer(item)">回答并继续</ElButton>
    </div>
  </article>
  <RecordPagination v-model:offset="buildOffset" :total="filteredBuilds.length" :search="buildSearch" label="Soul 构建记录分页" @update:search="buildSearch=$event;buildOffset=0"/>
</section>
<QuestionnaireDialog :build="questionnaire" @close="questionnaire=null" @submitted="reload"/><AppDialog :model-value="!!editing" title="完善当前 Soul" @close="editing=null"><label class="field">塑造提示<ElInput placeholder=" " v-model="prompt" type="textarea" /></label><label v-for="(label,key) in labels" :key="key" class="field">{{ label }}<ElInput placeholder=" " v-model="draft[key]" type="textarea" maxlength="4000" /></label><template #footer><ElButton type="primary" @click="refine">保存微调</ElButton></template></AppDialog></section>
</template>
