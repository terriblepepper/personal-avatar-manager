<script setup lang="ts">
import { RouterLink } from 'vue-router'
withDefaults(defineProps<{configured?:boolean;peerConfigured?:boolean;peerName?:string}>(), {configured:undefined,peerConfigured:undefined})
</script>
<template>
  <aside v-if="configured===false || peerConfigured===false" class="embedding-notice" role="status">
    <div>
      <strong>{{ configured===false && peerConfigured===false ? '双方均未配置嵌入模型' : configured===false ? '请配置嵌入模型' : (peerName || '对方分身')+'尚未配置嵌入模型' }}</strong>
      <span>{{ configured===false && peerConfigured===false ? '双方' : configured===false ? '本人' : '对方' }}仅能使用普通聊天，按已审核的填写资料回答。嵌入模型启用后可使用资料检索、Soul 和会话工具。</span>
    </div>
    <RouterLink v-if="configured===false" class="button small" to="/models">前往配置</RouterLink>
  </aside>
</template>
