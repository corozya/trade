import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(() => {
  const backendPort = process.env.CRYPTO_BACKEND_PORT || '8423'

  return {
    plugins: [react()],
    server: {
      port: 5175,
      proxy: {
        '/api': `http://127.0.0.1:${backendPort}`,
      },
    },
  }
})
