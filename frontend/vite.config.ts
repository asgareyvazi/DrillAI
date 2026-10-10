import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// The dev server binds to 0.0.0.0 and proxies /api to the backend, so the browser only ever talks
// to its own origin. That matters for the sandboxed preview environment (and for any deployment
// where the API is not exposed on a second public host).
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: false,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: process.env.DRILLAI_API_URL ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: { host: '0.0.0.0', port: 4173, allowedHosts: true },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // Split the graph editor and the React runtime out of the main bundle: the workflow canvas
        // is the only screen that needs xyflow, and nobody should pay for it to read a cockpit.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          flow: ['@xyflow/react'],
        },
      },
    },
  },
})
