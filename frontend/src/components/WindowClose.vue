<script setup lang="ts">
import AppIcon from './AppIcon.vue'

withDefaults(defineProps<{ label?: string }>(), { label: '关闭' })
defineEmits<{ close: [] }>()
</script>

<template>
  <button type="button" class="window-caption-button ui-window-close" :aria-label="label" :title="label" @click="$emit('close')">
    <AppIcon name="close" :size="22"/>
  </button>
</template>
