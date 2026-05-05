/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import path from "node:path";

export default defineConfig({
  plugins: [
    TanStackRouterVite({
      routesDirectory: "./src/app/routes",
      generatedRouteTree: "./src/app/routeTree.gen.ts",
      target: "react",
    }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    // Proxy `/api`, `/sse`, `/health`, `/ready` to the backend container so
    // that direct access to Vite (e.g. via `make dev-host` on a LAN IP) works
    // without going through Caddy. Inactive when accessed via Caddy because
    // Caddy already handles those paths before they reach Vite.
    proxy: {
      "/api": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/sse": {
        target: "http://backend:8000",
        changeOrigin: true,
        ws: true,
      },
      "/health": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/ready": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
    },
    watch: {
      usePolling: true, // Required for Docker volume mounts on some systems
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
  },
});
