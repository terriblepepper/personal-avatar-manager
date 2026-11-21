<script setup lang="ts">
import { computed } from 'vue'
import AppIcon from '../AppIcon.vue'

const props = defineProps<{ document: unknown }>()
function fields(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function text(value: unknown) {
  return typeof value === 'string' && value ? value : '—'
}
function names(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item) : []
}
const doc = computed(() => fields(props.document))
const identity = computed(() => [
  ['运行环境', text(fields(doc.value.entityMeta).environment)],
  ['AIC', doc.value.aic ? text(doc.value.aic) : '正式注册后分配'],
  ['正式激活状态', doc.value.active === true ? '已激活' : '未正式激活'],
  ['协议版本', text(doc.value.protocolVersion)],
  ['实体用户 ID', text(doc.value.entityUserId)],
])
const endpoints = computed(() => {
  if (!Array.isArray(doc.value.endPoints)) return []
  return doc.value.endPoints.map(fields).filter(endpoint => typeof endpoint.url === 'string' && endpoint.url)
    .map(endpoint => {
      const schemes = Array.isArray(endpoint.security)
        ? [...new Set(endpoint.security.flatMap(value => Object.keys(fields(value))))]
        : []
      return { url: String(endpoint.url), transport: text(endpoint.transport),
        security: schemes.map(value => value === 'mtls' ? 'mTLS（双向 TLS）' : value).join('、') || '未声明' }
    })
})
const certificate = computed(() => fields(doc.value.certificate))
const altNames = computed(() => fields(certificate.value.altNames))
const hasCertificate = computed(() => Object.keys(certificate.value).length > 0)
</script>

<template>
  <details class="acs-deployment compact-disclosure">
    <summary><AppIcon name="chevron" :size="14"/>平台维护的部署与证书信息</summary>
    <div class="acs-deployment-content">
      <dl class="acs-deployment-fields">
        <template v-for="[label,value] in identity" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></template>
      </dl>
      <section>
        <h4>通信端点</h4>
        <p v-if="!endpoints.length" class="sub">尚未配置通信端点。</p>
        <dl v-for="(endpoint,index) in endpoints" :key="index" class="acs-deployment-fields acs-endpoint">
          <dt>地址</dt><dd>{{ endpoint.url }}</dd>
          <dt>传输方式</dt><dd>{{ endpoint.transport }}</dd>
          <dt>身份认证</dt><dd>{{ endpoint.security }}</dd>
        </dl>
      </section>
      <section>
        <h4>证书申请</h4>
        <p v-if="!hasCertificate" class="sub">暂无证书申请信息。</p>
        <dl v-else class="acs-deployment-fields">
          <dt>申请有效期</dt><dd>{{ typeof certificate.requestedValidity === 'number' ? certificate.requestedValidity + ' 天' : '—' }}</dd>
          <dt>DNS 备用名称</dt><dd>{{ names(altNames.dns).join('、') || '—' }}</dd>
          <dt>IP 备用名称</dt><dd>{{ names(altNames.ip).join('、') || '—' }}</dd>
        </dl>
        <p v-if="hasCertificate" class="sub">实际有效期由证书颁发机构决定。</p>
      </section>
    </div>
  </details>
</template>
