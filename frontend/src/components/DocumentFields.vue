<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ value: unknown; depth?: number }>(), { depth: 0 })
const fields = computed(() => props.value && typeof props.value === 'object'
  ? Object.entries(props.value).filter(([, value]) => value !== null && value !== '' && value !== undefined) : [])
const labels: Record<string, string> = {
  degree: '学历', school: '学校', major: '专业', institution: '机构', role: '角色', start_date: '开始日期', end_date: '结束日期',
  id: '标识', name: '名称', avatar_name: '分身名称', description: '描述', version: '版本',
  skills: '技能', provider: '提供者', organization: '学校 / 组织', department: '部门',
  defaultInputModes: '默认输入格式', defaultOutputModes: '默认输出格式',
  default_input_modes: '默认输入格式', default_output_modes: '默认输出格式',
  input_modes: '输入格式', output_modes: '输出格式', inputModes: '输入格式', outputModes: '输出格式',
  icon_url: '图标地址', documentation_url: '文档地址', location: '地点', country_code: '国家或地区代码',
  email: '电子邮件', phone: '联系电话', tags: '标签', examples: '提问示例', bio: '简介',
  domain_registrations: '域名备案', domain: '域名', registration_number: '备案 / 注册号',
  registration_type: '注册类型', url: '网址', status: '状态', visibility: '可见性',
}
function label(key: string) { return Object.hasOwn(labels, key) ? labels[key] : key }
function scalar(value: unknown) {
  if (value === 'public') return '公开'
  if (value === 'private') return '私密'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return value === null || value === '' || value === undefined ? '—' : String(value)
}
</script>

<template>
  <template v-if="depth < 12">
    <ul v-if="Array.isArray(value) && value.length" class="document-items">
      <li v-for="(item,index) in value" :key="index"><DocumentFields :value="item" :depth="depth+1"/></li>
    </ul>
    <dl v-else-if="fields.length" class="document-fields">
      <template v-for="[key,item] in fields" :key="key">
        <dt>{{ label(key) }}</dt><dd><DocumentFields :value="item" :depth="depth+1"/></dd>
      </template>
    </dl>
    <span v-else class="document-value">{{ typeof value === 'object' ? '—' : scalar(value) }}</span>
  </template>
  <span v-else>…</span>
</template>
