import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const BACKEND = 'http://localhost:47950';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
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
      '/contracts': BACKEND,
      '/state': BACKEND,
      '/code': BACKEND,
    },
  },
});
