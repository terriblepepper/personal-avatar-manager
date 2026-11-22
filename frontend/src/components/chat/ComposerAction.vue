<script setup lang="ts">
defineProps<{ label: string; stop?: boolean; resume?: boolean }>()
</script>

<template>
  <button type="button" class="button primary chat-command chat-send" :title="label">
    <span class="sr-only">{{ label }}</span>
    <span v-if="stop || resume" aria-hidden="true" :class="resume ? 'chat-resume-icon' : 'chat-pause-icon'"/>
    <svg v-else aria-hidden="true" width="18" height="18" viewBox="0 0 20 20" fill="none">
      <path d="M10 17V3m-6 6 6-6 6 6" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </button>
</template>
