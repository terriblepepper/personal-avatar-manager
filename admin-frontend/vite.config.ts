import { createRequire } from 'node:module'

const frontend = createRequire(new URL('../frontend/package.json', import.meta.url))
const vue = frontend('@vitejs/plugin-vue').default

export default {
  base: '/ops/',
  plugins: [vue()],
  resolve: { alias: { vue: frontend.resolve('vue/dist/vue.esm-bundler.js') } },
}
