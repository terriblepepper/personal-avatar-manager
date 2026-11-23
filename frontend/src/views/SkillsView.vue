<script setup lang="ts">
import { SKILLS_PAUSED, SKILL_PAUSE_NOTICE } from '../skillPolicy'
import { date } from '../utils/format'
import RecordPagination from '../components/RecordPagination.vue'
import { computed, onMounted, ref } from 'vue'
import { ElButton, ElInput, ElMessage, ElMessageBox } from 'element-plus'
import AppDialog from '../components/AppDialog.vue'
import MarkdownContent from '../components/MarkdownContent.vue'
import { request, notifyError } from '../api/client'
interface Skill { id:string; name:string; description:string; enabled:boolean; updated_at:string; manifest:{path:string;size:number}[] }
interface Detail extends Skill { instructions:string }
interface Result { items:Skill[]; installed_count?:number }
const search=ref(''), offset=ref(0)
const items=ref<Skill[]>([]), selected=ref<string[]>([]), file=ref<File|null>(null), url=ref(''), detail=ref<Detail|null>(null), busy=ref(false)
const matches=computed(()=>items.value.filter(s=>(s.name+' '+s.description).toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
const page=computed(()=>matches.value.slice(offset.value,offset.value+10))
function selectPage(){const ids=page.value.map(s=>s.id);if(ids.every(id=>selected.value.includes(id)))selected.value=selected.value.filter(id=>!ids.includes(id));else selected.value=[...new Set([...selected.value,...ids])]}
async function load(){try{items.value=await request<Skill[]>('/me/skills')}catch(e){notifyError(e)}}
async function execute(action:()=>Promise<Result>){busy.value=true;try{const result=await action();items.value=result.items;selected.value=[];ElMessage.success('操作完成');return true}catch(e){notifyError(e);return false}finally{busy.value=false}}
function chooseFile(event:Event){const target=event.target;if(target instanceof HTMLInputElement)file.value=target.files?.[0]??null}
async function upload(){if(!file.value||file.value.size>50*1024*1024){ElMessage.error('请选择不超过 50 MiB 的技能包');return}const form=new FormData();form.append('file',file.value);await execute(()=>request<Result>('/me/skills','POST',form))}
async function source(){if(await execute(()=>request<Result>('/me/skills/source','POST',{url:url.value})))url.value=''}
async function batch(action:'enable'|'disable'|'delete'){if(action==='delete'){try{await ElMessageBox.confirm('卸载选中技能及全部资源？','确认卸载')}catch{return}}await execute(()=>request<Result>('/me/skills/batch','POST',{ids:selected.value,action}))}
async function uninstall(id:string){try{await ElMessageBox.confirm('卸载后会删除该技能的说明和全部资源，是否继续？','确认卸载');await execute(()=>request<Result>('/me/skills/'+encodeURIComponent(id),'DELETE'))}catch(e){if(e!=='cancel'&&e!=='close')notifyError(e)}}
async function view(id:string){try{detail.value=await request<Detail>('/me/skills/'+encodeURIComponent(id))}catch(e){notifyError(e)}}
onMounted(load)
</script>
<template>
<section v-if="SKILLS_PAUSED" class="card paused-skills" aria-labelledby="paused-skills-heading">
  <h2 id="paused-skills-heading" data-guide-target="skills">分身技能</h2>
  <p class="sub">{{ SKILL_PAUSE_NOTICE }}</p>
  <article v-for="skill in page" :key="skill.id" class="skill-item">
    <strong>{{ skill.name }}</strong> <span class="tag">暂停使用</span>
    <p v-if="skill.description">{{ skill.description }}</p>
    <p class="sub">{{ skill.manifest.length }} 个文件 · {{ date(skill.updated_at) }}</p>
  </article>
  <p v-if="!matches.length" class="sub">没有匹配的技能包。</p>
  <RecordPagination :offset="offset" :total="matches.length" :search="search" label="技能分页"
    @update:offset="offset=$event" @update:search="search=$event;offset=0"/>
</section>
<template v-else><section class="card"><h2 data-guide-target="skills">安装技能包</h2><p>支持 Markdown、ZIP、TAR 和公共 GitHub 仓库，上传上限 50 MiB。</p><div class="actions"><input type="file" accept=".md,.markdown,.txt,.zip,.skill,.tar,.tgz,.gz,.bz2,.xz" @change="chooseFile"><el-button type="primary" :loading="busy" @click="upload">安装技能</el-button></div><form class="actions" @submit.prevent="source"><el-input v-model="url" type="url" placeholder="公共 GitHub 仓库或技能目录地址" required/><el-button native-type="submit" :loading="busy">从仓库安装</el-button></form><div class="skill-bulk"><label><input type="checkbox" :checked="page.length>0 && page.every(s=>selected.includes(s.id))" @change="selectPage"> 全选</label><span>已选 {{ selected.length }} 项</span><el-button v-for="action in (['enable','disable','delete'] as const)" :key="action" :disabled="!selected.length || busy" @click="batch(action)">{{ {enable:'批量启用',disable:'批量停用',delete:'批量卸载'}[action] }}</el-button></div><article v-for="skill in page" :key="skill.id" class="skill-item"><label><input v-model="selected" type="checkbox" :value="skill.id"> <strong>${{ skill.name }}</strong></label> <span class="tag">{{ skill.enabled?'已启用':'已停用' }}</span><MarkdownContent :source="skill.description" :diagrams="false"/><p class="sub">{{ skill.manifest.length }} 个文件 · {{ date(skill.updated_at) }}</p><div class="actions"><el-button @click="execute(()=>request<Result>('/me/skills/'+encodeURIComponent(skill.id),'PATCH',{enabled:!skill.enabled}))">{{ skill.enabled?'停用':'启用' }}</el-button><el-button @click="view(skill.id)">查看说明与文件</el-button><el-button @click="uninstall(skill.id)">卸载</el-button><el-button :disabled="!skill.enabled" @click="$router.push({path:'/chat',query:{skill:skill.name}})">在对话中使用</el-button></div></article><p v-if="!matches.length">没有匹配的技能包。</p><RecordPagination :offset="offset" :total="matches.length" :search="search" label="技能分页" @update:offset="offset=$event" @update:search="search=$event;offset=0"/></section><AppDialog :model-value="!!detail" title="技能详情" @close="detail=null"><template v-if="detail"><h3>{{ detail.name }}</h3><p>{{ detail.description }}</p><MarkdownContent :source="detail.instructions" :diagrams="false"/><ul class="skill-resources"><li v-for="resource in detail.manifest" :key="resource.path"><a class="button small" :href="'/api/me/skills/'+encodeURIComponent(detail.id)+'/files/'+resource.path.split('/').map(encodeURIComponent).join('/')">{{ resource.path }}</a></li></ul></template></AppDialog></template>
</template>

<style scoped>
.paused-skills, .paused-skills .skill-item { color: var(--muted); }
.skill-resources { display:grid; gap:.35rem; padding:0; list-style:none; }
.skill-resources .button { max-inline-size:100%; overflow-wrap:anywhere; text-align:start; }
</style>
