import { defineConfig } from '@playwright/test'
import { resolve } from 'node:path'
export default defineConfig({
  testDir: './tests/browser', workers: 1, timeout: 60_000,
  outputDir: resolve(process.env.UI_PARITY_OUTPUT || '../Agent_Operate_Guide/tmp/vue-postgres-migration/profile_frontend/playwright'),
  use: { baseURL: 'http://127.0.0.1:18081', headless: true },
  webServer: { command: '../.venv/bin/python ../scripts/run_tests.py browser-server', url: 'http://127.0.0.1:18081/api/health', timeout: 180_000, gracefulShutdown: { signal: 'SIGTERM', timeout: 15_000 }, reuseExistingServer: false },
})
