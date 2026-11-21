<script setup lang="ts">
import { usePolling } from '../composables/usePolling'
import { fileSizeMB, quotaSummary, type StorageQuota } from '../utils/format'
import { animationUploadError } from '../utils/animation'
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElButton, ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import MaterialEntry from '../components/MaterialEntry.vue'
import AppIcon from '../components/AppIcon.vue'
import RecordPagination from '../components/RecordPagination.vue'
import MaterialFileDialog from '../components/MaterialFileDialog.vue'
import type { AnimationMetadata, FilePromotion } from '../types'
import { request, notifyError } from '../api/client'
import { useSessionStore } from '../stores/session'
const FilePreview = defineAsyncComponent(() => import('../components/FilePreview.vue'))
interface FileItem extends AnimationMetadata {id:string;filename:string;relative_path:string;content_type:string;size:number;caption:string;visibility:'public'|'private';updated_at:string}
interface Folders {quota?:StorageQuota;items:string[];captions:Record<string,string>;visibility:Record<string,string>;updated_at?:string}
const session=useSessionStore(), files=ref<FileItem[]>([]), folders=ref<Folders>({items:[],captions:{},visibility:{}}), folder=ref(''), search=ref(''), selected=ref<string[]>([]), chosen=ref<FileItem|null>(null), caption=ref(''), status=ref(''), busy=ref(false), index=ref('')
const renaming = ref<FileItem|null>(null)
const route = useRoute(), router = useRouter()
const trail = ref<string[]>(['']), trailIndex = ref(0)
const editingPath = ref(false), pathText = ref(''), pathError = ref(false)
const pathInput = ref<HTMLInputElement>(), pathEdit = ref<HTMLButtonElement>(), breadcrumb = ref<HTMLElement>()
const segments = computed(() => {
  const parts = folder.value ? folder.value.split('/') : []
  return [{ label: '根目录', path: '' }, ...parts.map((label, index) => ({ label, path: parts.slice(0, index + 1).join('/') }))]
})
let controller:AbortController|null=null
const serverVisibility=new Map<string,'public'|'private'>()
type FileDraft = {updated_at:string} & Partial<Pick<FileItem, 'caption'|'visibility'>>
const fileDrafts = new Map<string, FileDraft>(), folderDrafts = new Map<string,string>()
const preview = ref<FileItem|null>(null)
function chooseFile(file:FileItem) { chosen.value = file }
function rememberFile(field:'caption'|'visibility') {
  const file = chosen.value
  if (!file) return
  const draft = {...fileDrafts.get(file.id), updated_at:file.updated_at}
  if (field === 'caption') draft.caption = file.caption
  else if (file.visibility !== serverVisibility.get(file.id)) draft.visibility = file.visibility
  else delete draft.visibility
  if (draft.caption === undefined && draft.visibility === undefined) fileDrafts.delete(file.id)
  else fileDrafts.set(file.id, draft)
}
async function discardFileDraft() {
  if (!chosen.value) return
  fileDrafts.delete(chosen.value.id)
  await load()
}
const modified=ref<string|null>(null)
interface IndexItem {source:string;file_id:string;status:string;coverage:string;error:string}
interface IndexStatus {
  configured:boolean
  items:IndexItem[]
  summary:{profile:{total:number;completed:number;queued:number;running:number;failed:number;blocked:number;partial?:number}}
}
const indexRows = ref<IndexItem[]>([]), indexing = ref(false)
const canIndex = computed(() => !!session.user && session.user.access_scope !== 'profile')
let disposed = false, loadSequence = 0
async function readIndex(signal:AbortSignal) {
  try {
    const value = await request<IndexStatus>('/me/file-index?source=profile', 'GET', undefined, signal)
    if (signal.aborted) return
    indexRows.value = value.items.filter(item => item.source === 'profile')
    const c = value.summary.profile
    const partial = c.partial ?? indexRows.value.filter(item => item.status === 'partial').length
    indexing.value = value.configured && (c.queued > 0 || c.running > 0)
    const notes = [`共 ${c.total} 个文件 · 已就绪 ${c.completed}`]
    if (indexing.value) {
      const processed = c.completed + partial + c.failed + c.blocked
      notes.push(`已处理 ${processed} 个 | 正在处理 ${c.running} 个 | 等待处理 ${c.queued} 个`)
    }
    if (partial) notes.push(`部分索引 ${partial}`)
    if (c.blocked) notes.push(`已阻塞 ${c.blocked}`)
    if (c.failed) notes.push(`索引失败 ${c.failed}`)
    if (!value.configured) notes.push('请先配置嵌入模型')
    index.value = notes.join(' · ')
  } catch (error) {
    if (!signal.aborted) {
      indexing.value = false
      index.value = error instanceof Error ? error.message : '索引状态读取失败'
    }
  }
}
const indexPoll = usePolling(readIndex, () => indexing.value ? 5000 : 30000, {enabled:() => canIndex.value})
const visibleFiles = computed(() => {
  const needle = search.value.toLowerCase()
  return files.value.filter(file => needle
    ? [file.relative_path, file.caption, file.title, ...(file.tags || [])].join(' ').toLowerCase().includes(needle)
    : (file.relative_path || file.filename).split('/').slice(0, -1).join('/') === folder.value)
})
const visibleFolders=computed(()=>search.value?[]:folders.value.items.filter(p=>p.split('/').slice(0,-1).join('/')===folder.value))
const context=ref<{kind:'folder'|'file';value:string;x:number;y:number}|null>(null)
function openContext(event:MouseEvent,kind:'folder'|'file',value:string){event.preventDefault();context.value={kind,value,x:event.clientX,y:event.clientY}}
async function contextAction(actionName:'open'|'rename'|'public'|'private'|'delete'){const item=context.value;if(!item)return;context.value=null;if(item.kind==='folder')enter(item.value);else{const file=files.value.find(x=>x.id===item.value);if(file)chooseFile(file)}if(actionName==='rename')await rename();else if(actionName==='open'&&item.kind==='file'&&chosen.value)preview.value=chosen.value;else if(actionName==='public'||actionName==='private'||actionName==='delete'){selected.value=[(item.kind==='folder'?'d:':'f:')+item.value];await batch(actionName)}}
const fileInput = ref<HTMLInputElement>(), folderInput = ref<HTMLInputElement>()
const indexByFile = computed(() => new Map(indexRows.value.map(row => [row.file_id,row])))
const indexLabels: Record<string, string> = { ready: '已就绪', queued: '等待索引', running: '索引中', failed: '索引失败', partial:'部分索引', blocked:'已阻塞', stale:'等待重建' }
function togglePage() {
  const keys = page.value.map(entry => entry.kind === 'folder' ? 'd:' + entry.path : 'f:' + entry.file.id)
  selected.value = keys.every(key => selected.value.includes(key))
    ? selected.value.filter(key => !keys.includes(key)) : [...new Set([...selected.value,...keys])]
}
const offset=ref(0), entries=computed(() => [...visibleFolders.value.map(path=>({kind:'folder' as const,path})),...visibleFiles.value.map(file=>({kind:'file' as const,file}))]), page=computed(()=>entries.value.slice(offset.value,offset.value+10))
watch(() => entries.value.length, total => { offset.value = Math.min(offset.value, Math.max(0, Math.ceil(total / 10) - 1) * 10) })
async function load() {
  const ticket = ++loadSequence
  try {
    const [items, directories] = await Promise.all([request<FileItem[]>('/me/attachments'), request<Folders>('/me/material-folders')])
    if (disposed || ticket !== loadSequence) return
    for (const file of items) serverVisibility.set(file.id, file.visibility)
    files.value = items.map(file => ({...file, ...fileDrafts.get(file.id)}))
    folders.value = directories
    modified.value = directories.updated_at || null
    if (ticket === 1 && typeof route.query.folder === 'string') enter(route.query.folder)
    else if (folder.value && !directories.items.includes(folder.value)) {
      let parent = folder.value
      while (parent && !directories.items.includes(parent)) parent = parent.split('/').slice(0,-1).join('/')
      enter(parent)
    }
    if (chosen.value) chosen.value = files.value.find(file => file.id === chosen.value?.id) || null
  } catch (e) { if (!disposed && ticket === loadSequence) notifyError(e) }
}
async function refreshAfterWrite() {
  await Promise.all([load(), indexPoll.restart()])
}
async function action(path:string,method:string,body?:unknown){busy.value=true;try{await request(path,method,body);await refreshAfterWrite();selected.value=[];ElMessage.success('已保存');return true}catch(e){notifyError(e);return false}finally{busy.value=false}}
async function createFolder(){try{const result=await ElMessageBox.prompt('文件夹名称','新建文件夹',{inputPlaceholder:'请输入文件夹名称'});await action('/me/material-folders','POST',{path:[folder.value,result.value].filter(Boolean).join('/'),last_modified_at:modified.value})}catch(e){if(e!=='cancel'&&e!=='close')notifyError(e)}}
async function rename() {
  if (chosen.value) {
    renaming.value = chosen.value
    return
  }
  const directory = folder.value
  if (!directory) return
  try {
    const {value} = await ElMessageBox.prompt('新名称','重命名',{inputPlaceholder:'请输入新名称'})
    await action('/me/material-folders','PATCH',{path:directory,new_name:value,last_modified_at:modified.value})
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') notifyError(e)
  }
}
async function saveName({ new_name }: FilePromotion) {
  const file = renaming.value
  if (!file) return
  if (await action('/me/attachments/' + encodeURIComponent(file.id) + '/name', 'PATCH', {
    new_name, updated_at: file.updated_at, last_modified_at: modified.value,
  })) renaming.value = null
}
async function save() {
  if (busy.value || (!chosen.value && !folder.value)) return
  busy.value = true
  try {
    if (chosen.value) {
      const {id} = chosen.value, draft = fileDrafts.get(id)
      if (!draft) return
      await request('/me/attachments/'+encodeURIComponent(id),'PATCH',draft)
      if (fileDrafts.get(id) === draft) fileDrafts.delete(id)
    } else {
      const path = folder.value
      await request('/me/material-folders','PATCH',{path,caption:caption.value,last_modified_at:modified.value})
      folderDrafts.delete(path)
    }
    await refreshAfterWrite()
    ElMessage.success('已保存')
  } catch (e) { notifyError(e) } finally { busy.value = false }
}
async function batch(actionName:'public'|'private'|'delete'){if(!selected.value.length)return;if(actionName==='delete'){try{await ElMessageBox.confirm('删除所选文件及文件夹？','确认删除')}catch{return}}await action('/me/attachments/batch','POST',{action:actionName,ids:selected.value.filter(x=>x.startsWith('f:')).map(x=>x.slice(2)),folders:selected.value.filter(x=>x.startsWith('d:')).map(x=>x.slice(2)),last_modified_at:modified.value})}
interface UploadFile {file:File;path:string}
const allowed=/\.(txt|md|mmd|avanim|html?|csv|tsv|xlsx|json|log|png|jpe?g|webp|gif|bmp|pdf|docx?|pptx?|wav|mp3|m4a|ogg|flac|mp4|mov|webm)$/i
async function uploadFiles(list:UploadFile[], directory:boolean, emptyFolders:string[]=[]){
  if(busy.value || (!list.length&&!emptyFolders.length))return
  const sizeError = animationUploadError(list.map(item => item.file))
  if (sizeError) { status.value = sizeError; return }
  const batchId=Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');controller=new AbortController();busy.value=true;let count=0,skipped=0
  try{
    for(const path of emptyFolders){if(controller.signal.aborted)throw new Error('已取消上传');const full=[folder.value,path].filter(Boolean).join('/');if(!folders.value.items.includes(full)){const result=await request<Folders & {last_modified_at:string}>('/me/material-folders','POST',{path:full,last_modified_at:modified.value});folders.value=result;modified.value=result.last_modified_at}}
    for(const item of list){const file=item.file;if(directory&&!allowed.test(file.name)){skipped++;continue}status.value='正在上传：'+file.name;const relative=[folder.value,item.path].filter(Boolean).join('/');const response=await fetch('/api/me/attachments',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':session.csrf,'X-Upload-Batch':batchId,'X-Attachment-Filename':encodeURIComponent(file.name),'X-Attachment-Relative-Path':encodeURIComponent(relative)},body:file,signal:controller.signal});if(!response.ok){const data:unknown=await response.json();throw new Error(typeof data==='object'&&data!==null&&'detail' in data&&typeof data.detail==='string'?data.detail:'上传失败')}count++}
    status.value=`上传完成：${count} 个文件，跳过 ${skipped} 个文件`
  }catch(e){status.value=`已上传 ${count} 个文件；${e instanceof Error?e.message:'上传失败'}`}
  finally{if(list.length)try{await request('/me/upload-batches/'+batchId+'/complete','POST',{kind:'profile',selected:list.length,skipped})}catch(e){notifyError(e)}controller=null;busy.value=false;await refreshAfterWrite()}
}
async function upload(event:Event){const target=event.target;if(!(target instanceof HTMLInputElement)||!target.files?.length)return;const list=Array.from(target.files, file=>({file,path:file.webkitRelativePath||file.name}));await uploadFiles(list,list.some(item=>item.path.includes('/')));target.value=''}
async function readEntry(entry:FileSystemEntry,path:string,files:UploadFile[],dirs:string[]):Promise<void>{
  if(entry.isFile){const fileEntry=entry as FileSystemFileEntry;const file=await new Promise<File>((resolve,reject)=>fileEntry.file(resolve,reject));files.push({file,path:path+file.name});return}
  if(entry.isDirectory){const dir=entry as FileSystemDirectoryEntry,reader=dir.createReader(),next=path+entry.name+'/';dirs.push(next.slice(0,-1));while(true){const batch=await new Promise<FileSystemEntry[]>((resolve,reject)=>reader.readEntries(resolve,reject));if(!batch.length)break;for(const child of batch)await readEntry(child,next,files,dirs)}}
}
async function drop(event: DragEvent) {
  event.preventDefault()
  const transfer = event.dataTransfer
  if (busy.value || !transfer) return
  const list: UploadFile[] = [], dirs: string[] = []
  try {
    // Capture entry handles and files before the browser releases the drop event.
    const entries = Array.from(transfer.items || []).filter(item => item.kind === 'file')
      .map(item => ({ entry: item.webkitGetAsEntry?.(), file: item.getAsFile() }))
    const fallback = Array.from(transfer.files)
    for (const { entry, file } of entries) {
      if (entry) await readEntry(entry, '', list, dirs)
      else if (file) list.push({ file, path: file.webkitRelativePath || file.name })
    }
    if (!list.length && !dirs.length) {
      for (const file of fallback) list.push({ file, path: file.webkitRelativePath || file.name })
    }
    await uploadFiles(list, true, dirs)
  } catch (error) { notifyError(error) }
}

async function syncIndex(rebuild=false){if(!canIndex.value)return;try{await request('/me/file-index/'+(rebuild?'rebuild':'sync'),'POST',{});await indexPoll.restart()}catch(e){notifyError(e)}}
function folderLink(path: string) {
  return { path: route.path, query: { ...route.query, folder: path || undefined } }
}
function enter(path: string, record = true) {
  if (path && !folders.value.items.includes(path)) return false
  if (record && trail.value[trailIndex.value] !== path) {
    trail.value = [...trail.value.slice(0, trailIndex.value + 1), path]
    trailIndex.value++
  }
  folder.value = path
  search.value = ''
  offset.value = 0
  chosen.value = null
  caption.value = folderDrafts.get(path) ?? folders.value.captions[path] ?? ''
  selected.value = []
  if (route.query.folder !== (path || undefined)) void router.replace(folderLink(path))
  void nextTick(() => { if (breadcrumb.value) breadcrumb.value.scrollLeft = breadcrumb.value.scrollWidth })
  return true
}
const backIndex = computed(() => {
  for (let index = trailIndex.value - 1; index >= 0; index--) {
    if (!trail.value[index] || folders.value.items.includes(trail.value[index])) return index
  }
  return -1
})
const forwardIndex = computed(() => trail.value.findIndex((path, index) => index > trailIndex.value && (!path || folders.value.items.includes(path))))
function move(index: number) {
  if (index < 0) return
  trailIndex.value = index
  enter(trail.value[index], false)
}
async function editPath() {
  pathText.value = '/' + folder.value
  pathError.value = false
  editingPath.value = true
  await nextTick()
  pathInput.value?.select()
}
async function closePath(restoreFocus = false) {
  editingPath.value = false
  if (restoreFocus) {
    await nextTick()
    pathEdit.value?.focus()
  }
}
function submitPath() {
  const path = pathText.value.replaceAll('\\', '/').replace(/^\/+|\/+$/g, '')
  pathError.value = !enter(path)
  if (!pathError.value) void closePath(true)
}
onMounted(load); onBeforeUnmount(()=>{disposed=true;loadSequence++;controller?.abort()})
</script>
<template>
  <section class="card" id="attachments-editor">
    <div class="cardhead"><h2>资料文件</h2><span class="sub">共 {{ files.length }} 个文件</span></div>
    <p v-if="folders.quota" class="sub">{{ quotaSummary(folders.quota) }}</p>
    <div class="file-command-bar">
      <button type="button" class="button small" @click="createFolder"><AppIcon name="newFolder"/>新建文件夹</button>
      <button type="button" class="button small" data-guide-target="files" :disabled="busy" @click="fileInput?.click()"><AppIcon name="upload"/>上传文件</button>
      <button type="button" class="button small" :disabled="busy" @click="folderInput?.click()"><AppIcon name="folderUpload"/>上传文件夹</button>
      <input ref="fileInput" type="file" multiple :disabled="busy" @change="upload" hidden>
      <input ref="folderInput" type="file" webkitdirectory multiple :disabled="busy" @change="upload" hidden>
      <button v-if="busy" type="button" class="button small" @click="controller?.abort()">取消上传</button>
      <button v-if="canIndex" type="button" class="button small" @click="syncIndex()"><AppIcon name="sync"/>同步索引</button>
      <button v-if="canIndex" type="button" class="button small" @click="syncIndex(true)"><AppIcon name="rebuild"/>重构索引</button>
      <button type="button" class="button small" :disabled="!chosen&&!folder" @click="rename"><AppIcon name="rename"/>重命名</button>
      <button v-for="action in (['public','private','delete'] as const)" :key="action" type="button" class="button small" :disabled="!selected.length" @click="batch(action)">{{ {public:'公开所选',private:'设为私密',delete:'删除所选'}[action] }}</button>
      <button v-if="selected.length" type="button" class="button small" @click="selected=[]">清空选择</button>
    </div>
    <p role="status" class="sub">{{ status }} {{ canIndex ? index : '' }}</p>
    <div class="file-browser-bar">
      <div class="file-location">
        <div class="file-navigation">
          <button type="button" aria-label="后退" title="后退" :disabled="backIndex < 0" @click="move(backIndex)"><AppIcon name="back"/></button>
          <button type="button" aria-label="前进" title="前进" :disabled="forwardIndex < 0" @click="move(forwardIndex)"><AppIcon name="forward"/></button>
          <button type="button" aria-label="上一级" title="上一级" :disabled="!folder" @click="enter(segments.at(-2)?.path || '')"><AppIcon name="up"/></button>
        </div>
        <div class="file-address">
          <AppIcon name="folder" :size="16"/>
          <input placeholder=" " class="form-control" v-if="editingPath" ref="pathInput" v-model="pathText" aria-label="文件路径" :aria-invalid="pathError" :aria-describedby="pathError ? 'file-path-error' : undefined" autocomplete="off" spellcheck="false" @input="pathError=false" @keydown.enter.prevent="submitPath" @keydown.esc.prevent="closePath(true)" @blur="closePath()">
          <template v-else>
            <nav ref="breadcrumb" class="file-address-segments" aria-label="文件夹路径">
              <template v-for="(segment, index) in segments" :key="segment.path">
                <AppIcon v-if="index" name="chevron" :size="12"/>
                <span v-if="index===segments.length-1" aria-current="location" :title="segment.label">{{ segment.label }}</span>
                <a v-else :href="router.resolve(folderLink(segment.path)).href" :title="segment.label" @click.prevent="enter(segment.path)">{{ segment.label }}</a>
              </template>
            </nav>
            <button ref="pathEdit" type="button" class="file-address-edit" aria-label="编辑文件路径" title="编辑文件路径" @click="editPath"><AppIcon name="rename" :size="14"/></button>
          </template>
        </div>
        <small v-if="editingPath && pathError" id="file-path-error" class="error-text" role="alert">目录不存在</small>
      </div>
    </div>
    <div class="material-workspace" @dragover.prevent @drop="drop">
      <div class="min-w-0">
        <div v-if="page.length" class="tablewrap">
          <table class="material-file-table">
            <thead><tr><th><input type="checkbox" data-material-select-page aria-label="选择本页" :checked="page.every(entry=>selected.includes(entry.kind==='folder'?'d:'+entry.path:'f:'+entry.file.id))" @change="togglePage">名称</th><th>可见性 / 状态</th><th>大小</th></tr></thead>
            <tbody><template v-for="entry in page" :key="entry.kind==='folder'?entry.path:entry.file.id">
              <tr v-if="entry.kind==='folder'" @contextmenu="openContext($event,'folder',entry.path)">
                <td><input v-model="selected" type="checkbox" data-material-pick :aria-label="'选择 '+entry.path" :value="'d:'+entry.path"><MaterialEntry folder :name="entry.path.split('/').at(-1) || entry.path" @click="enter(entry.path)"/></td>
                <td>文件夹</td><td>—</td>
              </tr>
              <tr v-else @contextmenu="openContext($event,'file',entry.file.id)">
                <td><input v-model="selected" type="checkbox" data-material-pick :aria-label="'选择 '+entry.file.filename" :value="'f:'+entry.file.id"><MaterialEntry :name="entry.file.filename" :path="entry.file.relative_path" :metadata="entry.file" :aria-pressed="chosen?.id===entry.file.id" @click="chooseFile(entry.file)" @dblclick="preview=entry.file"/></td>
                <td><span class="tag">{{ entry.file.visibility==='public'?'公开':'私密' }}</span><small v-if="canIndex && indexByFile.has(entry.file.id)" :title="indexByFile.get(entry.file.id)?.error">{{ indexLabels[indexByFile.get(entry.file.id)?.status||''] || indexByFile.get(entry.file.id)?.status }}<template v-if="indexByFile.get(entry.file.id)?.coverage"> · {{ indexByFile.get(entry.file.id)?.coverage }}</template></small></td>
                <td>{{ fileSizeMB(entry.file.size) }}</td>
              </tr>
            </template></tbody>
          </table>
        </div>
        <p v-else class="sub material-empty">{{ search ? '没有匹配的文件。' : '当前目录暂无文件。' }}</p>
        <RecordPagination v-model:offset="offset" :total="entries.length" :search="search" label="文件分页" unit="项" @update:search="search=$event;offset=0"/>
      </div>
      <aside class="material-inspector" :class="{'material-inspector-empty':!chosen&&!folder}"><template v-if="chosen"><h3>{{ chosen.filename }}</h3><label class="field">资料说明<textarea placeholder=" " class="form-control" v-model="chosen.caption" maxlength="2000" @input="rememberFile('caption')"/></label><label>可见性<select class="form-control" v-model="chosen.visibility" @change="rememberFile('visibility')"><option value="private">私密</option><option value="public">公开</option></select></label><div class="actions"><el-button :disabled="busy" @click="save">保存</el-button><el-button v-if="fileDrafts.has(chosen.id)" :disabled="busy" @click="discardFileDraft">放弃草稿并刷新</el-button><el-button @click="preview=chosen">预览</el-button><a :href="'/api/me/attachments/'+encodeURIComponent(chosen.id)">下载文件</a></div></template><template v-else-if="folder"><h3>{{ folder }}</h3><label class="field">文件夹说明<textarea placeholder=" " class="form-control" v-model="caption" maxlength="2000" @input="folderDrafts.set(folder,caption)"/></label><el-button @click="save">保存说明</el-button></template><p v-else class="sub">选择文件查看详情及设置。</p></aside></div></section><div v-if="context" class="card" :style="{position:'fixed',left:context.x+'px',top:context.y+'px',zIndex:2000}" @mouseleave="context=null"><div v-for="name in (['open','rename','public','private','delete'] as const)" :key="name"><el-button text @click="contextAction(name)">{{ {open:context.kind==='file'?'预览':'打开',rename:'重命名',public:'公开',private:'设为私密',delete:'删除'}[name] }}</el-button></div></div><FilePreview v-if="preview" :file="preview" :url="'/api/me/attachments/'+encodeURIComponent(preview.id)" @close="preview=null"/>
  <MaterialFileDialog v-if="renaming" rename-only :filename="renaming.filename" :disabled="busy" :save="saveName" @close="renaming=null"/>
</template>
