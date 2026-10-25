import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Kept on 3000: the Netatmo OAuth redirect URI (backend/app/api/auth.py)
    // and FRONTEND_URL default point to http://localhost:3000
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
