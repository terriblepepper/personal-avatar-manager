<script setup lang="ts">
import { SKILL_PAUSE_NOTICE } from '../utils/skillPolicy'
import { date } from '../utils/format'
import RecordPagination from '../components/RecordPagination.vue'
import { computed, onMounted, ref } from 'vue'
import { request, notifyError } from '../api/client'
interface Skill { id:string; name:string; description:string; enabled:boolean; updated_at:string; manifest:{path:string;size:number}[] }
const search=ref(''), offset=ref(0)
const items=ref<Skill[]>([])
const matches=computed(()=>items.value.filter(s=>(s.name+' '+s.description).toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
const page=computed(()=>matches.value.slice(offset.value,offset.value+10))
async function load(){try{items.value=await request<Skill[]>('/me/skills')}catch(e){notifyError(e)}}
onMounted(load)
</script>
<template>
<section class="card paused-skills" aria-labelledby="paused-skills-heading">
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
</template>

<style scoped>
.paused-skills, .paused-skills .skill-item { color: var(--muted); }
</style>
