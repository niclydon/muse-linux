import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The production build is served by the muse-linux backend itself
// (same origin), so the API base is relative. In dev, vite proxies
// /api to a locally running `muse-linux serve`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8766',
    },
  },
})
