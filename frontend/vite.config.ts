import {defineConfig} from 'vite';

export default defineConfig({
  server: {host: '127.0.0.1', port: 5173, strictPort: true, proxy: {'/api': 'http://127.0.0.1:8002', '/_AMapService': 'http://127.0.0.1:8002', '/healthz': 'http://127.0.0.1:8002'}},
  build: {outDir: 'dist',rollupOptions:{output:{manualChunks:{maps:['leaflet','coordtransform'],react:['react','react-dom/client']}}}},
});

