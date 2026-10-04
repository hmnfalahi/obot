import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the cloudflared tunnel points at this server, which forwards
// /api to FastAPI so the mini app and its API share one HTTPS origin.
export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: ['.trycloudflare.com'],
    proxy: {
      '/api': process.env.OBOT_API_URL ?? 'http://localhost:8000',
    },
  },
})
