import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

export default defineConfig({
  root: here,
  base: "/assets/",
  plugins: [react()],
  build: {
    outDir: resolve(repoRoot, "static", "chat"),
    emptyOutDir: true,
    assetsDir: "",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: resolve(here, "src/testSetup.ts"),
    include: ["src/**/*.test.{ts,tsx}"],
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:6322",
      "/ws": { target: "ws://127.0.0.1:6322", ws: true },
    },
  },
});
