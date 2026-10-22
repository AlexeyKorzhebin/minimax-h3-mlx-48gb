/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built master is served by studio-api under /studio (spec §4.4); in `npm run dev` the API
// calls go to a studio-api started with --fakes on :8766.
export default defineConfig({
  base: "/studio/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": "/src" } },
  server: { proxy: { "/api": "http://127.0.0.1:8766" } },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], include: ["src/**/*.test.{ts,tsx}"] },
});
