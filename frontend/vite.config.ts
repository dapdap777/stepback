import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The production build is written into the Python package so it ships inside the wheel.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/stepback/server/static",
    emptyOutDir: true,
    // Everything is bundled: no CDN, no external requests at runtime.
    assetsInlineLimit: 0,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    strictPort: true,
    // `stepback ui --no-open` in another terminal. The Host header is forwarded unchanged
    // (no changeOrigin), so the API sees a same-origin request from localhost:5173.
    proxy: { "/api": { target: "http://127.0.0.1:7777", changeOrigin: false } },
  },
});
