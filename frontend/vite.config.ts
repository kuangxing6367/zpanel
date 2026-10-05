import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建产物直接落到 frontend/dist，由内核（core/api/static.py）托管。
// dev 模式下 /api 代理到内核 API，避免跨域配置。
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5273,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
