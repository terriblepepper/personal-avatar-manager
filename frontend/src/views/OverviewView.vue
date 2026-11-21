<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request } from '../api/client'
import { useSessionStore } from '../stores/session'
import { statusLabel } from '../utils/format'
import MessageRecent from '../components/MessageRecent.vue'
interface Overview { profile: { status: string; reason?: string; draft: { name?: string; avatar_name?: string }; registration?: { registration_phase?: string } | null }; agent: { aic: string; identity_status: string; profile: { name?: string; avatar_name?: string } } | null }
const session = useSessionStore(), router = useRouter(), data = ref<Overview | null>(null), error = ref('')
const controller = new AbortController()
const ready = computed(() => data.value?.agent?.identity_status === 'active' ? data.value.agent : null)
const status = computed(() => { const profile = data.value?.profile; if (!profile) return ''; return profile.status === 'creating' && profile.registration ? profile.registration.registration_phase || profile.status : profile.status })
const step = computed(() => ['draft', 'rejected'].includes(status.value) ? 1 : ['pending', 'updating'].includes(status.value) ? 2 : status.value === 'active' ? 4 : 3)
const name = computed(() => ready.value?.profile.name || data.value?.profile.draft.name || session.user?.identity_username || session.user?.username || '')
async function load() { try { data.value = await request<Overview>('/me', 'GET', undefined, controller.signal); error.value = '' } catch (e) { if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : '读取失败' } }
let timer: ReturnType<typeof setInterval>
onMounted(() => { void load(); timer = setInterval(() => { if (!document.hidden) void load() }, 8000) })
onUnmounted(() => { controller.abort(); clearInterval(timer) })
</script>
<template>
  <div class="heading"><div><h1>你好，{{ name }}</h1><p class="sub">你的数字分身旅程，从一份个人资料开始。</p></div></div>
  <p v-if="error" class="notice error" role="alert">{{ error }}</p>
  <template v-if="data">
    <section class="hero"><div><span class="eyebrow">YOUR DIGITAL IDENTITY</span><h2>{{ ready ? '另一个你，已经准备就绪' : '为自己创建一个数字分身' }}</h2><p>{{ ready ? '你的知识与经历，拥有了新的表达方式。可以与分身对话，也可以提交资料更新，让它持续成长。' : '填写个人资料，提交审核。审核通过后，平台审核通过后提交 Registry，Registry 审核通过再签证上线。' }}</p><button type="button" class="button primary" @click="router.push(ready ? '/chat' : '/profile')">{{ ready ? '与我的分身对话' : '开始填写个人资料' }} ↗</button></div><div class="hero-art"><div class="orb"></div></div></section>
    <div class="grid2">
      <section class="card">
        <div class="cardhead"><h2>我的申请进度</h2><span class="tag" :class="status">{{ statusLabel(status) }}</span></div>
        <div class="steps" data-guide-target="overview"><div v-for="(label, index) in ['填写资料', '管理员审核', '自动创建', '分身就绪']" :key="label" class="step" :class="{ current: index + 1 <= step }"><b>{{ '0' + (index + 1) }}</b>{{ label }}</div></div>
        <div v-if="data.profile.reason" class="notice error">处理说明：{{ data.profile.reason }}</div>
        <p class="sub">{{ ready ? '已生效身份：' : '你可以在个人资料页面保存草稿、提交申请和查看审核结果。' }}<span v-if="ready" class="mono">{{ ready.aic }}</span></p>
      </section>
      <MessageRecent/>
    </div>
  </template>
</template>
