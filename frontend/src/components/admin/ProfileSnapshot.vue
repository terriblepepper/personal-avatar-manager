<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request, notifyError } from '../../api/client'
import AppIcon from '../AppIcon.vue'
import DocumentFields from '../DocumentFields.vue'
import AcsDeployment from '../profile/AcsDeployment.vue'
interface Field {key:string;label:string;audience:string;visibility:string;fixed_public?:boolean}
const props=defineProps<{profile:unknown;acs?:unknown;model?:unknown;fieldsClass?:string}>()
function object(value:unknown):Record<string,unknown>{
  return value && typeof value==='object' && !Array.isArray(value) ? value as Record<string,unknown> : {}
}
const fields=ref<Field[]>([]), snapshot=computed(()=>object(props.profile)), config=computed(()=>object(props.model))
const visibility=computed(()=>object(snapshot.value.visibility))
const visible=computed(()=>fields.value.filter(field=>field.audience==='both'||field.audience===(snapshot.value.person_type||'student')))
const capabilities=computed(()=>{
  const document=object(props.acs)
  const keys=['name','version','description','iconUrl','documentationUrl','provider','capabilities','defaultInputModes','defaultOutputModes','skills']
  return Object.fromEntries(keys.filter(key=>document[key]!=null).map(key=>[key,document[key]]))
})
onMounted(async()=>{
  try {fields.value=await request<Field[]>('/profile-fields')} catch(error){notifyError(error)}
})
</script>
<template>
  <details class="compact-disclosure" open><summary><AppIcon name="chevron" :size="14"/>个人资料</summary>
    <dl v-if="visible.length" :class="fieldsClass || 'kv'">
      <template v-for="field in visible" :key="field.key">
        <dt>{{ field.label }} <small>{{ field.fixed_public?'公开（固定）':(visibility[field.key] || field.visibility)==='private'?'私密':'公开' }}</small></dt>
        <dd><DocumentFields v-if="snapshot[field.key]!=null && snapshot[field.key]!==''" :value="snapshot[field.key]"/><span v-else>未填写</span></dd>
      </template>
    </dl><DocumentFields v-else :value="snapshot"/>
  </details>
  <details v-if="Object.keys(object(acs)).length" class="compact-disclosure"><summary><AppIcon name="chevron" :size="14"/>ACS 能力描述</summary>
    <DocumentFields :value="capabilities"/><AcsDeployment :document="acs"/>
  </details>
  <section v-if="Object.keys(config).length"><h3>模型与额度</h3><dl :class="fieldsClass || 'kv'">
    <dt>模型平台</dt><dd>{{ config.provider || '未配置' }}</dd>
    <dt>模型名称</dt><dd>{{ config.model || '未配置' }}</dd>
    <dt>接口格式</dt><dd>{{ config.api_format || '—' }}</dd>
    <dt>API 地址</dt><dd>{{ config.base_url || '—' }}</dd>
    <dt>API Key</dt><dd>{{ config.key_configured?'已加密保存（不回显）':'未配置' }}</dd>
    <dt>单次输出上限</dt><dd>{{ config.max_tokens || '—' }} tokens</dd>
  </dl></section>
</template>
