<script setup lang="ts">
import AppIcon from '../AppIcon.vue'
</script>

<template>
  <div class="context-compacted" role="status">
    <AppIcon name="collapse-document" :size="15"/>上下文已压缩
  </div>
</template>

<style scoped>
.context-compacted { display:flex; align-items:center; gap:.4rem; align-self:stretch; padding:.3rem .45rem; margin-block:.15rem; color:var(--text-secondary); font-size:.8125rem; line-height:1.35; }
</style>
