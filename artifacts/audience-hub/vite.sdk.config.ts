import { defineConfig } from 'vite';
import path from 'node:path';

export default defineConfig({
  publicDir: false,
  build: {
    outDir: path.resolve(import.meta.dirname, 'dist/sdk'),
    emptyOutDir: true,
    minify: 'esbuild',
    target: 'es2020',
    lib: {
      entry: path.resolve(import.meta.dirname, 'sdk/ah.ts'),
      name: 'AudienceHub',
      formats: ['iife'],
      fileName: () => 'ah.js',
    },
  },
});
