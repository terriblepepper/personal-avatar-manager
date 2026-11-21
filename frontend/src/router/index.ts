import { createRouter, createWebHistory } from 'vue-router'
import { pages } from './pages'
const views = {
  profile: () => import('../views/ProfileView.vue'), files: () => import('../views/FilesView.vue'),
  models: () => import('../views/ModelsView.vue'), skills: () => import('../views/SkillsView.vue'),
  knowledge: () => import('../views/KnowledgeView.vue'), dialogues: () => import('../views/DialoguesView.vue'),
  chat: () => import('../views/ChatView.vue'), messages: () => import('../views/MessagesView.vue'),
  history: () => import('../views/HistoryView.vue'), account: () => import('../views/AccountView.vue'),
}
const oldPage = location.hash.slice(1)
if (oldPage && !oldPage.startsWith('token=') && /^[a-z_]+$/.test(oldPage)) {
  const admin = location.pathname.startsWith('/admin')
  const mapped = oldPage === 'assistant' ? 'chat' : oldPage
  if (admin || pages.includes(mapped as typeof pages[number]) || ['overview','avatar'].includes(mapped)) {
    history.replaceState(null, '', (admin ? '/admin/' : '/') + (mapped === 'overview' ? '' : mapped) + location.search)
  }
}
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('../views/OverviewView.vue') },
    { path: '/overview', redirect: '/' }, { path: '/avatar', component: () => import('../views/AvatarOverviewView.vue') },
    ...pages.map(page => ({ path: '/' + page, component: views[page] })),
    { path: '/platform-entry', component: () => import('../views/OverviewView.vue') },
    { path: '/admin/:page?', component: () => import('../views/AdminView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
