<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import WorkspaceGuide from '../components/WorkspaceGuide.vue'
import { useWorkspaceGuide, workspaceDebugKey } from '../composables/useWorkspaceGuide'
import ThemePicker from '../components/ThemePicker.vue'
import { createNotificationInbox, notificationInboxKey } from '../composables/useNotificationInbox'
import NotificationBell from '../components/NotificationBell.vue'
import { request, notifyError } from '../api/client'
import { useSessionStore } from '../stores/session'

const FilePreview = defineAsyncComponent(() => import('../components/FilePreview.vue'))

provide(notificationInboxKey, createNotificationInbox())
const session = useSessionStore(), route = useRoute(), router = useRouter()
const mainContent = ref<HTMLElement>(), sidebar = ref<HTMLElement>(), navTrigger = ref<HTMLButtonElement>()
const mobile = ref(false), more = ref(false), help = ref(false)
const menuTrigger = ref<HTMLButtonElement>(), menuPanel = ref<HTMLElement>()
const navMedia = window.matchMedia?.('(max-width: 900px)')
const narrowNav = ref(navMedia?.matches ?? false)
const direction = ref(1)
const admin = computed(() => route.path.startsWith('/admin'))
const profileScope = computed(() => session.user?.access_scope === 'profile')
const hubPages = ['/avatar', '/files', '/knowledge', '/chat', '/models', '/skills', '/dialogues']
const inHub = computed(() => !admin.value && hubPages.includes(route.path))

interface NavItem { key: string; icon: string; label: string; path: string }
const nav = computed<NavItem[]>(() => {
  const iconFor: Record<string, string> = { overview: 'home', profile: 'person', models: 'agent', avatar: 'agent', agents: 'agent', users: 'people', migration: 'sync', jobs: 'sync', history: 'clock', question_bank: 'book', messages: 'mail', settings: 'settings' }
  const entries: [string, string][] = profileScope.value
    ? [['profile', '/profile'], ['models', '/models'], ['history', '/history']]
    : admin.value
      ? [['overview', '/admin'], ['users', '/admin/users'], ...(session.user?.is_root ? [['migration', '/admin/migration'] as [string, string]] : []), ['agents', '/admin/agents'], ['jobs', '/admin/jobs'], ['history', '/admin/history'], ['question_bank', '/admin/question_bank'], ['messages', '/admin/messages'], ['settings', '/admin/settings']]
      : [['overview', '/'], ['profile', '/profile'], ['avatar', '/avatar'], ['messages', '/messages'], ['history', '/history']]
  const texts: Record<string, string> = { overview: admin.value ? '管理概览' : '我的工作台', profile: '个人资料', avatar: '我的分身', models: '我的分身', messages: '最近动态', history: admin.value ? '审核与审计' : '申请记录', users: '账户管理', migration: '账号迁移', agents: '分身管理', jobs: '创建任务', question_bank: 'Soul 题库', settings: '平台设置' }
  return entries.map(([key, path]) => ({ key, path, icon: iconFor[key], label: texts[key] }))
})
const activeKey = computed(() => {
  if (admin.value) return route.path === '/admin' ? 'overview' : route.path.slice('/admin/'.length)
  if (profileScope.value) return hubPages.includes(route.path) ? 'models' : route.path.slice(1)
  if (route.path === '/') return 'overview'
  if (hubPages.includes(route.path)) return 'avatar'
  return route.path.slice(1)
})
const hubTabs = computed<[string, string][]>(() => profileScope.value
  ? [['files', '资料文件'], ['models', '模型配置']]
  : [['avatar', '分身概览'], ['files', '资料文件'], ['knowledge', 'Soul'], ['chat', '与分身对话'], ['models', '模型配置'], ['skills', '分身技能'], ['dialogues', '分身会话']])
const { phase, debug, busy, error: guideError, toggleDebug, replay, advance } = useWorkspaceGuide(admin)
provide(workspaceDebugKey, debug)
const guideMenu = ref(false), pageRevision = ref(0)
const viewKey = computed(() => route.path + ':' + pageRevision.value)
const drawerOpen = computed(() => guideMenu.value || (phase.value !== 'tour' && mobile.value))
const menuOpen = computed(() => guideMenu.value || (phase.value !== 'tour' && more.value))
function guideClosed() {
  guideMenu.value = false
  if (!phase.value) (narrowNav.value && !mobile.value ? navTrigger.value : menuTrigger.value)?.focus({ preventScroll: true })
}
const moduleHelp: Record<string,string> = {
  overview: '查看资料审核进度和分身是否上线。还没有分身时，可以从这里开始填写资料。',
  profile: '填写身份与基本信息，检查分身介绍。保存草稿可以稍后继续，提交后可在申请记录查看结果。',
  avatar: '这里汇集分身的日常操作。顶部标签可以切换概览、资料、Soul、模型和对话。',
  models: '在这里管理资料文件和模型配置。',
  messages: '查看审核、资料处理和分身活动的通知，了解最近发生了什么。',
  history: '查找已提交的申请、处理结果和退回原因，再决定是否修改资料。',
}
const tabHelp: Record<string,string> = {
  avatar: '查看分身身份、在线状态以及尚未就绪的原因。',
  files: '上传文件，按目录整理资料，检查索引进度，并选择哪些资料可以公开。',
  knowledge: '用自己的写作样本和问答构建 Soul，让分身保留你的措辞与表达习惯。',
  chat: '直接向自己的分身提问。需要它参考指定文件时，从资料库引用到本次对话。',
  models: '配置对话模型生成回答，配置嵌入模型用于资料检索。调用费用归对应配置的所有者。',
  skills: '管理分身可以使用的技能。在对话中引入所需技能后，可以用 $技能名称 调用。',
  dialogues: '查看与其他分身的消息，跟进需要确认、补充或结束的独立任务。',
}
const guideSteps = computed(() => [
  ...nav.value.flatMap(item => {
    const hub = item.key === 'avatar' || item.key === 'models'
    const main = { title: item.label, detail: moduleHelp[item.key], target: hub ? 'hub' : item.key, path: item.path }
    if (!hub) return [main]
    return [main, ...hubTabs.value.map(([key, title]) => ({
      title, detail: tabHelp[key], target: key === 'avatar' ? 'avatar-summary' : key, path: '/' + key,
    }))]
  }),
  { title: '账户与帮助', detail: '在这里打开帮助文档，按任务查找操作步骤。同一菜单还可以切换主题或退出登录。', target: 'help', path: profileScope.value ? '/profile' : '/' },
])
const runtimePill = computed(() => (({ demo: 'DEMO · 本地演示', 'acps-direct': 'ACPS · 独立实体模式', 'acps-entity': 'ACPS · 派生实体模式' }) as Record<string, string>)[session.authConfig?.runtime_mode || ''] || '服务已启动')
const notice = computed(() => {
  const user = session.user
  if (!user || admin.value || user.person_type !== 'teacher' || user.account_status === 'active') return null
  return user.account_status === 'pending'
    ? { error: false, text: '教师注册待管理员审核，可先填写资料；通过后方可提交分身申请。' }
    : { error: true, text: '教师注册被退回：' + (user.account_reason || '') }
})
async function resubmit() { try { await request('/me/account-resubmit', 'POST'); await session.refresh() } catch (e) { notifyError(e) } }
function closeMore(restoreFocus = false) {
  more.value = false
  if (restoreFocus) menuTrigger.value?.focus()
}
function closeHelp() {
  help.value = false
  void nextTick(() => menuTrigger.value?.focus())
}
async function toggleNavigation() {
  mobile.value = !mobile.value
  await nextTick()
  if (mobile.value) (sidebar.value?.querySelector<HTMLElement>('[aria-current="page"]') ?? sidebar.value?.querySelector<HTMLElement>('nav button'))?.focus()
  else navTrigger.value?.focus()
}
function go(path: string) {
  const from = hubPages.indexOf(route.path), to = hubPages.indexOf(path)
  direction.value = from >= 0 && to >= 0 && to < from ? -1 : 1
  const fromDrawer = mobile.value
  mobile.value = false
  closeMore(more.value)
  void router.push(path).then(() => { if (fromDrawer) mainContent.value?.focus() })
}
function menuItems() {
  return [...(menuPanel.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || [])]
    .filter(button => button.getClientRects().length > 0)
}
function menuKeys(event: KeyboardEvent) {
  if (event.defaultPrevented || phase.value) return
  if (event.key === 'Escape' && more.value) {
    event.preventDefault()
    closeMore(true)
    return
  }
  if (event.key === 'Escape' && mobile.value && sidebar.value?.contains(event.target as Node)) {
    event.preventDefault()
    void toggleNavigation()
    return
  }
  if (event.target === menuTrigger.value && event.key === 'ArrowDown') {
    event.preventDefault()
    more.value = true
    void nextTick(() => menuItems()[0]?.focus())
    return
  }
  if (!(event.target instanceof Node) || !menuPanel.value?.contains(event.target)) return
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  const items = menuItems()
  if (!items.length) return
  event.preventDefault()
  const current = items.findIndex(item => item === document.activeElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
    : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[next]?.focus()
}
const controller = new AbortController()
let polling = false
async function pollSidebar() {
  if (document.hidden || polling) return
  polling = true
  try {
    await session.refresh()
    const user = session.user
    if (!admin.value || user?.role !== 'admin' || user.access_scope === 'profile') return
    const { pending } = await request<{ pending: number }>('/admin/pending-count', 'GET', undefined, controller.signal)
    if (!controller.signal.aborted && admin.value && session.user?.id === user.id && session.user.role === 'admin') session.pending = pending
  } catch { /* 短暂断连时保留最后一次成功结果。 */ }
  finally { polling = false }
}
function dismissMore(event: MouseEvent) { if (more.value && event.target instanceof Element && !event.target.closest('.sidebar-menu-anchor')) closeMore() }
let timer: ReturnType<typeof setInterval>
onMounted(() => {
  navMedia?.addEventListener('change', event => {
    narrowNav.value = event.matches
    mobile.value = false
  }, { signal: controller.signal })
  void pollSidebar()
  timer = setInterval(pollSidebar, 8000)
  document.addEventListener('visibilitychange', pollSidebar, { signal: controller.signal })
  document.addEventListener('click', dismissMore)
  document.addEventListener('keydown', menuKeys)
})
watch(admin, () => { void pollSidebar() })
onUnmounted(() => { controller.abort(); clearInterval(timer); document.removeEventListener('click', dismissMore); document.removeEventListener('keydown', menuKeys) })
async function logout() { try { await session.logout() } catch (e) { notifyError(e) } }
</script>
<template>
  <div class="shell" :class="{ touring: phase === 'tour' }">
    <button type="button" class="button skip-navigation" @click="mainContent?.focus()">跳至主要内容</button>
    <aside ref="sidebar" id="console-sidebar" class="sidebar" :inert="narrowNav && !drawerOpen" :class="{ 'mobile-open': drawerOpen, 'guide-drawer': phase === 'tour' }">
      <div class="brand"><span class="mark"><AppIcon name="agent"/></span><div>镜像<small>个人分身工作台</small></div></div>
      <div class="sidebar-navigation">
        <div class="sidebar-account"><AppIcon name="person"/><div><strong>{{ session.user?.profile_name || session.user?.identity_username || session.user?.username }}</strong><small>{{ session.user?.is_root ? '根管理员' : session.user?.role === 'admin' ? '管理员' : '个人账户' }}</small></div></div>
        <p class="navgroup">{{ admin ? '管理' : '工作空间' }}</p>
        <nav class="nav" aria-label="主导航">
          <button v-for="item in nav" :key="item.key" type="button" :data-nav="item.key" :class="{ active: activeKey === item.key }" :aria-current="activeKey === item.key ? 'page' : undefined" @click="go(item.path)">
            <span class="icon"><AppIcon :name="item.icon"/></span>{{ item.label }}<span v-if="admin && item.key === 'history' && session.pending" class="badgecount">{{ session.pending }}</span>
          </button>
        </nav>
      </div>
      <div class="sidefoot">
        <div class="sidebar-menu-anchor">
          <button type="button" ref="menuTrigger" class="sidebar-more" :aria-expanded="menuOpen" aria-haspopup="menu" aria-controls="sidebar-extra-actions" aria-label="账户与帮助" title="更多操作" @click="more = !more">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2.3"/><circle cx="12" cy="12" r="2.3"/><circle cx="19" cy="12" r="2.3"/></svg>
          </button>
          <Transition name="avatar-pop"><div v-show="menuOpen" ref="menuPanel" :inert="!menuOpen" id="sidebar-extra-actions" class="sidebar-actions" role="menu" aria-label="账户与帮助">
            <button type="button" role="menuitem" class="sidebar-action sidebar-signout" @click="logout"><AppIcon name="forward"/><span>退出登录</span></button>
            <ThemePicker/>
            <button v-if="!profileScope" type="button" role="menuitem" class="sidebar-action" @click="go(admin ? '/admin/account' : '/account')"><AppIcon name="settings"/><span>账户设置</span></button>
            <button type="button" role="menuitem" class="sidebar-action" data-guide-target="help" @click="closeMore(true); help = true"><AppIcon name="book"/><span>帮助文档</span></button>
            <template v-if="!admin">
              <button type="button" role="menuitemcheckbox" :aria-checked="debug" class="sidebar-action" @click="toggleDebug"><AppIcon name="settings"/><span>Debug 模式</span><span class="debug-switch" aria-hidden="true"/></button>
              <template v-if="debug">
                <small class="guide-replay-note">导览会切换页面，请先保存草稿。</small>
                <button type="button" role="menuitem" class="sidebar-action" @click="closeMore(true); replay('welcome')"><AppIcon name="agent"/><span>重播首次介绍</span></button>
                <button type="button" role="menuitem" class="sidebar-action" @click="closeMore(true); replay('tour')"><AppIcon name="panel"/><span>重播模块导览</span></button>
              </template>
            </template>
            <button v-if="session.user?.role === 'admin' && !profileScope" type="button" role="menuitem" class="sidebar-action" @click="go(admin ? '/' : '/admin')"><AppIcon name="panel"/><span>{{ admin ? '个人中心' : '管理控制台' }}</span></button>
          </div></Transition>
        </div>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <button ref="navTrigger" type="button" class="button small mobile-nav" aria-controls="console-sidebar" :aria-expanded="mobile" :aria-label="mobile ? '收起导航' : '展开导航'" @click="toggleNavigation"><AppIcon name="panel" :size="18"/></button>
        <span>工作空间 / {{ admin ? '管理控制台' : '个人中心' }}</span>
        <span class="pill" data-runtime-mode><span class="dot"></span>{{ runtimePill }}</span>
      </header>
      <main :data-guide-page="route.path" ref="mainContent" class="content" tabindex="-1">
        <div v-if="notice" class="notice" :class="{ error: notice.error }">{{ notice.text }}<button v-if="notice.error" type="button" class="button small" @click="resubmit">重新提交注册审核</button></div>
        <div v-if="inHub" class="heading"><div><h1 data-guide-target="hub">我的分身</h1><p class="sub">集中管理分身、资料文件、知识库、模型、技能和任务；对话中可用 $技能名称 加可选参数调用技能。</p></div></div>
        <nav v-if="inHub" class="avatar-tabs" aria-label="我的分身功能">
          <button v-for="[key, label] in hubTabs" :key="key" :data-hub="key" type="button" :class="{ active: route.path === '/' + key }" :aria-current="route.path === '/' + key ? 'page' : undefined" @click="go('/' + key)">{{ label }}</button>
        </nav>
        <div v-if="inHub" class="avatar-panel" :style="{ '--avatar-view-offset': 24 * direction + 'px' }"><Transition name="avatar-view" appear><div :key="viewKey"><slot/></div></Transition></div>
        <Transition v-else name="avatar-view" appear><div :key="viewKey" class="workspace-page"><slot/></div></Transition>
        <footer class="footer"><span>镜像 · 个人分身智能体管理系统</span><span>本地演示</span></footer>
      </main>
    </div>
  </div>
  <Transition name="guide-dialog" appear @after-leave="guideClosed">
    <WorkspaceGuide v-if="phase && !admin" :phase="phase" :steps="guideSteps" :limited="profileScope" :busy="busy" :error="guideError"
    @menu="guideMenu = $event" @advance="advance" @reload-page="pageRevision++"/>
  </Transition>
  <NotificationBell v-if="!profileScope"/>
  <FilePreview v-if="help" :file="{ id: 'help', filename: '用户操作手册.md' }" url="/api/help/用户操作手册.md" @close="closeHelp"/>
</template>

<style scoped>
.sidebar.guide-drawer { transition:none; }
.touring :deep(.avatar-view-enter-active),.touring :deep(.sidebar-actions.avatar-pop-enter-active) { animation:none; }
@media (prefers-reduced-motion: no-preference) {
  .guide-dialog-enter-active,.guide-dialog-leave-active { transition:opacity 160ms ease; }
  .guide-dialog-enter-from,.guide-dialog-leave-to { opacity:0; }
  .guide-dialog-enter-active :deep(.guide-card),.guide-dialog-leave-active :deep(.guide-card) { transition:scale 160ms ease-out; }
  .guide-dialog-enter-from :deep(.guide-card),.guide-dialog-leave-to :deep(.guide-card) { scale:.98; }
}
.guide-replay-note { padding:4px 8px; color:var(--text-secondary); }
.debug-switch { margin-left:auto; width:28px; height:16px; padding:2px; border:1px solid var(--text-secondary); border-radius:9px; box-sizing:border-box; }
.debug-switch::after { content:''; display:block; width:10px; height:10px; border-radius:50%; background:var(--text-secondary); }
[aria-checked="true"] .debug-switch { background:var(--accent); border-color:var(--accent); }
[aria-checked="true"] .debug-switch::after { margin-left:auto; background:var(--on-accent); }
</style>
