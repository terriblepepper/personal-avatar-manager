<script setup lang="ts">
import { computed } from 'vue'
import { iconSvg } from '../utils/icons'
const props = withDefaults(defineProps<{ name: string; size?: number }>(), { size: 18 })
const svg = computed(() => iconSvg(props.name, props.size))
</script>
<template><span class="app-icon" v-html="svg"/></template>
