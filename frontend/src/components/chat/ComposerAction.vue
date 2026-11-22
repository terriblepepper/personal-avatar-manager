<script setup lang="ts">
import AppIcon from '../AppIcon.vue'

defineProps<{ label: string; stop?: boolean; resume?: boolean }>()
</script>

<template>
  <button type="button" class="button primary chat-command chat-send" :title="label">
    <span class="sr-only">{{ label }}</span>
    <span v-if="stop || resume" aria-hidden="true" :class="resume ? 'chat-resume-icon' : 'chat-pause-icon'"/>
    <AppIcon v-else name="up" :size="21"/>
  </button>
</template>
