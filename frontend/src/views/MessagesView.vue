<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton } from 'element-plus'
import AppDialog from '../components/AppDialog.vue'
import ContextMenu from '../components/ContextMenu.vue'
import RecordPagination from '../components/RecordPagination.vue'
import { request, notifyError } from '../api/client'
import { useNotificationInbox } from '../composables/useNotificationInbox'
import { usePolling } from '../composables/usePolling'
import { date } from '../utils/format'
interface Notice { id:number; title:string; content:string; kind:string; read_at:string|null; created_at:string; session_id?:string; task_id?:string; target_page?:string; admin_only?:boolean }
const props = withDefaults(defineProps<{ management?: boolean }>(), { management: true })
const emit = defineEmits<{ opened: [] }>()
const router = useRouter(), items = ref<Notice[]>([]), total = ref(0), offset = ref(0), status = ref('all'), category = ref('all'), search = ref(''), selected = ref<number[]>([]), detail = ref<Notice | null>(null), busy = ref(false), failure = ref('')
const inbox = useNotificationInbox()
const controller = new AbortController()
const poll = usePolling(async signal => {
  const update = inbox.readUnread()
  try {
    const query = new URLSearchParams({offset:String(offset.value), status:status.value, category:category.value, search:search.value})
    const data = await request<{items:Notice[];total:number;offset:number;unread:number}>('/notifications?' + query, 'GET', undefined, signal)
    if (signal.aborted) return
    items.value = data.items
    total.value = data.total
    offset.value = data.offset
    failure.value = ''
    update(data.unread)
  } catch (error) {
    if (signal.aborted) return
    failure.value = error instanceof Error ? error.message : '读取消息失败'
    if (error instanceof Error && 'status' in error && [401,403].includes(Number(error.status))) {
      items.value = []; selected.value = []; detail.value = null; total.value = 0; update(0)
    }
  }
}, () => 30000)
onMounted(() => { inbox.lists.add(poll.restart) })
onBeforeUnmount(() => { controller.abort(); inbox.lists.delete(poll.restart) })
async function change(action:'read'|'unread'|'delete', ids = selected.value) {
  if (busy.value) return
  if (action === 'delete' && !confirm('删除所选消息条目？聊天和任务仍会保留。')) return
  busy.value = true
  try {
    await request('/notifications/batch', 'POST', {ids,action}, controller.signal)
    if (controller.signal.aborted) return
    selected.value = selected.value.filter(id => !ids.includes(id))
    await inbox.refreshLists()
  } catch (error) { notifyError(error) } finally { busy.value = false }
}
async function deleteAll() {
  if (busy.value || !confirm('删除全部分类、所有分页的消息记录？聊天和任务仍会保留。')) return
  busy.value = true
  try {
    await request('/notifications/delete-all', 'POST', {}, controller.signal)
    if (controller.signal.aborted) return
    selected.value = []
    offset.value = 0
    await inbox.refreshLists()
  } catch (error) { notifyError(error) } finally { busy.value = false }
}
async function open(item:Notice) {
  try {
    await request('/notifications/' + item.id + '/read', 'POST', undefined, controller.signal)
    if (controller.signal.aborted) return
    if (item.session_id) {
      await router.push({path:'/dialogues',query:{session:item.session_id,task:item.task_id}})
      emit('opened')
    } else detail.value = item
    await inbox.refreshLists()
  } catch (error) { notifyError(error) }
}
function go(item:Notice){const path=item.target_page;if(path&&['overview','reviews','jobs','agents','users','settings','history','avatar','profile','account','models','files'].includes(path)){detail.value=null;void router.push(item.admin_only?'/admin/'+(path==='reviews'?'history':path):path==='overview'?'/':'/'+path)}}
const menu=ref<InstanceType<typeof ContextMenu>>()
function showMenu(event:MouseEvent|KeyboardEvent,item:Notice) {
  if (busy.value || !(event.currentTarget instanceof HTMLElement) || (event.target instanceof Element && event.target.closest('input'))) return
  const anchor=event.target instanceof Element && event.target.closest<HTMLButtonElement>('button') || event.currentTarget
  void menu.value?.open(event,anchor,[
    {label:'查看消息',icon:'info',run:()=>void open(item)},
    {label:item.read_at?'标记未读':'标记已读',icon:'sync',run:()=>void change(item.read_at?'unread':'read',[item.id])},
    {label:'删除消息',icon:'trash',run:()=>void change('delete',[item.id])},
  ])
}
const allChecked = computed(()=>!!items.value.length&&items.value.every(item=>selected.value.includes(item.id)))
function toggle(id:number){ if(selected.value.includes(id)) selected.value=selected.value.filter(value=>value!==id); else if(selected.value.length>=100) notifyError(Error('每次最多选择 100 条消息')); else selected.value=[...selected.value,id] }
function toggleAll() {
  const page=items.value.map(item=>item.id)
  if (allChecked.value) selected.value=selected.value.filter(id=>!page.includes(id))
  else {
    const ids=[...new Set([...selected.value,...page])]
    selected.value=ids.slice(0,100)
    if (ids.length>100) notifyError(Error('每次最多选择 100 条消息'))
  }
}
function filter(value:'all'|'read'|'unread'){ status.value=value; offset.value=0; selected.value=[] }
function goPage(value: number) { offset.value=value; void poll.restart() }
watch([status,category],()=>{offset.value=0;selected.value=[];void poll.restart()})
</script>
<template>
  <div v-if="management" class="pagehead"><div><div class="eyebrow">MESSAGES</div><h1>最近动态</h1><p class="sub">查看通知，点击标题进入会话。删除消息条目不会删除聊天或任务。</p></div></div>
  <section :class="management ? 'card' : 'notification-center-popup'">
    <div class="notification-toolbar">
      <div class="profile-tabs notification-filters" role="tablist" data-guide-target="messages">
        <button v-for="[value,label] in ([['all','全部消息'],['read','已读消息'],['unread','未读消息']] as const)" :key="value" type="button" class="profile-tab" :class="{active: status===value}" role="tab" :aria-selected="status===value" @click="filter(value)">{{ label }}</button>
      </div>
      <details class="notification-category"><summary class="button small icon-button" aria-label="消息分类" title="消息分类">☷</summary><label>消息分类<select class="form-control" v-model="category" aria-label="筛选消息类别"><option v-for="[value,label] in ([['all','全部类别'],['chat','分身消息'],['request','聊天请求'],['notification','通知'],['activity','动态']] as const)" :key="value" :value="value">{{ label }}</option></select></label></details>
    </div>
    <div v-if="management" class="notification-batch actions">
      <label class="privacy"><input type="checkbox" :checked="allChecked" :indeterminate="!allChecked && items.some(item=>selected.includes(item.id))" @change="toggleAll">选择本页</label>
      <span class="sub">已选 {{ selected.length }} 条</span>
      <button type="button" class="button small" :disabled="!selected.length||busy" @click="change('read')">批量已读</button>
      <button type="button" class="button small" :disabled="!selected.length||busy" @click="change('unread')">批量未读</button>
      <button type="button" class="button small danger" :disabled="!selected.length||busy" @click="change('delete')">批量删除</button>
      <button type="button" class="button small danger" :disabled="busy" @click="deleteAll">删除全部记录</button>
    </div>
    <p v-if="failure" class="notice error" role="alert">{{ failure }} <button type="button" class="button small" @click="poll.restart()">重试</button></p>
    <div class="notification-list">
      <p v-if="!items.length" class="sub">暂无消息。</p>
      <article v-for="item in items" :key="item.id" class="notification-row ui-record-row" tabindex="-1" @contextmenu="showMenu($event,item)" @keydown="showMenu($event,item)" :class="{unread: !item.read_at}">
        <input v-if="management" type="checkbox" :checked="selected.includes(item.id)" :aria-label="'选择：'+item.title" @change="toggle(item.id)">
        <button type="button" class="notification-line" aria-haspopup="menu" @click="open(item)"><strong>{{ item.title }}</strong><span class="notification-summary">{{ item.content }}</span></button>
        <small class="notification-state">{{ date(item.created_at) }} · {{ item.read_at ? '已读' : '未读' }}</small>
      </article>
    </div>
    <RecordPagination class="notification-pagination" :offset="offset" :total="total" :search="search" label="消息分页" @update:offset="goPage" @update:search="search=$event;goPage(0)"/>
  </section>
  <ContextMenu ref="menu" label="消息操作"/>
  <AppDialog append-to-body :model-value="!!detail" :title="detail?.title" @close="detail=null">
    <template v-if="detail"><small>{{ date(detail.created_at) }}</small><p class="notification-detail-content whitespace-pre-wrap">{{ detail.content }}</p><ElButton v-if="detail.target_page" @click="go(detail)">查看对应页面</ElButton></template>
  </AppDialog>
</template>
