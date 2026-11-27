<script setup lang="ts">
import { useConfirmation } from '../../composables/useConfirmation'
import { date, fileSizeMB } from '../../utils/format'
import RecordPagination from '../RecordPagination.vue'
import { defineAsyncComponent, computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { ElButton, ElInput } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import MaterialFileDialog from '../MaterialFileDialog.vue'
import type { AnimationMetadata, FilePromotion } from '../../types'
import { request, notifyError } from '../../api/client'
import AppIcon from '../AppIcon.vue'
import MaterialEntry from '../MaterialEntry.vue'
import MaterialTypeIcon from '../MaterialTypeIcon.vue'

const confirmAction = useConfirmation()
const FilePreview = defineAsyncComponent(() => import('../FilePreview.vue'))
const MAX_CHAT_UPLOAD_BYTES = 100 * 1024 * 1024
const props=defineProps<{conversationId:string;busy?:boolean;readOnly?:boolean;temporary?:boolean;includeConversationFiles?:boolean;selectedIds?:string[];label?:string;sharedRevision?:string}>()
const emit=defineEmits<{selected:[ids:string[]];uploading:[value:boolean]}>()
interface ChatFile {id:string;filename:string;content_type:string;size:number;description:string;referenced:boolean;download_url:string;origin?:string;source_label?:string;expires_at?:string|null;shared_attachment?:boolean;promoted_id?:string|null}
interface LibraryFile extends AnimationMetadata {id:string;filename:string;source:'folder'|'profile';path?:string;relative_path?:string;description:string;size:number;sha256?:string;already_referenced?:boolean}
interface LibraryPage {items:LibraryFile[];total:number;offset:number;limit:number}
interface Result {items:ChatFile[];file_id?:string}
const files=ref<ChatFile[]>([]), working=ref(false), status=ref(''), preview=ref<ChatFile|null>(null), detail=ref<ChatFile|null>(null), description=ref(''), promoting=ref<ChatFile|null>(null), folders=ref<string[]>([])
const uploadInput = ref<HTMLInputElement | null>(null), uploadController = shallowRef<AbortController>()
const libraryOpen=ref(false), library=ref<LibraryFile[]>([]), selectedLibrary=ref<LibraryFile[]>([]), search=ref(''), folder=ref(''), offset=ref(0), total=ref(0), listOffset=ref(0)
const selectedInDraft=ref<string[]>([...(props.selectedIds || [])])
const included=(file:ChatFile)=>props.temporary ? selectedInDraft.value.includes(file.id) : file.origin!=='attachment' && file.referenced
const visible=computed(()=>files.value.filter(file=>props.temporary
  ? props.includeConversationFiles || file.origin==='attachment' || included(file) : file.origin!=='attachment' || file.shared_attachment))
const active=computed(()=>visible.value.filter(included)), page=computed(()=>visible.value.slice(listOffset.value,listOffset.value+10))
const selectionKey = (file: LibraryFile) => file.sha256 || file.id
const uniqueSelection = computed(() => [...new Map(selectedLibrary.value.map(file => [selectionKey(file), file])).values()])
const overLimit = computed(() => active.value.length + uniqueSelection.value.length > 5)
const libraryLoading = ref(false), libraryError = ref(false)
const segments = computed(() => folder.value.split('/').filter(Boolean).map((name, i, parts) => ({name, path:parts.slice(0, i+1).join('/')})))
let generation=0, controller=new AbortController(), librarySequence=0
const endpoint=(id:string)=>'/conversations/'+encodeURIComponent(id)+'/files'
function expiryTimestamp(value: string) {
  return /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value) ? value.replace(' ','T')+'Z' : value
}
function emitSelection(){emit('selected',active.value.map(file=>file.id))}
function update(items:ChatFile[],ticket:number,addedId?:string){
  if(ticket!==generation)return
  if(!Array.isArray(items)) throw Error('文件列表响应格式错误')
  if(props.temporary && addedId && !selectedInDraft.value.includes(addedId)) {
    selectedInDraft.value=[...selectedInDraft.value,addedId]
  }
  files.value=items
  emitSelection()
}
async function refresh(id:string,ticket:number){working.value=true;emit('uploading',true);try{const rows=await request<ChatFile[]>(endpoint(id),'GET',undefined,controller.signal);update(rows,ticket)}catch(e){if(ticket===generation){status.value='读取会话文件失败，请重新进入会话';notifyError(e)}}finally{if(ticket===generation){working.value=false;emit('uploading',false)}}}
watch(
  () => props.conversationId,
  (id) => {
    generation++
    controller.abort()
    uploadController.value = undefined
    controller = new AbortController()
    librarySequence++
    selectedInDraft.value = [...(props.selectedIds || [])]
    files.value = []
    selectedLibrary.value = []
    libraryOpen.value = false
    preview.value = null
    detail.value = null
    promoting.value = null
    status.value = ''
    working.value = false
    listOffset.value = 0
    emit('selected', [...selectedInDraft.value])
    emit('uploading', false)
    if (id) void refresh(id, generation)
  },
  { immediate: true }
)
function refreshShared() {
  if(props.sharedRevision!==undefined && props.conversationId && !working.value) void refresh(props.conversationId,generation)
}
watch(()=>props.sharedRevision,refreshShared)
onBeforeUnmount(()=>{generation++;controller.abort();emit('uploading',false)})
async function operation(fn: (id: string, signal: AbortSignal, ticket: number) => Promise<void>) {
  if (props.busy || working.value || !props.conversationId) {
    status.value = '请等待当前操作完成'
    return
  }
  const ticket = generation,
    id = props.conversationId,
    signal = controller.signal
  working.value = true
  emit('uploading', true)
  try {
    await fn(id, signal, ticket)
  } catch (e) {
    if (ticket === generation) {
      status.value = e instanceof Error ? e.message : '操作失败'
      notifyError(e)
    }
  } finally {
    if (ticket === generation) {
      working.value = false
      emit('uploading', false)
    }
  }
}
async function toggle(file:ChatFile,event:Event){
  const target=event.target
  if(!(target instanceof HTMLInputElement))return
  const enabled=target.checked
  target.checked=included(file)
  if(enabled&&active.value.length>=5){status.value='最多同时引用五份文件';return}
  await operation(async(id,signal,ticket)=>{
    const result=await request<Result>(endpoint(id)+'/'+encodeURIComponent(file.id)+'/reference','PATCH',{enabled},signal)
    update(result.items,ticket)
  })
}
function toggleAttachment(file:ChatFile) {
  if (!included(file) && active.value.length>=5) {status.value='一次最多发送五份附件';return}
  selectedInDraft.value=included(file)
    ? selectedInDraft.value.filter(id=>id!==file.id)
    : [...selectedInDraft.value,file.id]
  emitSelection()
}
async function upload(event:Event) {
  const target = event.target
  if (!(target instanceof HTMLInputElement) || !target.files?.length) return
  const list = Array.from(target.files)
  target.value = ''
  if (props.temporary && active.value.length + list.length > 5) {
    status.value = '一次最多发送五份附件'
    return
  }
  const oversized = list.find(file => file.size > MAX_CHAT_UPLOAD_BYTES)
  if (oversized) {
    status.value = `文件「${oversized.name}」超过 100 MiB`
    return
  }
  await operation(async (id, sessionSignal, ticket) => {
    const transfer = new AbortController()
    uploadController.value = transfer
    const signal = AbortSignal.any([sessionSignal, transfer.signal])
    let done = 0
    try {
      for (const file of list) {
        signal.throwIfAborted()
        status.value = '正在上传：' + file.name
        const form = new FormData()
        form.append('file', file)
        if (props.temporary) form.append('temporary', 'true')
        const result = await request<Result>(endpoint(id), 'POST', form, signal, null)
        update(result.items, ticket, result.file_id)
        done++
      }
      if (ticket === generation) status.value = `已添加 ${done} 个文件` + (props.temporary ? '到本次附件' : '到会话')
    } catch (error) {
      if (sessionSignal.aborted || ticket !== generation) return
      if (!transfer.signal.aborted) throw new Error(`已添加 ${done} 个文件；${error instanceof Error ? error.message : '上传失败'}`)
      // A canceled response can follow a server commit. Read back the current conversation.
      status.value = '上传已停止，正在刷新文件列表'
      update(await request<ChatFile[]>(endpoint(id), 'GET', undefined, sessionSignal), ticket)
      if (ticket === generation) status.value = '上传已停止，文件列表已刷新'
    } finally {
      if (uploadController.value === transfer) uploadController.value = undefined
    }
  })
}
async function openLibrary(){if(props.busy||working.value||!props.conversationId)return;libraryOpen.value=true;status.value='';offset.value=0;total.value=0;library.value=[];selectedLibrary.value=[];folder.value='';search.value='';await loadLibrary(0)}
async function loadLibrary(next:number) {
  const seq = ++librarySequence, id = props.conversationId
  libraryLoading.value = true
  libraryError.value = false
  try {
    const query = new URLSearchParams({folder:folder.value, search:search.value, offset:String(next)})
    const result = await request<LibraryPage>('/conversations/'+encodeURIComponent(id)+'/file-library?'+query,'GET',undefined,controller.signal)
    if (seq !== librarySequence) return
    library.value = result.items
    offset.value = result.offset
    total.value = result.total
  } catch (error) {
    if (seq !== librarySequence || controller.signal.aborted) return
    libraryError.value = true
    notifyError(error)
  } finally {
    if (seq === librarySequence) libraryLoading.value = false
  }
}
function navigate(path:string){folder.value=path;search.value='';void loadLibrary(0)}
async function chooseFolder(item: LibraryFile) {
  if (!item.path) return
  await operation(async (id, signal, ticket) => {
    const all: LibraryFile[] = []
    let start = 0
    while (true) {
      const query = new URLSearchParams({
        folder: item.path || '',
        recursive: 'true',
        offset: String(start)
      })
      const result = await request<LibraryPage>(
        '/conversations/' + encodeURIComponent(id) + '/file-library?' + query,
        'GET',
        undefined,
        signal
      )
      all.push(
        ...result.items.filter((file) => file.source === 'profile' && !file.already_referenced)
      )
      start += result.items.length
      if (start >= result.total || !result.items.length) break
    }
    if (ticket !== generation) return
    const unique = new Map<string, LibraryFile>()
    for (const file of [...selectedLibrary.value, ...all]) unique.set(file.sha256 || file.id, file)
    selectedLibrary.value = [...unique.values()]
  })
}
function choose(file:LibraryFile,checked:boolean){selectedLibrary.value=checked?[...selectedLibrary.value.filter(x=>x.id!==file.id),file]:selectedLibrary.value.filter(x=>x.id!==file.id)}
async function reference() {
  const selected = uniqueSelection.value
  if (!selected.length || overLimit.value) return
  await operation(async (id, signal, ticket) => {
    let count = 0
    try {
      for (const file of selected) {
        const result = await request<Result>(endpoint(id)+'/reference','POST',{source:'profile',id:file.id,temporary:!!props.temporary},signal)
        if (ticket !== generation) return
        update(result.items,ticket,result.file_id)
        selectedLibrary.value = selectedLibrary.value.filter(item => selectionKey(item) !== selectionKey(file))
        library.value.forEach(item => { if (selectionKey(item) === selectionKey(file)) item.already_referenced = true })
        count++
      }
    } catch (e) {
      throw new Error(`已引用 ${count} 份；${e instanceof Error ? e.message : '引用失败'}`)
    }
    libraryOpen.value = false
    status.value = props.temporary ? `已添加 ${count} 份附件` : `已引用 ${count} 份资料`
  })
}
function openDetail(file:ChatFile){detail.value=file;description.value=file.description||''}
async function saveDescription(){const file=detail.value;if(!file)return;await operation(async(id,signal,ticket)=>{const result=await request<Result>(endpoint(id)+'/'+encodeURIComponent(file.id),'PATCH',{description:description.value},signal);update(result.items,ticket);if(ticket===generation)detail.value=null})}
async function remove(file: ChatFile) {
  if (props.readOnly || props.busy || working.value) return
  await confirmAction('删除会话原件及索引？已转存的资料副本保留。', '确认删除', () => [
    generation, props.conversationId, working.value, props.busy, props.readOnly, files.value.some(item => item.id === file.id)
  ], () => operation(async (id, signal, ticket) => {
    const result = await request<Result>(endpoint(id) + '/' + encodeURIComponent(file.id), 'DELETE', undefined, signal)
    update(result.items, ticket)
  }))
}
async function openPromotion(file:ChatFile){if(props.busy||working.value||file.promoted_id)return;status.value='';const ticket=generation;try{const result=await request<{items:string[]}>('/me/material-folders','GET',undefined,controller.signal);if(ticket!==generation)return;folders.value=result.items;promoting.value=file}catch(e){if(ticket===generation)notifyError(e)}}
async function promote(value:FilePromotion) {
  const file = promoting.value
  if (!file) return
  await operation(async (id,signal,ticket) => {
    const result = await request<Result>(endpoint(id)+'/'+encodeURIComponent(file.id)+'/promote', 'POST', value, signal)
    update(result.items, ticket)
    if (ticket === generation) { promoting.value = null; status.value = '已转存到资料库' }
  })
}
</script>
<template>
  <details
    class="composer-menu composer-tool"
    @toggle="($event.target as HTMLDetailsElement).open && refreshShared()"
  >
    <summary :title="label || (temporary ? '添加和预览发送附件' : '上传、引用和管理会话文件')">
      <AppIcon name="folder" :size="14" />
      <span>{{ label || '文件' }}</span>
      <small>{{ sharedRevision === undefined ? active.length : visible.length }}</small>
    </summary>
    <div class="composer-popover chat-file-menu">
      <h3>{{ label || (temporary ? '本次发送附件' : '会话文件') }}</h3>
      <div class="actions">
  <button v-if="!readOnly" type="button" class="button small" :disabled="busy||working||!conversationId" @click="uploadInput?.click()">上传本地文件</button>
  <button v-if="!readOnly" type="button" class="button small" :disabled="busy||working||!conversationId" @click="openLibrary">{{ temporary ? '从资料库添加' : '从资料库引用' }}</button>
  <button v-if="uploadController" type="button" class="button small" @click="uploadController.abort()">取消上传</button>
  <input ref="uploadInput" hidden type="file" multiple accept=".png,.jpg,.jpeg,.webp,.gif,.bmp,.txt,.md,.mmd,.html,.htm,.csv,.tsv,.xlsx,.json,.log,.pdf,.docx,.pptx,.mp3,.wav,.m4a,.ogg,.flac,.webm,.mp4,.mov" :disabled="busy||working||!conversationId" @change="upload">
</div>
<p v-if="!readOnly" class="sub">{{ temporary?'发送选中的原件副本，最多五份。':'后续提问最多同时引用五份，超过时可继续上传。其他会话临时文件需先转存为资料；索引与检索使用模型额度。' }} 本地上传每个文件最多 100 MiB。</p>
<p v-if="status" class="sub" role="status">{{ status }}</p>
<article v-for="file in page" :key="file.id" class="chat-file compact-chat-file"><label><input v-if="!temporary && !readOnly && file.origin!=='attachment'" type="checkbox" :checked="included(file)" :disabled="busy||working" @change="toggle(file,$event)"><MaterialTypeIcon :filename="file.filename"/> {{ file.filename }}</label>
    <div v-if="sharedRevision !== undefined" class="sub">
      <template v-if="file.expires_at"><time :datetime="expiryTimestamp(file.expires_at)">{{ date(expiryTimestamp(file.expires_at)) }}</time> 到期自动删除</template>
      <template v-else>未设到期时间</template>
    </div>
    <div class="actions"><el-button @click="preview=file">{{ /\.mmd$/i.test(file.filename) ? '预览与下载' : '预览' }}</el-button>
    <el-button v-if="temporary" :disabled="busy||working" @click="toggleAttachment(file)">{{ included(file) ? '移除附件' : '添加为附件' }}</el-button>
    <template v-else>
      <el-button v-if="!readOnly" @click="openDetail(file)">详情与说明</el-button>
      <el-button
        v-if="!/\.mmd$/i.test(file.filename)"
        tag="a"
        :href="file.download_url"
        :download="file.filename"
      >下载</el-button>
      <el-button
        :disabled="busy || working || !!file.promoted_id"
        @click="openPromotion(file)"
      >{{ file.promoted_id ? '已转存' : '转存资料' }}</el-button>
      <el-button v-if="!readOnly" :disabled="busy || working" @click="remove(file)">删除</el-button>
    </template>
  </div>
</article>
<p v-if="!visible.length" class="sub">{{ temporary ? '暂无发送附件。' : '本会话暂无文件。' }}</p>
<RecordPagination v-model:offset="listOffset" :total="visible.length" :label="temporary ? '附件分页' : '会话文件分页'" unit="项"/></div></details><FilePreview v-if="preview" :file="preview" :url="preview.download_url" @close="preview=null"/>
<AppDialog v-model="libraryOpen" :title="temporary ? '从资料库添加附件' : '从资料库引用'" class="library-picker" width="min(52rem, calc(100vw - 1rem))" align-center>
  <div class="file-location picker-location">
    <div class="file-navigation"><button type="button" aria-label="上一级" title="上一级" :disabled="!folder" @click="navigate(segments.at(-2)?.path || '')"><AppIcon name="up"/></button></div>
    <div class="file-address"><AppIcon name="folder" :size="16"/>
      <nav class="file-address-segments" aria-label="资料库路径">
        <button v-if="folder" type="button" @click="navigate('')">根目录</button><span v-else aria-current="location">根目录</span>
        <template v-for="segment in segments" :key="segment.path"><AppIcon name="chevron" :size="12"/>
          <span v-if="segment.path===folder" aria-current="location">{{ segment.name }}</span>
          <button v-else type="button" @click="navigate(segment.path)">{{ segment.name }}</button>
        </template>
      </nav>
    </div>
  </div>
  <div class="picker-scroll" role="region" aria-label="资料库文件列表" tabindex="0" :aria-busy="libraryLoading">
    <p v-if="libraryLoading" class="sub" role="status">正在读取资料文件…</p>
    <p v-else-if="libraryError" role="alert">资料库加载失败 <button class="button small" @click="loadLibrary(offset)">重试</button></p>
    <table v-else-if="library.length" class="material-file-table">
      <thead><tr><th scope="col">名称</th><th scope="col">状态 / 操作</th><th scope="col">大小</th></tr></thead>
      <tbody><tr v-for="item in library" :key="item.id">
        <td>
          <input v-if="item.source==='profile'" type="checkbox" data-material-pick :aria-label="'选择 '+item.filename" :disabled="working || item.already_referenced" :checked="selectedLibrary.some(x=>x.id===item.id)" @change="choose(item,($event.target as HTMLInputElement).checked)">
          <MaterialEntry v-if="item.source==='folder'" folder :name="item.filename" @click="navigate(item.path || '')"/>
          <MaterialEntry v-else :name="item.filename" :path="item.relative_path" :metadata="item" :disabled="working || item.already_referenced" :aria-pressed="selectedLibrary.some(x=>x.id===item.id)" @click="choose(item,!selectedLibrary.some(x=>x.id===item.id))"/>
          <small v-if="item.description">{{ item.description }}</small>
        </td>
        <td><button v-if="item.source==='folder'" class="button small" :disabled="busy||working" @click="chooseFolder(item)">选择目录内文件</button>
          <span v-else-if="item.already_referenced" class="tag">{{ temporary ? '已添加到文件列表' : '已在会话' }}</span><span v-else class="sub">{{ temporary ? '可添加' : '可引用' }}</span></td>
        <td>{{ fileSizeMB(item.size) }}</td>
      </tr></tbody>
    </table>
    <p v-else class="sub material-empty">{{ search ? '没有匹配的文件。' : '当前目录暂无文件。' }}</p>
  </div>
  <RecordPagination :offset="offset" :total="total" :search="search" label="资料库分页" unit="项" @update:offset="loadLibrary($event)" @update:search="search=$event;loadLibrary(0)"/>
  <template #footer>
    <div v-if="selectedLibrary.length" class="picker-selection" aria-label="已选文件">
      <button v-for="item in selectedLibrary" :key="item.id" class="button small" :aria-label="'移除 '+item.filename" :disabled="working" @click="choose(item,false)"><span>{{ item.filename }}</span><AppIcon name="close" :size="12"/></button>
    </div>
    <p v-if="overLimit" class="sub" role="status">所选文件超过五份，请移除多余文件。</p>
    <p v-if="status" class="sub" role="status">{{ status }}</p>
    <div class="picker-footer"><span class="sub">{{ temporary ? '附件' : '会话已引用' }} {{ active.length }} 份 · 已选 {{ uniqueSelection.length }} 份 · 上限 5 份</span>
      <button type="button" class="button small" @click="libraryOpen=false">取消</button>
      <button type="button" class="button small primary" :disabled="!selectedLibrary.length || overLimit || busy || working" @click="reference">{{ temporary ? '添加所选' : '引用所选' }}</button>
    </div>
  </template>
</AppDialog>
<AppDialog :model-value="!!detail" title="会话文件详情" @close="detail = null">
  <template v-if="detail">
    <p>{{ detail.filename }} · {{ fileSizeMB(detail.size) }} ·
      {{ detail.source_label || '会话文件' }} {{ detail.expires_at }}</p>
    <label class="field">内容说明<el-input placeholder=" " v-model="description" type="textarea" maxlength="2000" />
    </label>
    <el-button :disabled="busy || working" @click="saveDescription">保存说明</el-button>
  </template>
</AppDialog>
<MaterialFileDialog
  v-if="promoting"
  :filename="promoting.filename"
  :caption="promoting.description"
  :folders="folders" :disabled="busy || working" :save="promote" @close="promoting=null"/>
</template>
