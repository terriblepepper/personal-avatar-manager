import { defineConfig, type Connect } from 'vite'
import { pages } from './src/router/pages'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

const consolePaths = new Set(['/', '/overview', '/avatar', '/platform-entry', '/admin', ...pages.map(page => '/' + page)])
const consoleHistory: Connect.NextHandleFunction = (request, _response, next) => {
  const url = new URL(request.url || '/', 'http://localhost')
  const path = url.pathname.replace(/\/+$/, '') || '/'
  if (['GET', 'HEAD'].includes(request.method || '') && (consolePaths.has(path) || /^\/admin\/[\w-]+$/.test(path))) {
    request.url = '/index.html' + url.search
  }
  next()
}

export default defineConfig({
  appType: 'mpa',
  plugins: [vue(), tailwindcss(), {
    name: 'console-history',
    configureServer(server) { server.middlewares.use(consoleHistory) },
    configurePreviewServer(server) { server.middlewares.use(consoleHistory) },
  }],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: process.env.AVATAR_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: false, ws: true },
    },
  },
  build: {
    rolldownOptions: {
      input: { app: 'index.html', renderer: 'static/preview/render.js' },
      // The opaque-origin frame loads a classic script in both Vite and FastAPI.
      output: { entryFileNames: chunk => chunk.name === 'renderer' ? 'static/preview/render.js' : 'assets/[name]-[hash].js' },
    },
  },
})
