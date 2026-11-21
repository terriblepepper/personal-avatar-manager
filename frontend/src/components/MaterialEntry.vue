<script setup lang="ts">
import type { AnimationMetadata } from '../types'
import MaterialTypeIcon from './MaterialTypeIcon.vue'

defineProps<{ name: string; folder?: boolean; path?: string; metadata?: AnimationMetadata }>()
</script>

<template>
  <button type="button" class="material-entry" :title="path || name">
    <MaterialTypeIcon :folder="folder" :filename="name"/>
    <span><span class="file-list-name">{{ name }}</span><small v-if="path && path !== name">{{ path }}</small>
      <small v-if="metadata?.title">{{ [metadata.title, ...(metadata.tags || []), metadata.durationMs && metadata.durationMs / 1000 + ' 秒'].filter(Boolean).join(' · ') }}</small>
    </span>
  </button>
</template>
