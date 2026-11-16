import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";

// The tests run against the real `stepback ui` server (FastAPI + the built bundle) on
// databases seeded by e2e/seed.py. Build first: `npm run build`.
//
// Two servers:
// - PORT:        read-only specs. No API key in its environment (exercises the "no key" path).
// - REPLAY_PORT: replay specs, on its own database (replays create forks). Its SDK talks to
//                e2e/fake_anthropic.py via ANTHROPIC_BASE_URL: no real API calls, no cost.
const PORT = Number(process.env.STEPBACK_E2E_PORT ?? 7799);
const REPLAY_PORT = PORT - 2;
const FAKE_API_PORT = PORT - 1;
const DB = join(tmpdir(), `stepback-e2e-${PORT}.db`);
const REPLAY_DB = join(tmpdir(), `stepback-e2e-replay-${REPLAY_PORT}.db`);

const serve = (db: string, port: number) =>
  `cd .. && uv run python frontend/e2e/seed.py "${db}" && uv run stepback --db "${db}" ui --no-open --port ${port}`;



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
  webServer: [
    {
      command: `cd .. && uv run python frontend/e2e/fake_anthropic.py ${FAKE_API_PORT}`,
      url: `http://127.0.0.1:${FAKE_API_PORT}/`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: serve(DB, PORT),
      url: `http://127.0.0.1:${PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      // No credentials at all: replay must explain how to set a key.
      env: { ANTHROPIC_API_KEY: "", ANTHROPIC_AUTH_TOKEN: "", ANTHROPIC_BASE_URL: `http://127.0.0.1:${FAKE_API_PORT}` },
    },
    {
      command: serve(REPLAY_DB, REPLAY_PORT),
      url: `http://127.0.0.1:${REPLAY_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ANTHROPIC_API_KEY: "sk-ant-e2e-fake-key-not-real", ANTHROPIC_AUTH_TOKEN: "", ANTHROPIC_BASE_URL: `http://127.0.0.1:${FAKE_API_PORT}` },
    },
  ],
});
