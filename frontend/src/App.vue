<script setup lang="ts">
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElButton, ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useSessionStore } from './stores/session'
import LoginView from './views/LoginView.vue'
import ConsoleLayout from './layouts/ConsoleLayout.vue'
import { bindDisclosures } from './utils/disclosures'
import SelectMenu from './components/SelectMenu.vue'
import ThemePicker from './components/ThemePicker.vue'
const session = useSessionStore(), route = useRoute(), router = useRouter()
const platformToken = location.pathname === '/platform-entry' ? new URLSearchParams(location.hash.slice(1)).get('token') || '' : ''
if (platformToken) history.replaceState(null, '', '/platform-entry')
watch([() => session.user, () => route.path], ([user, path]) => {
  if (user?.access_scope === 'profile' && !['/profile','/files','/models','/history','/platform-entry'].includes(path)) void router.replace('/profile')
  else if (user && user.role !== 'admin' && path.startsWith('/admin')) void router.replace('/')
})
const events = new AbortController()
onMounted(() => { bindDisclosures(events.signal); void session.initialize() })
onBeforeUnmount(() => events.abort())
</script>
<template>
  <ElConfigProvider :locale="zhCn">
  <div v-if="!session.ready" class="loading" role="status">正在打开分身工作台…</div>
  <div v-else-if="session.failure" class="loading"><p role="alert">{{ session.failure }}</p><ElButton @click="session.initialize">重试加载</ElButton></div>
  <template v-else-if="!session.user || route.path === '/platform-entry'"><LoginView :platform-token="platformToken"/><div class="login-theme"><ThemePicker/></div></template>
  <ConsoleLayout v-else :key="[session.user.id, session.user.role, session.user.access_scope || 'full'].join(':')"><RouterView/></ConsoleLayout>
  <SelectMenu/>
  </ElConfigProvider>
</template>
