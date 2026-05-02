import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Build output is placed inside the Python package so the wheel ships
// the dashboard as static assets served by FastAPI. The dev server
// proxies API calls to the local service on :8080 so we never need
// CORS in development.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: path.resolve(__dirname, "../src/mediarefinery/web"),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: false,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
