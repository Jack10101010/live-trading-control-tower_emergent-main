import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

/**
 * Minimal Vite-native test setup (UI-0). Deliberately small: jsdom + React
 * Testing Library, no browser E2E, no snapshot fixtures. Tests assert semantics
 * (provenance, no-silent-fallback, honest failure states), never pixels.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
});
