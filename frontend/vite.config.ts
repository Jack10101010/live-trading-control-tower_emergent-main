import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backendUrl = env.REACT_APP_BACKEND_URL || '';

  // Local dev: when REACT_APP_BACKEND_URL is unset the app fetches relative
  // `/api/*` paths. Proxy those to the local FastAPI backend so the frontend
  // and backend share an origin (no CORS, no frontend contract change).
  const localBackend = env.LOCAL_BACKEND_URL || 'http://localhost:8000';

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 3000,
      strictPort: true,
      proxy: backendUrl
        ? undefined
        : {
            '/api': {
              target: localBackend,
              changeOrigin: true,
            },
          },
    },
    preview: {
      host: '0.0.0.0',
      port: 3000,
    },
    define: {
      'import.meta.env.REACT_APP_BACKEND_URL': JSON.stringify(backendUrl),
      'process.env.REACT_APP_BACKEND_URL': JSON.stringify(backendUrl),
    },
  };
});
