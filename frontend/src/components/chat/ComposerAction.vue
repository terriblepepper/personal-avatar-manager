<script setup lang="ts">
import AppIcon from '../AppIcon.vue'

defineProps<{ label: string; stop?: boolean }>()
</script>

<template>
  <button type="button" class="button primary chat-command chat-send" :title="label">
    <span class="sr-only">{{ label }}</span>
    <span v-if="stop" aria-hidden="true" class="chat-stop-icon"/>
    <AppIcon v-else name="up" :size="21"/>
  </button>
</template>
