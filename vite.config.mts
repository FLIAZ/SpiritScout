import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

export default defineConfig(({ command }) => ({
  root: resolve(import.meta.dirname, 'src/spiritscout/web/static'),
  plugins: [react(), {
    name: 'spiritscout-development-shell',
    transformIndexHtml: {
      order: 'pre',
      handler(html, context) {
        if (!context.server) return html;
        return html.replace('/static/app/session.js', `/@fs/${resolve(import.meta.dirname, 'frontend/session/main.tsx')}`)
          .replace(/<link rel="stylesheet" href="\/static\/app\/session\.css(?:\?[^"]*)?">/, '');
      },
    },
  }],
  base: command === 'serve' ? '/' : '/static/app/',
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  server: {
    host: '127.0.0.1',
    proxy: { '/api': 'http://127.0.0.1:8000', '/static': 'http://127.0.0.1:8000' },
  },
  build: {
    outDir: resolve(import.meta.dirname, 'src/spiritscout/web/static/app'),
    emptyOutDir: true,
    minify: true,
    lib: {
      entry: resolve(import.meta.dirname, 'frontend/session/main.tsx'),
      formats: ['es'],
      fileName: () => 'session.js',
      cssFileName: 'session',
    },
  },
}));
