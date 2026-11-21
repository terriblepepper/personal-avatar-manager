<script setup lang="ts">
import { useSessionStore } from '../stores/session'
import ModelUsage from '../components/models/ModelUsage.vue'
import ModelForm from '../components/models/ModelForm.vue'
const session=useSessionStore()
</script>
<template><ModelForm/><ModelForm v-if="session.user?.access_scope!=='profile'" embedding/><ModelUsage/></template>
