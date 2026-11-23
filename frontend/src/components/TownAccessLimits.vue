<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElButton } from 'element-plus'
import { notifyError, request } from '../api/client'

interface Limits {
  visitor_daily_limit: number | null
  total_daily_limit: number | null
}

const endpoint = '/me/town-access-limits'
const fields = [
  { key: 'visitor_daily_limit', label: '单访客日额度' },
  { key: 'total_daily_limit', label: '总日额度' },
] as const

const values = reactive({ visitor_daily_limit: '', total_daily_limit: '' })
const touched = reactive({ visitor_daily_limit: false, total_daily_limit: false })
const errors = computed(() => Object.fromEntries(fields.map(({ key }) => {
  const value = values[key].trim()
  const invalid = value && (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))
  return [key, invalid ? '请输入有效的非负整数，或留空。' : '']
})))
const form = ref<HTMLFormElement>()
const ready = ref(false)
const loading = ref(false)
const saving = ref(false)
const failed = ref(false)
const status = ref('')

function fill(limits: Limits) {
  for (const { key } of fields) {
    values[key] = limits[key] === null ? '' : String(limits[key])
    touched[key] = false
  }
}

async function load() {
  loading.value = true
  failed.value = false
  status.value = ''
  try {
    fill(await request<Limits>(endpoint))
    ready.value = true
  } catch (error) {
    failed.value = true
    notifyError(error)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!ready.value || saving.value) return
  status.value = ''
  for (const { key } of fields) touched[key] = true
  if (fields.some(({ key }) => errors.value[key])) {
    await nextTick()
    form.value?.querySelector<HTMLInputElement>('[aria-invalid="true"]')?.focus()
    return
  }
  const value = (key: keyof Limits) => values[key].trim() === '' ? null : Number(values[key])
  saving.value = true
  try {
    fill(await request<Limits>(endpoint, 'PUT', {
      visitor_daily_limit: value('visitor_daily_limit'),
      total_daily_limit: value('total_daily_limit'),
    }))
    status.value = '访问额度已保存。'
  } catch (error) {
    notifyError(error)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <section id="town-access-limits" class="card">
    <h2>小镇访问额度</h2>
    <p id="town-access-limits-help" class="sub">
      留空为不限，0 暂停公开问答。每次受理的新提问或追问计一次，每天北京时间 08:00 重置。
    </p>
    <form ref="form" @submit.prevent="save">
      <fieldset :disabled="!ready || saving">
        <div class="formgrid">
          <label v-for="field in fields" :key="field.key" class="field">
            {{ field.label }}
            <input
              v-model="values[field.key]" class="form-control" type="text" inputmode="numeric"
              :placeholder="ready ? '不限' : ' '" :aria-invalid="touched[field.key] && !!errors[field.key]"
              :aria-describedby="touched[field.key] && errors[field.key] ? field.key + '-error' : 'town-access-limits-help'"
              @blur="touched[field.key] = true" @input="status = ''"
            >
            <small v-if="touched[field.key] && errors[field.key]" :id="field.key + '-error'" role="alert">
              {{ errors[field.key] }}
            </small>
          </label>
        </div>
      </fieldset>
      <div class="actions model-save-actions">
        <ElButton v-if="failed" @click="load">重新读取额度</ElButton>
        <ElButton v-else type="primary" native-type="submit" :disabled="!ready" :loading="saving">
          保存访问额度
        </ElButton>
        <span class="sub model-saved-at" role="status">{{ loading ? '正在读取额度…' : status }}</span>
      </div>
    </form>
  </section>
</template>

