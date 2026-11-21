<script setup lang="ts">
import { ElDialog } from 'element-plus'
import WindowControls from './WindowControls.vue'
import WindowClose from './WindowClose.vue'

defineOptions({ inheritAttrs: false })
withDefaults(defineProps<{ modelValue: boolean; title?: string; subtitle?: string; closeLabel?: string; initialFullscreen?: boolean; anchorBottomRight?: boolean }>(), { closeLabel: '关闭' })
</script>

<template>
  <ElDialog append-to-body v-bind="$attrs" :model-value="modelValue" :title="title" :show-close="false" class="avatar-window app-dialog">
    <template #header="{ titleId, titleClass, close }">
      <div class="window-caption"><div class="window-caption-title">
        <span v-if="subtitle" class="eyebrow">{{ subtitle }}</span>
        <h2 :id="titleId" :class="titleClass">{{ title }}</h2>
      </div>
      <div v-if="modelValue" class="window-controls"><WindowControls :initially-fullscreen="initialFullscreen" :anchor-bottom-right="anchorBottomRight"/><WindowClose :label="closeLabel" @close="close"/></div></div>
    </template>
    <slot/>
    <template v-if="$slots.footer" #footer><slot name="footer"/></template>
  </ElDialog>
</template>
