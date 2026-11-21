<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppDialog from '../AppDialog.vue'
import RecordPagination from '../RecordPagination.vue'
import { request, notifyError } from '../../api/client'
import type { Page } from '../../types'
import { requestId } from '../../utils/requestId'

interface Category { id: string; name: string; active: boolean; available: number; total: number }
interface Source { source_url: string; source_id: string; original: string; license: string; language: string }
interface Question { id: string; category_id: string; category_name: string; prompt: string; active: boolean; source: Source }
interface QuestionForm extends Source { id: string; category: string; prompt: string; active: boolean }
type Dialog =
  | { kind: 'categories' }
  | { kind: 'category'; editing: boolean; form: Pick<Category, 'id' | 'name' | 'active'> }
  | { kind: 'question'; editing: boolean; form: QuestionForm }
  | { kind: 'import' }

const base = '/admin/soul-question-bank'
const page = ref<Page<Question> & { categories: Category[] }>()
const categories = computed(() => page.value?.categories ?? [])
const questionCount = computed(() => categories.value.reduce((sum, category) => sum + category.total, 0))
const category = ref(''), status = ref('all'), search = ref('')
const dialog = ref<Dialog | null>(null), busy = ref(false), error = ref('')
const title = computed(() => {
  switch (dialog.value?.kind) {
    case 'categories': return '题库类别'
    case 'category': return dialog.value.editing ? '编辑类别' : '新增类别'
    case 'question': return dialog.value.editing ? '编辑题目' : '新增题目'
    case 'import': return '导入已清洗题库'
    default: return ''
  }
})
let sequence = 0
async function load(offset = 0) {
  const ticket = ++sequence
  try {
    const query = new URLSearchParams({ category: category.value, status: status.value, search: search.value, offset: String(offset) })
    const result = await request<Page<Question> & { categories: Category[] }>(base + '?' + query)
    if (ticket === sequence) page.value = result
  } catch (e) { if (ticket === sequence) notifyError(e) }
}
function open(next: Dialog | null) {
  error.value = ''
  dialog.value = next
}
function editCategory(item?: Category) {
  open({ kind: 'category', editing: !!item,
    form: { id: item?.id || 'cat-' + requestId(), name: item?.name || '', active: !!item?.active } })
}
function editQuestion(item?: Question) {
  if (!categories.value.length) { ElMessage.info('请先添加一个类别'); return }
  open({ kind: 'question', editing: !!item, form: {
    id: item?.id || 'q-' + requestId(), category: item?.category_id || categories.value[0].id,
    prompt: item?.prompt || '', active: item ? !!item.active : true, source_url: item?.source?.source_url || '',
    source_id: item?.source?.source_id || '', original: item?.source?.original || '',
    license: item?.source?.license || '管理员原创', language: item?.source?.language || 'zh-CN',
  } })
}
async function submit(event: SubmitEvent) {
  const current = dialog.value
  if (!current || current.kind === 'categories' || busy.value) return
  const element = event.currentTarget as HTMLFormElement
  busy.value = true
  error.value = ''
  try {
    let message = '已保存'
    if (current.kind === 'import') {
      const file = element.querySelector<HTMLInputElement>('input[type=file]')?.files?.[0]
      if (!file) throw Error('请选择题库文件')
      if (file.size > 8 * 1024 * 1024) throw Error('题库文件最大 8 MiB')
      const payload: unknown = JSON.parse(await file.text())
      const result = await request<{ added: number; skipped: number }>(base + '/import', 'POST', payload)
      message = '已导入 ' + result.added + ' 道，跳过 ' + result.skipped + ' 道重复题'
    } else {
      const path = base + (current.kind === 'category' ? '/categories' : '/questions')
      await request(path + (current.editing ? '/' + encodeURIComponent(current.form.id) : ''), current.editing ? 'PUT' : 'POST', current.form)
    }
    await load(page.value?.offset)
    if (dialog.value === current) open(current.kind === 'category' ? { kind: 'categories' } : null)
    ElMessage.success(message)
  } catch (e) {
    if (dialog.value === current) error.value = e instanceof Error ? e.message : String(e)
  } finally { busy.value = false }
}
onMounted(() => load())
onBeforeUnmount(() => { sequence++ })
</script>

<template>
  <section id="soul-bank" class="card">
    <div class="cardhead">
      <h2>Soul 分类题库</h2>
      <div class="actions">
        <button type="button" class="button small" @click="open({ kind: 'categories' })">管理类别</button>
        <button type="button" class="button small" @click="editQuestion()">新增题目</button>
        <button type="button" class="button small" @click="open({ kind: 'import' })">导入题库</button>
      </div>
    </div>
    <p v-if="page" class="sub">{{ categories.length }} 个类别 · {{ questionCount }} 道题 · 用户按每个启用类别随机抽取 3 或 5 道，已开始的问答保留题目快照。</p>
    <div class="toolbar">
      <label class="field">类别<select class="form-control" v-model="category" @change="load()">
        <option value="">全部类别</option>
        <option v-for="c in categories" :key="c.id" :value="c.id">{{ c.name }} · {{ c.available }} 道</option>
      </select></label>
      <label class="field">题目状态<select class="form-control" v-model="status" @change="load()">
        <option value="all">全部</option><option value="active">已启用</option><option value="inactive">已停用</option>
      </select></label>
    </div>
    <div v-if="page?.items.length" class="tablewrap compact-record-table">
      <table>
        <thead><tr><th>题目</th><th>类别</th><th>状态</th><th>操作</th></tr></thead>
        <tbody><tr v-for="q in page.items" :key="q.id">
          <td class="bank-question">{{ q.prompt }}<small>{{ q.id }} · {{ q.source?.license || '管理员原创' }}</small></td>
          <td>{{ q.category_name }}</td><td><span class="tag">{{ q.active ? '启用' : '停用' }}</span></td>
          <td><button type="button" class="button small" @click="editQuestion(q)">编辑</button></td>
        </tr></tbody>
      </table>
    </div>
    <p v-else-if="page" class="sub">暂无匹配题目。</p>
    <RecordPagination v-if="page" :offset="page.offset" :total="page.total" :search="search" label="题库分页"
      @update:offset="load($event)" @update:search="search = $event; load()"/>
    <AppDialog :model-value="!!dialog" :title="title" width="min(40rem, calc(100vw - 2rem))" @update:model-value="open(null)">
      <template v-if="dialog?.kind === 'categories'">
        <div class="bank-categories">
          <div v-for="c in categories" :key="c.id" class="knowledge-asset">
            <strong>{{ c.name }}</strong><span class="sub">{{ c.available }}/{{ c.total }} 道 · {{ c.active ? '启用' : '停用' }}</span>
            <button type="button" class="button small" @click="editCategory(c)">编辑</button>
          </div>
        </div>
        <div class="actions"><button type="button" class="button small" @click="editCategory()">新增类别</button></div>
        <p class="sub">新类别先保存为停用状态；补足至少 5 道题后启用。</p>
      </template>
      <form v-else-if="dialog" :key="dialog.kind" class="bank-form" @submit.prevent="submit">
        <template v-if="dialog.kind === 'question'">
          <div class="formgrid"><label class="field">类别<select class="form-control" v-model="dialog.form.category" required>
            <option v-for="c in categories" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select></label></div>
          <label class="field">题目<textarea placeholder=" " class="form-control" v-model="dialog.form.prompt" minlength="12" maxlength="1200" required/></label>
          <label class="privacy"><input v-model="dialog.form.active" type="checkbox">启用题目</label>
          <details class="soul-evidence">
            <summary>来源与授权</summary>
            <label class="field">来源地址<input placeholder=" " class="form-control" v-model="dialog.form.source_url" type="url" maxlength="500"></label>
            <label class="field">授权说明<input placeholder=" " class="form-control" v-model="dialog.form.license" maxlength="100"></label>
            <p class="sub form-readout">{{ dialog.form.original }}</p>
          </details>
        </template>
        <template v-else-if="dialog.kind === 'category'">
          <label class="field">类别名称<input placeholder=" " class="form-control" v-model="dialog.form.name" maxlength="60" required></label>
          <label class="privacy"><input v-model="dialog.form.active" type="checkbox">启用类别（至少 5 道启用题目）</label>
        </template>
        <template v-else>
          <p class="sub">仅上传 version=1 的清洗后 JSON，最大 8 MiB；重复题目跳过，已有停用设置保留。平台不抓取网络内容。</p>
          <label class="field">题库文件<input type="file" accept=".json,application/json" required></label>
        </template>
        <div class="actions"><button class="button small primary" type="submit" :disabled="busy">
          {{ dialog.kind === 'question' ? '保存题目' : dialog.kind === 'category' ? '保存类别' : '导入' }}
        </button></div>
        <p class="error-text" role="alert">{{ error }}</p>
      </form>
    </AppDialog>
  </section>
</template>
