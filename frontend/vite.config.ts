import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The API streams SSE; proxy it so the UI is same-origin in dev.
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true,
                       rewrite: (p) => p.replace(/^\/api/, "") } },
  },
});
