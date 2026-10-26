/// <reference types="vitest/config" />
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
  test: {
    // Pure helpers and server-rendered markup: no DOM environment needed
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
    // Vitest empties CSS modules by default; the token tests read tokens.css?raw
    css: { include: [/tokens\.css/] },
  },
})
