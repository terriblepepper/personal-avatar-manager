<script setup lang="ts">
import { computed, ref } from 'vue'
import TrendChart from './TrendChart.vue'
import { amount, bytes, latency, timestamp, type ComponentState, type WindowName } from './metrics'
import { useMonitor } from './useMonitor'

const { snapshot, period, phase, paused, loading, submitting, failure, refresh, login, logout } = useMonitor()
const username = ref('')
const password = ref('')
const direction = ref<'sent_bytes' | 'received_bytes'>('sent_bytes')
const windows: { value: WindowName; label: string }[] = [
  { value: '1h', label: '近 1 小时' }, { value: '24h', label: '近 24 小时' }, { value: '7d', label: '近 7 天' },
]
const queueLabels: Record<string, string> = {
  chat: '个人生成', peer: '分身协作', controls: '协作控制', index: '资料索引',
  soul: 'Soul 构建', provision: '分身注册', identity: '身份维护',
}
const timingLabels: Record<string, string> = {
  event_loop_lag: '事件循环', db_pool_acquire: '数据库连接池等待', db_write_lock: '数据库写锁等待',
  dispatch_poll: '事件转发表读取', document_image_process_start: '文档图像处理启动',
}
const stateText: Record<ComponentState, string> = { ready: '就绪', disabled: '未配置', unavailable: '不可用' }
const http = computed(() => snapshot.value?.http)
const services = computed(() => snapshot.value?.services)
const requests = computed(() => snapshot.value?.series.map(row => ({ ...row, value: row.requests })) ?? [])
const traffic = computed(() => snapshot.value?.series.map(row => ({ ...row, value: row[direction.value] })) ?? [])
const errors = computed(() => snapshot.value?.series.map(row => ({ ...row, value: row.server_errors })) ?? [])
const hitRate = computed(() => {
  const cache = services.value?.redis
  if (cache?.hits == null || cache.misses == null || cache.hits + cache.misses === 0) return '—'
  return amount(100 * cache.hits / (cache.hits + cache.misses)) + '%'
})
const delayed = computed(() => services.value && snapshot.value &&
  Date.parse(snapshot.value.now) - Date.parse(services.value.sampled_at) > 45000)

function submit() {
  if (submitting.value) return
  const secret = password.value
  password.value = ''
  void login(username.value, secret)
}
</script>

<template>
  <main v-if="phase !== 'monitor'" class="entry">
    <section class="panel login-panel" aria-labelledby="login-title">
      <a class="brand" href="/">镜像</a>
      <h1 id="login-title">运行监控</h1>
      <template v-if="phase === 'checking'">
        <p v-if="!failure" role="status">正在检查会话…</p>
        <p v-if="failure" class="alert" role="alert">{{ failure }}</p>
        <button v-if="failure" class="button primary" :disabled="loading" @click="refresh">重新检查</button>
      </template>
      <form v-else method="post" @submit.prevent="submit">
        <label for="username">本地管理员账号</label>
        <input id="username" v-model="username" name="username" autocomplete="username" required>
        <label for="password">密码</label>
        <input id="password" v-model="password" name="password" type="password"
               autocomplete="current-password" required>
        <p v-if="failure" class="alert" role="alert">{{ failure }}</p>
        <button class="button primary" type="submit" :disabled="submitting">{{ submitting ? '正在登录…' : '登录' }}</button>
      </form>
    </section>
  </main>
  <template v-else-if="snapshot && http">
    <header class="topbar">
      <div class="topbar-content">
        <a class="brand" href="/">镜像</a>
        <span class="topbar-section">运行监控</span>
        <button class="button subtle" :disabled="submitting" @click="logout">退出</button>
      </div>
    </header>
    <main class="dashboard">
      <div class="page-head">
        <div>
          <h1>运行监控</h1>
          <p class="muted">刷新于 {{ timestamp(snapshot.now) }}（北京时间）</p>
        </div>
        <div class="actions">
          <button class="button" :aria-pressed="paused" @click="paused = !paused">{{ paused ? '恢复自动刷新' : '暂停自动刷新' }}</button>
          <button class="button primary" :disabled="loading || submitting" @click="refresh">{{ loading ? '刷新中…' : '刷新' }}</button>
        </div>
      </div>
      <p v-if="failure" class="alert" role="alert">{{ failure }} 当前显示上次成功快照。</p>
      <section class="kpis" aria-label="最近五分钟 HTTP 汇总">
        <div class="panel kpi"><span>请求 / 秒</span><strong>{{ amount(http.requests / http.span_seconds) }}</strong><small>{{ amount(http.requests) }} 次请求 · 最近 {{ amount(http.span_seconds) }} 秒</small></div>
        <div class="panel kpi"><span>错误率 · 5xx</span><strong>{{ amount(http.requests ? 100 * http.server_errors / http.requests : 0) }}%</strong><small>{{ amount(http.server_errors) }} 次 5xx · {{ amount(http.client_errors) }} 次 4xx</small></div>
        <div class="panel kpi"><span>响应 P95</span><strong>{{ latency(http.p95_ms) }}</strong><small>首响应 P95 {{ latency(http.first_byte_p95_ms) }}</small></div>
        <div class="panel kpi"><span>HTTP 正文流量</span><strong>{{ bytes(http.sent_bytes) }}</strong><small>上行 {{ bytes(http.received_bytes) }} · 下行 {{ bytes(http.sent_bytes) }}</small></div>
      </section>
      <div class="summary-line">
        <span>当前并发 <b>{{ amount(http.in_flight) }}</b></span>
        <span>SSE 连接 <b>{{ amount(http.open_streams) }}</b></span>
        <span>P50 <b>{{ latency(http.p50_ms) }}</b></span>
        <span>P99 <b>{{ latency(http.p99_ms) }}</b></span>
        <span>中断 <b>{{ amount(http.interrupted) }}</b></span>
      </div>
      <div class="section-head">
        <h2>访问趋势</h2>
        <div class="segmented" role="group" aria-label="历史时间范围">
          <select v-model="direction" aria-label="流量方向">
            <option value="sent_bytes">下行流量</option>
            <option value="received_bytes">上行流量</option>
          </select>
          <button v-for="window in windows" :key="window.value" type="button"
                  :aria-pressed="period === window.value" @click="period = window.value">{{ window.label }}</button>
        </div>
      </div>
      <p v-if="!snapshot.history_ready" class="muted">历史采样仍在加载。</p>
      <div class="trends">
        <TrendChart title="请求量" unit="次" metric="requests" :rows="requests" :bucket-minutes="snapshot.bucket_minutes" :format="amount"/>
        <TrendChart :title="direction === 'sent_bytes' ? '下行流量' : '上行流量'" unit="HTTP 正文字节" :metric="direction" :rows="traffic" :bucket-minutes="snapshot.bucket_minutes" :format="bytes"/>
        <TrendChart title="服务错误" unit="次" metric="server_errors" :rows="errors" :bucket-minutes="snapshot.bucket_minutes" :format="amount"/>
      </div>
      <div class="section-head"><h2>服务与任务</h2><span v-if="services" class="muted">{{ delayed ? '采样已延迟 · ' : '' }}{{ timestamp(services.sampled_at) }}</span></div>
      <template v-if="services">
        <div class="services">
          <section class="panel service">
            <div class="section-title"><h3>应用进程</h3><span class="state" :data-state="services.process ? 'ready' : 'unavailable'">{{ services.process ? '就绪' : '不可用' }}</span></div>
            <dl><div><dt>CPU</dt><dd>{{ services.process ? amount(services.process.cpu_percent) + '%' : '—' }}</dd></div>
              <div><dt>内存 RSS</dt><dd>{{ bytes(services.process?.memory_bytes) }}</dd></div>
              <div><dt>线程</dt><dd>{{ amount(services.process?.threads) }}</dd></div></dl>
          </section>
          <section class="panel service">
            <div class="section-title"><h3>PostgreSQL</h3><span class="state" :data-state="services.postgres.state">{{ stateText[services.postgres.state] }}</span></div>
            <dl><div><dt>池可用 / 总数</dt><dd>{{ amount(services.postgres.pool.pool_available) }} / {{ amount(services.postgres.pool.pool_size) }}</dd></div>
              <div><dt>等待连接</dt><dd>{{ amount(services.postgres.pool.requests_waiting) }}</dd></div>
              <div><dt>锁等待 / 死锁累计</dt><dd>{{ amount(services.postgres.lock_waits) }} / {{ amount(services.postgres.deadlocks) }}</dd></div>
              <div><dt>数据库连接</dt><dd>{{ amount(services.postgres.connections) }}</dd></div></dl>
          </section>
          <section class="panel service">
            <div class="section-title"><h3>Redis</h3><span class="state" :data-state="services.redis.state">{{ stateText[services.redis.state] }}</span></div>
            <dl><div><dt>已用内存</dt><dd>{{ bytes(services.redis.memory_bytes) }}</dd></div>
              <div><dt>缓存命中率</dt><dd>{{ hitRate }}</dd></div>
              <div><dt>淘汰累计</dt><dd>{{ amount(services.redis.evicted) }}</dd></div></dl>
          </section>
          <section class="panel service">
            <div class="section-title"><h3>Kafka</h3><span class="state" :data-state="services.kafka.state">{{ stateText[services.kafka.state] }}</span></div>
            <dl><div><dt>事务转发积压</dt><dd>{{ amount(services.kafka.relay_pending) }}</dd></div></dl>
          </section>
        </div>
      </template>
      <p v-else class="panel empty">等待服务采样</p>
      <div class="tables">
        <section class="panel details">
          <h2>热门接口</h2>
          <table v-if="http.routes.length">
            <thead><tr><th scope="col">路由</th><th scope="col">请求</th><th scope="col">P95</th></tr></thead>
            <tbody><tr v-for="row in http.routes" :key="row.route">
              <td><code>{{ row.route }}</code></td><td>{{ amount(row.count) }}</td><td>{{ latency(row.p95_ms) }}</td>
            </tr></tbody>
          </table>
          <p v-else class="muted">等待接口访问</p>
        </section>
        <section class="panel details">
          <h2>任务队列</h2>
          <table v-if="services?.queues">
            <thead><tr><th scope="col">队列</th><th scope="col">排队</th><th scope="col">执行</th></tr></thead>
            <tbody><tr v-for="(job, key) in services.queues" :key="key">
              <td>{{ queueLabels[key] || key }}</td><td>{{ amount(job.queued) }}</td><td>{{ amount(job.running) }}</td>
            </tr></tbody>
          </table>
          <p v-else class="muted">暂无队列样本</p>
        </section>
        <section class="panel details">
          <h2>内部耗时</h2>
          <table v-if="Object.keys(snapshot.timings).length">
            <thead><tr><th scope="col">阶段</th><th scope="col">P95</th><th scope="col">P99</th></tr></thead>
            <tbody><tr v-for="(metric, key) in snapshot.timings" :key="key">
              <td>{{ timingLabels[key] || key }}</td><td>{{ latency(metric.p95_ms) }}</td><td>{{ latency(metric.p99_ms) }}</td>
            </tr></tbody>
          </table>
          <p v-else class="muted">暂无耗时样本</p>
        </section>
      </div>
      <footer>流量按应用收发的 HTTP 正文字节计量。SSE 连接数单独统计。趋势保留最近 7 天的完整分钟。</footer>
    </main>
  </template>
</template>
