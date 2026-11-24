import { createRouter, createWebHistory } from 'vue-router'
import { pages } from './pages'
const views = {
  profile: () => import('../views/ProfileView.vue'), files: () => import('../views/FilesView.vue'),
  models: () => import('../views/ModelsView.vue'), skills: () => import('../views/SkillsView.vue'),
  knowledge: () => import('../views/KnowledgeView.vue'), dialogues: () => import('../views/DialoguesView.vue'),
  chat: () => import('../views/ChatView.vue'), messages: () => import('../views/MessagesView.vue'),
  history: () => import('../views/HistoryView.vue'), account: () => import('../views/AccountView.vue'),
}
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('../views/OverviewView.vue') },
    { path: '/avatar', component: () => import('../views/AvatarOverviewView.vue') },
    ...pages.map(page => ({ path: '/' + page, component: views[page] })),
    { path: '/platform-entry', component: () => import('../views/OverviewView.vue') },
    { path: '/admin/:page?', component: () => import('../views/AdminView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
