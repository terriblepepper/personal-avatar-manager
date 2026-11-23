<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import DocumentFields from '../DocumentFields.vue'
import ProfileDialog from './ProfileDialog.vue'
import RecordPagination from '../RecordPagination.vue'
import { notifyError, request } from '../../api/client'
import { useSessionStore } from '../../stores/session'
import type { Page, User } from '../../types'
import { date, statusLabel } from '../../utils/format'

interface Account extends User { name?: string; employee_id?: string }
interface Profile extends Record<string, unknown> { name?: string; person_type?: string; employee_id?: string; visibility?: Record<string, string> }
interface Detail { account: Account; profile: Profile; approved_at: string | null }
interface Field { key: string; label: string; audience: string; visibility: string; fixed_public?: boolean }

const session = useSessionStore()
const rows = ref<Account[]>([]), search = ref(''), role = ref('all'), offset = ref(0), total = ref(0)
const detail = ref<Detail | null>(null), fields = ref<Field[]>([]), loadingProfile = ref('')
const visibleFields = computed(() => fields.value.filter(field =>
  field.audience === 'both' || field.audience === (detail.value?.profile.person_type || 'student')))
let listRequest = 0, profileRequest = 0

async function load(next = 0) {
  const ticket = ++listRequest
  try {
    const query = new URLSearchParams({ search: search.value, role: role.value, offset: String(next) })
    const page = await request<Page<Account>>('/admin/accounts?' + query)
    if (ticket !== listRequest) return
    rows.value = page.items
    total.value = page.total
    offset.value = page.offset
  } catch (error) { if (ticket === listRequest) notifyError(error) }
}

async function profile(item: Account) {
  const ticket = ++profileRequest, actor = session.user?.id
  loadingProfile.value = item.id
  try {
    const data = await request<Detail>('/admin/accounts/' + encodeURIComponent(item.id) + '/profile')
    if (Object.keys(data.profile).length && !fields.value.length) fields.value = await request<Field[]>('/profile-fields')
    if (actor === session.user?.id && ticket === profileRequest) detail.value = data
  } catch (error) { if (ticket === profileRequest) notifyError(error) }
  finally { if (ticket === profileRequest) loadingProfile.value = '' }
}
function hasEntries(value: unknown) { return value !== null && typeof value === 'object' && !!Object.keys(value).length }
async function setRole(item: Account, value: 'user' | 'admin') {
  const message = value === 'user' ? '确认将自己降级为普通用户？管理权限会立即撤销。' :
    '确认将账户 ' + (item.identity_username || item.username) + ' 设为管理员？该账户将能审核申请、管理平台并授权其他用户。'
  if (!confirm(message)) return
  try {
    await request('/admin/accounts/' + encodeURIComponent(item.id) + '/role', 'PUT', { role: value })
    if (value === 'user') await session.refresh()
    else await load(offset.value)
  } catch (error) { notifyError(error) }
}
onMounted(() => load())
onBeforeUnmount(() => { listRequest++; profileRequest++ })
</script>

<template>
  <section class="card">
    <h2>账户管理</h2>
    <div class="toolbar">
      <select v-model="role" aria-label="账户角色" class="form-control compact-record-filter" @change="load()">
        <option value="all">全部角色</option><option value="user">普通用户</option><option value="admin">管理员</option>
      </select>
    </div>
    <p class="sub">共 {{ total }} 个账户。管理员可为已激活账户授予管理权限。</p>
    <div v-if="rows.length" class="tablewrap compact-record-table">
      <table>
        <thead><tr><th>姓名 / 用户名</th><th>身份 / 学号或工号</th><th>登记联系方式</th><th>角色 / 注册状态</th><th>操作</th></tr></thead>
        <tbody><tr v-for="item in rows" :key="item.id">
          <td><strong>{{ item.name || '未填写姓名' }}</strong><small>{{ item.identity_username || item.username }}</small></td>
          <td>{{ item.person_type === 'teacher' ? '教师' : '学生' }}<small>{{ (item.person_type === 'teacher' ? item.employee_id : item.student_id) || '未填写学号或工号' }}</small></td>
          <td>{{ item.email || '未绑定邮箱' }}<small>{{ item.phone || '未绑定手机号' }}</small></td>
          <td><span class="tag" :class="{ active: item.role === 'admin' }">{{ item.is_root ? '根管理员' : item.role === 'admin' ? '管理员' : '普通用户' }}</span>
            <small>{{ item.account_status === 'active' ? '已激活' : statusLabel(item.account_status) }}</small></td>
          <td><div class="admin-row-actions">
            <button v-if="item.account_status === 'active'" type="button" class="button small" :disabled="loadingProfile === item.id" @click="profile(item)">查看资料</button>
            <button v-if="item.role === 'user' && item.account_status === 'active'" type="button" class="button small" @click="setRole(item, 'admin')">设为管理员</button>
            <button v-else-if="item.role === 'admin' && !item.is_root && item.id === session.user?.id" type="button" class="button small danger" @click="setRole(item, 'user')">自己降级</button>
            <span v-else>{{ item.is_root ? '根管理员权限保留' : item.role === 'admin' ? '仅本人可降级' : '等待注册审核' }}</span>
          </div></td>
        </tr></tbody>
      </table>
    </div>
    <p v-else class="empty">暂无匹配账户</p>
    <RecordPagination :offset="offset" :total="total" :search="search" label="账户分页"
      @update:offset="load($event)" @update:search="search = $event; load()"/>
    <ProfileDialog :model-value="!!detail" :title="'用户资料 · ' + (detail?.profile.name || detail?.account.name || detail?.account.identity_username || detail?.account.username || '')" body-label="用户资料正文" @close="detail = null">
      <template v-if="detail">
        <section><h3>账户登记</h3>
        <dl class="account-profile-fields">
          <dt>姓名</dt><dd>{{ detail.profile.name || detail.account.name || '—' }}</dd>
          <dt>用户名</dt><dd>{{ detail.account.identity_username || detail.account.username }}</dd>
          <dt>注册身份</dt><dd>{{ detail.account.person_type === 'teacher' ? '教师' : '学生' }}</dd>
          <dt>学号 / 教师工号</dt><dd>{{ (detail.account.person_type === 'teacher' ? (detail.profile.employee_id || detail.account.employee_id) : (detail.account.student_id || detail.profile.student_id)) || '—' }}</dd>
          <dt>登记邮箱</dt><dd>{{ detail.account.email || '—' }}</dd>
          <dt>登记手机号</dt><dd>{{ detail.account.phone || '—' }}</dd>
        </dl></section>
        <section v-if="Object.keys(detail.profile).length">
          <h3>已审核通过的个人资料</h3>
          <p class="sub">审核时间：{{ date(detail.approved_at) }}</p>
          <dl class="account-profile-fields">
            <template v-for="field in visibleFields" :key="field.key">
              <dt :title="field.label">{{ field.label.split('（')[0] }} <small>{{ field.fixed_public ? '公开（固定）' : (detail.profile.visibility?.[field.key] || field.visibility) === 'private' ? '私密' : '公开' }}</small></dt>
              <dd><DocumentFields v-if="hasEntries(detail.profile[field.key])" :value="detail.profile[field.key]"/><template v-else>{{ typeof detail.profile[field.key] === 'object' ? '未填写' : detail.profile[field.key] || '未填写' }}</template></dd>
            </template>
          </dl>
        </section>
        <p v-else class="sub">注册已通过，暂无已审核通过的个人资料。</p>
      </template>
    </ProfileDialog>
  </section>
</template>
