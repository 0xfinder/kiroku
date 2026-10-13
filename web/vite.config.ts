import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [
    sveltekit({
      adapter: adapter({
        pages: '../src/kiroku/web/static',
        assets: '../src/kiroku/web/static',
        fallback: 'index.html',
      }),
    }),
  ],
  server: { proxy: { '/api': 'http://127.0.0.1:8737' } },
  test: { include: ['src/**/*.test.ts'], maxWorkers: 1 },
});
