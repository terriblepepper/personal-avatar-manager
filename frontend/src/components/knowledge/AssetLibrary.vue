<script setup lang="ts">
import { fileSizeMB, quotaSummary, type StorageQuota } from '../../utils/format'
import { SOUL_ACCEPT, supportsSoulAsset } from '../../utils/soulFormats'
import { computed, onMounted, ref, watch } from 'vue'
import { ElButton, ElCheckbox, ElInput, ElMessage, ElMessageBox } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import { request, notifyError, getCsrfToken } from '../../api/client'
import AppIcon from '../AppIcon.vue'
import MaterialTypeIcon from '../MaterialTypeIcon.vue'
import RecordPagination from '../RecordPagination.vue'
import FilePreview from '../FilePreview.vue'
import type { Asset } from './types'
const props=defineProps<{assets:Asset[];selected:string[];quota?:StorageQuota}>()
const emit=defineEmits<{refresh:[]; 'update:selected':[value:string[]]}>()
const folder=ref(''), folders=ref<string[]>([]), captions=ref<Record<string,string>>({}), caption=ref(''), selectedFiles=ref<string[]>([]), selectedFolders=ref<string[]>([]), search=ref(''), inspected=ref<Asset|null>(null), preview=ref<Asset|null>(null), importOpen=ref(false), materials=ref<Asset[]>([]), imports=ref<string[]>([]), busy=ref(false), offset=ref(0)
const visible=computed(()=>props.assets.filter(a=> search.value ? `${a.relative_path||a.filename} ${a.caption}`.toLowerCase().includes(search.value.toLowerCase()) : (a.relative_path||a.filename).split('/').slice(0,-1).join('/')===folder.value))
const children=computed(()=>search.value ? [] : folders.value.filter(p=>p.split('/').slice(0,-1).join('/')===folder.value))
const entries=computed(()=>[
  ...children.value.map(path=>({kind:'folder' as const,path})),
  ...visible.value.map(asset=>({kind:'file' as const,asset})),
])
const page=computed(()=>entries.value.slice(offset.value,offset.value+10))
watch(folder,()=>{caption.value=captions.value[folder.value]||'';offset.value=0})
watch(()=>entries.value.length,total=>{offset.value=Math.min(offset.value,Math.max(0,Math.ceil(total/10)-1)*10)})
function openFolder(path: string) { folder.value=path; search.value=''; offset.value=0 }
async function loadFolders(){try{const value=await request<{items:string[];captions:Record<string,string>}>('/me/knowledge/folders');folders.value=value.items;captions.value=value.captions||{};caption.value=captions.value[folder.value]||''}catch(e){notifyError(e)}}
async function act(path:string,method:string,body:unknown){if(busy.value)return false;busy.value=true;try{await request(path,method,body);await loadFolders();emit('refresh');return true}catch(e){notifyError(e);return false}finally{busy.value=false}}
async function upload(event:Event){
  const input=event.target as HTMLInputElement, files=Array.from(input.files||[])
  if(!files.length)return
  if (!files.some(file => supportsSoulAsset(file.name))) {
    input.value = ''
    ElMessage.error('请选择图片或 MD、TXT、DOC、DOCX 文档')
    return
  }
  const batch=Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('')
  let skipped=0, completed=0; busy.value=true
  try {
    for(const file of files){
      if (!supportsSoulAsset(file.name)) { skipped++; continue }
      const form=new FormData();form.append('file',file);form.append('relative_path',[folder.value,file.webkitRelativePath||file.name].filter(Boolean).join('/'))
      const response=await fetch('/api/me/knowledge/assets',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':getCsrfToken(),'X-Upload-Batch':batch},body:form})
      if(!response.ok){const error:unknown=await response.json().catch(()=>null);throw Error(error && typeof error==='object' && 'detail' in error && typeof error.detail==='string'?error.detail:'附件上传失败')}
      completed++
    }
  } catch(e){notifyError(e)} finally {
    try{await request(`/me/upload-batches/${batch}/complete`,'POST',{kind:'soul',selected:files.length,skipped})}catch(e){notifyError(e)}
    await loadFolders();emit('refresh');busy.value=false;input.value='';if(completed)ElMessage.success(skipped ? `已上传 ${completed} 个文件，跳过 ${skipped} 个不支持的文件` : `已上传 ${completed} 个文件`)
  }
}
async function saveCaption(){if(inspected.value && await act(`/me/knowledge/assets/${encodeURIComponent(inspected.value.id)}`,'PATCH',{caption:inspected.value.caption}))inspected.value=null}
async function requestName(title:string, initial=''){
  try {
    const {value}=await ElMessageBox.prompt('名称',title,{inputValue:initial,inputPlaceholder:'请输入名称'})
    return value.trim()
  } catch(error) {
    if(error!=='cancel' && error!=='close')notifyError(error)
    return ''
  }
}
async function create(){const name=await requestName('新建文件夹');if(name)await act('/me/knowledge/folders','POST',{path:[folder.value,name].filter(Boolean).join('/')})}
async function rename(path:string,asset?:Asset){const name=await requestName('重命名',asset?.filename||path.split('/').at(-1));if(!name)return;if(await act(asset?`/me/knowledge/assets/${encodeURIComponent(asset.id)}/name`:'/me/knowledge/folders','PATCH',asset?{new_name:name}:{path,new_name:name}) && !asset){const parent=path.split('/').slice(0,-1).join('/');folder.value=[parent,name].filter(Boolean).join('/')}}
const pageSelected = computed(()=>!!page.value.length && page.value.every(entry=>entry.kind==='file'
  ? selectedFiles.value.includes(entry.asset.id) : selectedFolders.value.includes(entry.path)))
function selectPage(){
  const files=page.value.flatMap(entry=>entry.kind==='file'?[entry.asset.id]:[])
  const directories=page.value.flatMap(entry=>entry.kind==='folder'?[entry.path]:[])
  if(pageSelected.value) {
    selectedFiles.value=selectedFiles.value.filter(id=>!files.includes(id))
    selectedFolders.value=selectedFolders.value.filter(path=>!directories.includes(path))
  } else {
    selectedFiles.value=[...new Set([...selectedFiles.value,...files])]
    selectedFolders.value=[...new Set([...selectedFolders.value,...directories])]
  }
}
async function remove(){if(!selectedFiles.value.length&&!selectedFolders.value.length)return;if(!confirm('删除所选文件或文件夹？已学到的风格保留。'))return;if(await act('/me/knowledge/assets/batch','POST',{action:'delete',ids:selectedFiles.value,folders:selectedFolders.value})){selectedFiles.value=[];selectedFolders.value=[];inspected.value=null}}
async function openImport(){try{const value=await request<Asset[]>('/me/attachments');materials.value=value.filter(item=>supportsSoulAsset(item.filename));imports.value=[];importOpen.value=true}catch(e){notifyError(e)}}
async function importFiles(){busy.value=true;let count=0;try{for(const id of imports.value){const item=materials.value.find(x=>x.id===id);const parent=(item?.relative_path||item?.filename||'').split('/').slice(0,-1).join('/');await request('/me/knowledge/assets/import','POST',{attachment_id:id,folder:[folder.value,parent].filter(Boolean).join('/')});count++}importOpen.value=false;ElMessage.success(`已导入 ${count} 份素材`)}catch(e){ElMessage.warning(`已导入 ${count} 份；后续导入未完成`);notifyError(e)}finally{if(count){await loadFolders();emit('refresh')}busy.value=false}}
onMounted(loadFolders)
</script>
<template>
  <section class="soul-library">
    <div class="cardhead"><h3>知识库素材</h3><span class="sub">共 {{ assets.length }} 个文件</span></div>
    <p class="sub">支持 PNG、JPEG、WebP、GIF、BMP 图片和 MD、TXT、DOC、DOCX 文档。资料文件引用不计入上传额度。<template v-if="quota">{{ quotaSummary(quota) }}。</template></p>
    <div class="file-command-bar">
      <button type="button" class="button small" :disabled="busy" @click="create"><AppIcon name="newFolder" :size="14"/>新建文件夹</button>
      <label class="button small" tabindex="0" @keydown.enter.prevent="($event.currentTarget as HTMLElement).querySelector('input')?.click()"><AppIcon name="upload" :size="14"/>上传文件<input type="file" multiple :accept="SOUL_ACCEPT" hidden :disabled="busy" @change="upload"></label>
      <label class="button small" tabindex="0" @keydown.enter.prevent="($event.currentTarget as HTMLElement).querySelector('input')?.click()"><AppIcon name="folderUpload" :size="14"/>上传文件夹<input type="file" multiple webkitdirectory hidden :disabled="busy" @change="upload"></label>
      <button type="button" class="button small" :disabled="busy" @click="openImport"><AppIcon name="import" :size="14"/>从资料文件导入</button>
      <button type="button" class="button small danger" :disabled="busy || (!selectedFiles.length&&!selectedFolders.length)" @click="remove">删除所选</button>
      <button v-if="selectedFiles.length || selectedFolders.length" type="button" class="button small" @click="selectedFiles=[];selectedFolders=[]">清空选择</button>
    </div>
    <div class="file-browser-bar">
      <div class="file-location">
        <div class="file-navigation"><button type="button" aria-label="上一级" title="上一级" :disabled="!folder" @click="openFolder(folder.split('/').slice(0,-1).join('/'))"><AppIcon name="up" :size="15"/></button></div>
        <nav class="file-address" aria-label="素材目录">
          <AppIcon name="folder" :size="16"/>
          <div class="file-address-segments">
            <button v-if="folder" type="button" class="soul-root-link" @click="openFolder('')">全部文件</button>
            <span v-else aria-current="location">全部文件</span>
            <template v-if="folder"><AppIcon name="chevron" :size="12"/><span aria-current="location" :title="folder">{{ folder }}</span></template>
          </div>
        </nav>
      </div>
    </div>
    <details v-if="folder" class="compact-disclosure soul-folder-details">
      <summary><AppIcon name="chevron" :size="14"/>文件夹说明</summary>
      <label class="field">说明<ElInput placeholder=" " v-model="caption" type="textarea" maxlength="2000" /></label>
      <div class="actions"><ElButton size="small" @click="act('/me/knowledge/folders','PATCH',{path:folder,caption})">保存说明</ElButton><ElButton size="small" @click="rename(folder)">重命名文件夹</ElButton></div>
    </details>
    <div v-if="page.length" class="tablewrap">
      <table class="material-file-table soul-asset-table">
        <thead><tr><th><input type="checkbox" data-material-select-page aria-label="选择本页素材" :checked="pageSelected" @change="selectPage">名称</th><th>用于构建</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="entry in page" :key="entry.kind==='file'?'f:'+entry.asset.id:'d:'+entry.path">
            <template v-if="entry.kind==='folder'">
              <td><input v-model="selectedFolders" type="checkbox" data-material-pick :value="entry.path" :aria-label="'选择 '+entry.path"><button type="button" class="material-entry" @click="openFolder(entry.path)"><MaterialTypeIcon folder/><span>{{ entry.path.split('/').at(-1) }}</span></button></td>
              <td>—</td><td><button type="button" class="button small" @click="rename(entry.path)">重命名</button></td>
            </template>
            <template v-else>
              <td><input v-model="selectedFiles" type="checkbox" data-material-pick :value="entry.asset.id" :aria-label="'选择 '+entry.asset.filename"><button type="button" class="material-entry" @click="inspected={...entry.asset}"><MaterialTypeIcon :filename="entry.asset.filename"/><span>{{ entry.asset.filename }}<small v-if="entry.asset.relative_path && entry.asset.relative_path!==entry.asset.filename">{{ entry.asset.relative_path }}</small></span></button></td>
              <td><input type="checkbox" :checked="selected.includes(entry.asset.id)" :disabled="!supportsSoulAsset(entry.asset.filename)" :aria-label="'用于构建 '+entry.asset.filename" @change="emit('update:selected',($event.target as HTMLInputElement).checked ? [...selected,entry.asset.id] : selected.filter(id=>id!==entry.asset.id))"></td>
              <td><div class="soul-asset-actions"><button type="button" class="button small" @click="preview=entry.asset">预览</button><a :href="entry.asset.download_url || `/api/me/knowledge/assets/${encodeURIComponent(entry.asset.id)}`" target="_blank" rel="noopener">下载</a><button type="button" class="button small" @click="rename('',entry.asset)">重命名</button></div></td>
            </template>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="sub">{{ search ? '没有匹配的素材。' : '当前目录暂无素材。' }}</p>
    <RecordPagination v-model:offset="offset" :total="entries.length" :search="search" unit="项" label="知识库素材分页" @update:search="search=$event;offset=0"/>
    <AppDialog :model-value="!!inspected" title="文件详情" @close="inspected=null"><template v-if="inspected"><p>{{ inspected.filename }} · {{ inspected.relative_path }} · {{ fileSizeMB(inspected.size) }} · {{ inspected.created_at }}</p><label>素材说明<ElInput placeholder=" " v-model="inspected.caption" type="textarea" maxlength="2000"/></label><ElButton @click="saveCaption">保存说明</ElButton></template></AppDialog><FilePreview v-if="preview" :file="{id:preview.id,filename:preview.filename,content_type:preview.mime}" :url="preview.download_url || `/api/me/knowledge/assets/${encodeURIComponent(preview.id)}`" @close="preview=null"/><AppDialog v-model="importOpen" title="从资料文件导入"><ElCheckbox v-for="item in materials" :key="item.id" :model-value="imports.includes(item.id)" @change="value => { imports=value ? [...imports,item.id] : imports.filter(id=>id!==item.id) }">{{ item.relative_path || item.filename }}</ElCheckbox><template #footer><ElButton :loading="busy" @click="importFiles">导入所选</ElButton></template></AppDialog></section></template>
