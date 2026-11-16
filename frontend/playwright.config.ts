import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";

// The tests run against the real `stepback ui` server (FastAPI + the built bundle), on a
// database seeded by e2e/seed.py. Build first: `npm run build`.
const PORT = Number(process.env.STEPBACK_E2E_PORT ?? 7799);
const DB = process.env.STEPBACK_E2E_DB ?? join(tmpdir(), `stepback-e2e-${PORT}.db`);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "off",
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `cd .. && uv run python frontend/e2e/seed.py "${DB}" && uv run stepback --db "${DB}" ui --no-open --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "pipe",
  },
});
