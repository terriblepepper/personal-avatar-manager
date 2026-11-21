<script setup lang="ts">
import AppIcon from './AppIcon.vue'
defineProps<{ allowSave?: boolean; saveDisabled?: boolean; saved?: boolean }>()
defineEmits<{ save: [] }>()
</script>

<template>
  <details class="mermaid-menu chat-session-menu">
    <summary class="button small icon-button" aria-label="更多绘图操作" title="更多绘图操作"><AppIcon name="more" :size="16"/></summary>
    <div class="mermaid-menu-items chat-session-actions">
      <slot/>
      <button v-if="allowSave" type="button" :disabled="saveDisabled || saved" @click="$emit('save')"><AppIcon name="save" :size="15"/>{{ saved ? '已转存' : '转存资料' }}</button>
      <slot name="extra"/>
    </div>
  </details>
</template>

<style scoped>
.mermaid-menu-items { font-size: .8125rem; }
.mermaid-menu-items button, .mermaid-menu-items :slotted(button), .mermaid-menu-items :slotted(a) { display:flex; align-items:center; gap:.5rem; width:100%; min-height:2rem; padding:.375rem .5rem; border:0; border-radius:.375rem; background:transparent; color:var(--text); font:inherit; text-align:start; text-decoration:none; cursor:pointer; }
.mermaid-menu-items button:enabled:hover, .mermaid-menu-items :slotted(button:enabled:hover), .mermaid-menu-items :slotted(a:hover) { background:var(--ui-hover); }
.mermaid-menu-items button:disabled, .mermaid-menu-items :slotted(button:disabled) { color:var(--muted); cursor:default; }
</style>
