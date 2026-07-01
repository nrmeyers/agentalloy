import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const BACKEND = 'http://localhost:47950';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': BACKEND,
      '/telemetry': BACKEND,
      '/health': BACKEND,
      '/readiness': BACKEND,
      '/diagnostics': BACKEND,
      '/retrieve': BACKEND,
      '/compose': BACKEND,
      '/skills': BACKEND,
    },
  },
});
