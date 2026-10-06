import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import packageInfo from "./package.json";

const displayVersion = packageInfo.version
  .split(".")
  .map((part, index) => index === 0 || part.length > 1 ? part : `0${part}`)
  .join(".");

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(displayVersion)
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  },
  build: {
    outDir: "dist",
    emptyOutDir: true
  }
});

