import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dirname = path.dirname(fileURLToPath(import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(dirname, './src'),
    },
  },
  server: {
    proxy: {
      // The backend never takes a browser-facing CORS decision beyond '*' (see backend/app/main.py);
      // this proxy just keeps dev requests same-origin so cookies/redirects behave normally and the
      // API base URL doesn't need an env var during local development.
      //
      // Target is overridable via VITE_BACKEND_URL (e.g. `VITE_BACKEND_URL=http://localhost:8001
      // npm run dev`) for the case where something else on the machine already holds :8000.
      '/api': {
        target: process.env.VITE_BACKEND_URL ?? 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
