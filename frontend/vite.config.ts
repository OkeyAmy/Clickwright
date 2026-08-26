import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The console talks to the FastAPI service; in dev we proxy so there is one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/connectors': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
});
