import { defineConfig } from "@playwright/test";

// Phone layout checks (spec §4.4: 390 px, the main button pinned to the bottom). They run against
// the BUILT master (`npm run build` first) served by `vite preview`; every API call is answered by
// the test itself (page.route with exact URLs) — no studio-api, no network.
export default defineConfig({
  testDir: "e2e",
  use: { baseURL: "http://127.0.0.1:4173", viewport: { width: 390, height: 844 } },
  webServer: { command: "npx vite preview --port 4173 --strictPort", url: "http://127.0.0.1:4173/studio/", reuseExistingServer: false },
});
