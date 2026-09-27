import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end configuration.
 *
 * The E2E suite runs the *real* product: a real FastAPI service on a throwaway SQLite database seeded
 * with labelled synthetic fixtures, and the real Vite build of the client. Nothing is mocked — no
 * fake runs, no fake approvals, no canned engineering values — because the point of an end-to-end
 * test is to fail when the two halves of the system stop agreeing.
 *
 * Browser resolution is environment driven so the suite is reproducible anywhere:
 *
 * * default — Playwright's own Chromium (install with `npx playwright install chromium`);
 * * `DRILLAI_CHROMIUM_PATH` — an explicit executable (used in sandboxes that ship their own build);
 * * `DRILLAI_CHROMIUM_LIBS` — an extra library directory, e.g. NSS/NSPR for a stripped binary.
 *
 * Servers are started by Playwright itself (`webServer`), so `npx playwright test` is the only
 * command a developer or CI job needs: no manual clicking, no "start the API first" step.
 */

const API_PORT = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
const WEB_PORT = Number(process.env.DRILLAI_E2E_WEB_PORT ?? 5199)
const API_URL = `http://127.0.0.1:${API_PORT}`
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`

const chromiumPath = process.env.DRILLAI_CHROMIUM_PATH
const chromiumLibs = process.env.DRILLAI_CHROMIUM_LIBS

/** Flags needed when running a container-oriented Chromium build without a user namespace. */
const containerArgs = chromiumPath
  ? ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-setuid-sandbox']
  : []

export default defineConfig({
  testDir: './e2e',
  // The suite writes to a shared seeded database, so the files run in order: a fixture created by an
  // earlier journey is read back by a later one. Parallel workers would race on that state.
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  globalSetup: './e2e/global-setup.ts',
  use: {
    baseURL: WEB_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 20_000,
    launchOptions: {
      ...(chromiumPath ? { executablePath: chromiumPath } : {}),
      args: containerArgs,
      ...(chromiumLibs ? { env: { ...process.env, LD_LIBRARY_PATH: chromiumLibs } as Record<string, string> } : {}),
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: [
    {
      command: 'node e2e/start-api.mjs',
      url: `${API_URL}/api/v1/health`,
      // Never reuse a running API. It holds the database of an earlier run, so a green suite would
      // be reporting on a server and a seed that no longer belong to this commit.
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_E2E_API_PORT: String(API_PORT),
      },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: `${WEB_URL}/`,
      // Same reason as the API: a dev server left running serves the previous revision's modules.
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_API_URL: API_URL,
      },
    },
  ],
})
