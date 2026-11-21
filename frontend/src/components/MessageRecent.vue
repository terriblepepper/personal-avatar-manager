<script setup lang="ts">
import { useRouter } from 'vue-router'
import MessagesView from '../views/MessagesView.vue'
withDefaults(defineProps<{to?:string}>(),{to:'/messages'})
const router = useRouter()
</script>
<template>
  <section class="card">
    <div class="cardhead"><h2>最近动态</h2><button type="button" class="button small" @click="router.push(to)">查看全部</button></div>
    <MessagesView :management="false"/>
  </section>
</template>
