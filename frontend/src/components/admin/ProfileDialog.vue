<script setup lang="ts">
import AppDialog from '../AppDialog.vue'

defineProps<{ modelValue: boolean; title: string; bodyLabel: string }>()
defineEmits<{ close: [] }>()
</script>

<template>
  <AppDialog :model-value="modelValue" :title="title" class="account-profile" align-center
    width="min(46rem, calc(100vw - 2rem))" @update:model-value="$emit('close')">
    <div class="account-profile-body" role="region" :aria-label="bodyLabel" tabindex="0"><slot/></div>
  </AppDialog>
</template>

<style>
.account-profile.el-dialog {
  display: flex; flex-direction: column; max-block-size: min(82dvh, 52rem); overflow: hidden;
  padding: 0; color: var(--text); font-family: inherit;
}
.account-profile.vue-window-fullscreen { max-block-size: calc(100dvh - 1rem); }
.account-profile.el-dialog > .el-dialog__header { flex: none; padding: 1rem 1.25rem .75rem; border-bottom: 1px solid var(--border); }
.account-profile > .el-dialog__body { display: flex; min-block-size: 0; flex: 1; color: var(--text); }
.account-profile-body { flex: 1; min-width: 0; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 1rem 1.25rem; scrollbar-gutter: stable; }
.account-profile-body section + section { margin-top: 1.25rem; padding-top: 1rem; border-top: 1px solid var(--border); }
.account-profile-body h3 { font: inherit; font-weight: 600; font-size: .94rem; margin: 0 0 .5rem; }
.account-profile-body .sub { margin: 0 0 .65rem; color: var(--muted); }
.account-profile-fields { display: grid; grid-template-columns: minmax(7rem, 25%) minmax(0, 1fr); align-items: start; gap: .75rem 1rem; margin: 0; font-size: var(--type-body); line-height: 1.6; }
.account-profile-fields dt { color: var(--text-secondary); }
.account-profile-fields dt small { display: block; color: var(--muted); font-size: var(--type-caption); }
.account-profile-fields dd { margin: 0; color: var(--text); white-space: pre-line; overflow-wrap: anywhere; }
.account-profile-fields .document-items, .account-profile-fields .document-fields { margin-block: 0; }
@media (max-width: 540px) {
  .account-profile-fields { grid-template-columns: 6.5rem minmax(0, 1fr); gap: .625rem; }
  .account-profile-body { padding: .875rem; }
}
</style>
