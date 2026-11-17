import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The production bundle is written into the Python package so that the server can serve it.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/kaizeval/webui",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
