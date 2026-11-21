<script setup lang="ts">
import { ref, useId } from 'vue'
import { ElInput } from 'element-plus'
import AppDialog from './AppDialog.vue'
import { notifyError } from '../api/client'
import type { FilePromotion } from '../types'
import { splitFilename } from '../utils/format'

const props = defineProps<{
  filename: string
  renameOnly?: boolean
  caption?: string
  folders?: string[]
  disabled?: boolean
  save: (value: FilePromotion) => Promise<void>
}>()
const emit = defineEmits<{ close: [] }>()
const { stem, suffix } = splitFilename(props.filename)
const name = ref(stem), caption = ref(props.caption || ''), folder = ref('')
const visibility = ref<'private'|'public'>('private')
const saving = ref(false), nameId = useId()
const filenameInput = ref<InstanceType<typeof ElInput>>()

function focusFilename() {
  requestAnimationFrame(() => {
    filenameInput.value?.focus()
    filenameInput.value?.select()
  })
}

function beforeClose(done: () => void) {
  if (!saving.value) done()
}

async function submit() {
  if (saving.value || props.disabled || !name.value.trim()) return
  saving.value = true
  try {
    await props.save({
      new_name: name.value.trim() + suffix,
      caption: caption.value,
      visibility: visibility.value,
      ...(props.folders ? { folder: folder.value } : {}),
    })
  } catch (error) { notifyError(error) }
  finally { saving.value = false }
}
</script>

<template>
  <AppDialog :model-value="true" :title="renameOnly ? '重命名文件' : '转存资料'" width="min(28rem, calc(100vw - 1rem))"
    :before-close="beforeClose" :close-on-click-modal="!saving" :close-on-press-escape="!saving" @open-auto-focus="focusFilename" @close="emit('close')">
    <form class="promotion-form" @submit.prevent="submit">
      <div class="field">
        <label :for="nameId">文件名</label>
        <ElInput ref="filenameInput" :id="nameId" v-model="name" :maxlength="200 - suffix.length" :disabled="saving">
          <template v-if="suffix" #append>{{ suffix }}</template>
        </ElInput>
      </div>
      <label v-if="!renameOnly" class="field">资料说明<ElInput v-model="caption" type="textarea" maxlength="2000" :disabled="saving"/></label>
      <template v-if="folders">
        <label class="field">保存到<select v-model="folder" class="form-control" :disabled="saving">
          <option value="">全部文件 /</option>
          <option v-for="path in folders" :key="path" :value="path">/{{ path }}</option>
        </select></label>
        <label class="field">可见性<select v-model="visibility" class="form-control" :disabled="saving">
          <option value="private">私密</option>
          <option value="public">公开</option>
        </select></label>
      </template>
      <div class="promotion-actions">
        <button type="button" class="button" :disabled="saving" @click="emit('close')">取消</button>
        <button type="submit" class="button primary" :disabled="disabled || saving || !name.trim()">{{ saving ? '正在保存…' : '确定' }}</button>
      </div>
    </form>
  </AppDialog>
</template>

<style scoped>
.promotion-form { display:grid; gap:.7rem; }
.promotion-actions { display:flex; justify-content:flex-end; gap:.5rem; }
</style>
