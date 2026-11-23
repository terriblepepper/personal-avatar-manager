<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AdminOverview from './AdminOverview.vue'
import ReviewsPanel from '../components/admin/ReviewsPanel.vue'
import AccountsPanel from '../components/admin/AccountsPanel.vue'
import OperationsPanel from '../components/admin/OperationsPanel.vue'
import MigrationPanel from '../components/admin/MigrationPanel.vue'
import QuestionBank from '../components/admin/QuestionBank.vue'
import SettingsPanel from '../components/admin/SettingsPanel.vue'
import MessagesView from './MessagesView.vue'
import AccountView from './AccountView.vue'
const route=useRoute()
const page=computed(()=>typeof route.params.page==='string' && route.params.page ? route.params.page : 'overview')
const headings: Record<string, [string, string]> = {
  users: ['账户管理', '管理员可以授权普通账户，也可以降级自己的账户。'],
  migration: ['账号迁移', '核验 Keycloak 身份并关联原业务账号，保留原分身及历史。'],
  agents: ['分身管理', '查看分身生效资料、稳定身份和证书。'],
  jobs: ['创建任务', '查看创建与证书任务，立即检查等待中的请求，重试失败任务。'],
  history: ['审核与审计', '处理分身创建、资料更新和分身 Soul 审核。'],
  settings: ['平台设置', '查看管理系统与个人实体注册状态。'],
  question_bank: ['Soul 分类题库', '维护用于分身表达风格问答的分类题目。'],
}
const heading = computed(() => headings[['reviews','accounts'].includes(page.value) ? 'history' : page.value])
</script>
<template><div>
  <div v-if="heading" class="heading"><div><h1>{{ heading[0] }}</h1><p class="sub">{{ heading[1] }}</p></div></div>
  <AdminOverview v-if="page==='overview'"/><ReviewsPanel v-else-if="page==='reviews'||page==='history'||page==='accounts'" :key="page" :initial-category="page==='accounts'?'teacher':'all'"/><AccountsPanel v-else-if="page==='users'"/><MigrationPanel v-else-if="page==='migration'"/><QuestionBank v-else-if="page==='question_bank'"/><SettingsPanel v-else-if="page==='settings'"/><MessagesView v-else-if="page==='messages'"/><AccountView v-else-if="page==='account'"/><OperationsPanel v-else :key="page" :page="page"/></div></template>
