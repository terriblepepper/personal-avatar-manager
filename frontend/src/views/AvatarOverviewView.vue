<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request, notifyError } from '../api/client'
import ModelUsage from '../components/models/ModelUsage.vue'
import { date } from '../utils/format'
interface Agent { aic: string; identity_status: string; created_at?: string; last_modified_at?: string; updated_at?: string; profile: Record<string, string | undefined>; model_config?: Record<string, unknown>; acs_document?: Record<string, unknown>; certificate?: { mode?: string; status?: string; id?: string; expires_at?: string } }
interface Overview { agent: Agent | null }
const router = useRouter(), data = ref<Overview | null>(null)
const agent = computed(() => data.value?.agent || null)
const personType = computed(() => (({ student: '学生分身', teacher: '教师分身' }) as Record<string, string>)[agent.value?.profile?.person_type || 'student'] || '未设置')
const registrationMode = computed(() => (({ 'acps-direct': 'ACPS 独立实体', 'acps-entity': 'ACPS 本体派生实体', demo: '本地 demo（模拟 AIC 与证书）' }) as Record<string, string>)[agent.value?.certificate?.mode || ''] || '注册方式待确认')
async function load() { try { data.value = await request<Overview>('/me') } catch (e) { notifyError(e) } }
async function downloadACS() { try { const value = await request<{ document: unknown }>('/me/acs?source=approved'); const url = URL.createObjectURL(new Blob([JSON.stringify(value.document, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = 'avatar-acs.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) } catch (e) { notifyError(e) } }
onMounted(load)
</script>
<template>
  <template v-if="agent">
    <section class="card">
      <div class="avatarcard" data-guide-target="avatar-summary"><div class="avataricon">◈</div><div><span class="tag active">分身已就绪</span><h2 style="margin-top:12px">{{ agent.profile.avatar_name }}</h2><p class="sub">分身类型：{{ personType }}</p><span class="mono">{{ agent.aic }}</span></div></div>
      <dl class="kv"><dt>姓名</dt><dd>{{ agent.profile.name || '—' }}</dd><dt>分身名称</dt><dd>{{ agent.profile.avatar_name || '—' }}</dd><dt>个人简介</dt><dd>{{ agent.profile.bio || '—' }}</dd></dl>
      <div class="actions"><button type="button" class="button primary" @click="router.push('/chat')">与分身对话 ↗</button><button type="button" class="button" @click="router.push('/profile')">提交资料更新</button><button v-if="Object.keys(agent.acs_document || {}).length" type="button" class="button" @click="downloadACS">下载已审核 ACS</button></div>
    </section>
    <section class="card">
      <h2>身份与证书</h2>
      <dl class="kv"><dt>注册方式</dt><dd>{{ registrationMode }}</dd><dt>证书状态</dt><dd>{{ agent.certificate?.status || '待检查' }}</dd><dt>证书标识</dt><dd class="mono">{{ agent.certificate?.id }}</dd><dt>证书有效期</dt><dd>{{ date(agent.certificate?.expires_at) }}</dd><dt>创建时间</dt><dd>{{ date(agent.created_at) }}</dd><dt>上次修改于</dt><dd>{{ date(agent.last_modified_at) }}</dd><dt>资料生效于</dt><dd>{{ date(agent.updated_at) }}</dd></dl>
    </section>
    <ModelUsage/>
  </template>
  <section v-else class="card"><div class="empty" data-guide-target="avatar-summary"><span class="symbol">◇</span>你还没有分身，提交资料并通过审核后会自动创建</div><div class="actions"><button type="button" class="button primary" @click="router.push('/profile')">填写资料 →</button></div></section>
</template>
