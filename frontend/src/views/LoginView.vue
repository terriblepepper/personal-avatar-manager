<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton } from 'element-plus'
import AppIcon from '../components/AppIcon.vue'
import ThemePicker from '../components/ThemePicker.vue'
import { request } from '../api/client'
import { useSessionStore } from '../stores/session'
import type { SessionResponse } from '../types'
const props = defineProps<{ platformToken: string }>()
const session = useSessionStore(), route = useRoute(), router = useRouter()
const mode = ref<'login' | 'register' | 'recover'>('login'), busy = ref(false), error = ref(''), message = ref(''), platformAccount = ref('')
const local = ref(route.path.startsWith('/admin') || route.query.login === 'local')
const form = reactive({ username: local.value ? 'admin' : '', password: '', confirm_password: '', email: '', phone: '', person_type: 'student', student_id: '', name: '', contact: '', new_password: '' })
const title = computed(() => mode.value === 'register' ? '创建你的账户' : mode.value === 'recover' ? '找回密码' : '登录镜像')
const subtitle = computed(() => mode.value === 'register' ? '学号选填，教师注册需管理员审核。' : mode.value === 'recover' ? '填写注册邮箱或手机号，匹配后即可设置新密码。' : '支持用户名、注册邮箱或手机号登录。')
async function authenticated(result: SessionResponse) {
  session.accept(result)
  if (props.platformToken && result.user.access_scope !== 'profile') await request('/platform/bind', 'POST', { token: props.platformToken })
  await router.replace(result.user.access_scope === 'profile' ? '/profile' : result.user.role === 'admin' && route.path.startsWith('/admin') ? '/admin' : '/')
}
async function oidc() {
  busy.value = true; error.value = ''
  try { const value = await request<{ authorization_url: string }>('/auth/oidc/start', 'POST', { return_to: route.path.startsWith('/admin') ? '/admin' : '/', ...(props.platformToken ? { platform_entry_token: props.platformToken } : {}) }); location.assign(value.authorization_url) }
  catch (e) { error.value = e instanceof Error ? e.message : '登录暂不可用'; busy.value = false }
}
async function submit() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    if (mode.value === 'login') await authenticated(await request<SessionResponse>('/auth/login', 'POST', { username: form.username, password: form.password }))
    else if (mode.value === 'register') {
      const value = await request<{ message: string; user?: SessionResponse['user']; csrf?: string }>('/auth/register', 'POST', { username: form.username, password: form.password, email: form.email, phone: form.phone, person_type: form.person_type, student_id: form.person_type === 'student' ? form.student_id : '', name: form.name, ...(props.platformToken ? { platform_entry_token: props.platformToken } : {}) })
      if (value.user && value.csrf) await authenticated({ user: value.user, csrf: value.csrf })
      else { message.value = value.message; mode.value = 'login'; form.password = '' }
    } else {
      if (form.new_password !== form.confirm_password) throw Error('两次新密码输入不一致')
      const value = await request<{ message: string }>('/auth/recover-password', 'POST', { contact: form.contact, new_password: form.new_password })
      message.value = value.message; mode.value = 'login'; form.new_password = ''; form.confirm_password = ''
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '操作失败，请稍后重试' }
  finally { busy.value = false }
}
onMounted(async () => {
  const loginError = route.query.login_error
  if (typeof loginError === 'string') { error.value = loginError; await router.replace({ path: route.path, query: { ...route.query, login_error: undefined } }) }
  if (props.platformToken) {
    busy.value = true
    try {
      const context = await request<{ mode: string; account_id?: string }>('/platform/entry/preview', 'POST', { token: props.platformToken })
      if (context.mode === 'profile') { await authenticated(await request<SessionResponse>('/platform/entry/session', 'POST', { token: props.platformToken })); return }
      platformAccount.value = context.account_id || ''; mode.value = 'register'
    } catch (e) { error.value = e instanceof Error ? e.message : '临时入口不可用' }
    finally { busy.value = false }
  }
  if (session.authConfig?.oidc_enabled && !local.value && !error.value) await oidc()
})
</script>
<template>
  <div class="loginpage">
    <section class="loginintro">
      <div class="brand"><span class="mark"><AppIcon name="agent"/></span><div>镜像<small>个人分身工作台</small></div></div>
      <h1>个人分身工作台</h1>
      <p>管理个人资料、文件、Soul 与分身会话。</p>
      <div class="hero-art"><div class="orb"></div></div>
      <span v-if="session.authConfig?.version" class="pill">v{{ session.authConfig.version }}</span>
    </section>
    <section class="loginform">
      <div class="loginbox">
        <div class="eyebrow">WELCOME TO MIRROR</div>
        <h2>{{ title }}</h2>
        <p class="sub">{{ subtitle }}</p>
        <div v-if="platformToken && platformAccount" class="notice">北邮小镇叮当平台账号：{{ platformAccount }}<br>注册新账户即可绑定；已有账户请选择“登录并绑定”。</div>
        <template v-if="session.authConfig?.oidc_enabled && !local">
          <p class="sub">通过 Keycloak 统一认证登录。</p>
          <ElButton type="primary" :loading="busy" @click="oidc">前往 Keycloak 登录</ElButton>
          <div class="actions"><ElButton @click="local = true; error = ''">根管理员本地登录</ElButton></div>
        </template>
        <template v-else>
          <div class="tabs">
            <button type="button" :class="{ active: mode === 'login' }" @click="mode = 'login'; error = ''">{{ platformToken ? '登录并绑定' : '登录' }}</button>
            <button type="button" :class="{ active: mode === 'register' }" @click="mode = 'register'; error = ''">注册账户</button>
            <button type="button" :class="{ active: mode === 'recover' }" @click="mode = 'recover'; error = ''">找回密码</button>
          </div>
          <form @submit.prevent="submit">
            <template v-if="mode === 'register'">
              <label class="field">身份类型<select class="form-control" name="person_type" v-model="form.person_type"><option value="student">学生</option><option value="teacher">教师（管理员审核）</option></select></label>
              <label class="field">姓名<input placeholder=" " class="form-control" name="name" v-model="form.name" maxlength="80" :required="form.person_type === 'teacher'"></label>
              <label v-if="form.person_type !== 'teacher'" class="field">学号（选填）<input class="form-control" name="student_id" v-model="form.student_id" maxlength="80" placeholder="选填，不作为登录账号"></label>
              <label class="field">邮箱 *<input placeholder=" " class="form-control" name="email" v-model="form.email" type="email" required maxlength="160" autocomplete="email"></label>
              <label class="field">手机号（选填）<input placeholder=" " class="form-control" name="phone" v-model="form.phone" type="tel" maxlength="40" autocomplete="tel"></label>
            </template>
            <label v-if="mode !== 'recover'" class="field">{{ mode === 'register' ? '用户名' : '用户名 / 邮箱 / 手机号' }}<input placeholder=" " class="form-control" name="username" v-model="form.username" required minlength="3" :maxlength="mode === 'register' ? 40 : 160" :pattern="mode === 'register' ? '[a-zA-Z0-9_.-]+' : undefined" autocomplete="username"></label>
            <label v-if="mode === 'recover'" class="field">注册邮箱 / 手机号<input placeholder=" " class="form-control" name="contact" v-model="form.contact" required maxlength="160" autocomplete="username"></label>
            <label class="field">{{ mode === 'recover' ? '新密码' : '密码' }}<input class="form-control" v-if="mode === 'recover'" name="new_password" v-model="form.new_password" type="password" required minlength="8" maxlength="128" autocomplete="new-password" placeholder="至少 8 位字符"><input class="form-control" v-else name="password" v-model="form.password" type="password" required minlength="8" maxlength="128" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" placeholder="至少 8 位字符"></label>
            <label v-if="mode === 'recover'" class="field">确认新密码<input placeholder=" " class="form-control" name="confirm_password" v-model="form.confirm_password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
            <p v-if="message" class="sub" role="status">{{ message }}</p>
            <div class="error-text" role="alert">{{ error }}</div>
            <button class="button primary" type="submit" :disabled="busy">{{ mode === 'recover' ? '重置密码 →' : mode === 'register' ? '注册账户 →' : '登录工作台 →' }}</button>
          </form>
          <p class="sub" style="margin-top:24px">{{ mode === 'recover' ? 'DEMO 简化找回：仅匹配注册联系方式，未验证邮箱或手机持有人。' : '手机号选填。登录联系方式与资料公开性独立管理。' }}</p>
          <div v-if="session.authConfig?.oidc_enabled" class="actions"><ElButton @click="local = false; error = ''; form.username = ''">返回 Keycloak 登录</ElButton></div>
        </template>
      </div>
    </section>
  </div>
</template>
