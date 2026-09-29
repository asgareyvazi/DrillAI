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

// Two more stacks, each the same application and the same client, configured differently. The
// product code is identical in all three; what differs is the deployment they are pointed at:
//
//  * `faults` — the API runs with deterministic fault injection enabled, and the client is built
//    with a short request deadline, so a real timeout, a real malformed body and a real 500 can be
//    produced for the browser journeys. Nothing is intercepted in the browser.
//  * `auth` — the API runs with authentication enabled, which is the only way to see a real 401.
//
// They are separate stacks because the restrictions are process-wide settings, and because a journey
// that proves the product survives an authentication-enabled deployment must not be a special case
// inside the main suite's server.
const FAULT_API_PORT = Number(process.env.DRILLAI_E2E_FAULT_API_PORT ?? 8098)
const FAULT_WEB_PORT = Number(process.env.DRILLAI_E2E_FAULT_WEB_PORT ?? 5198)
const FAULT_API_URL = `http://127.0.0.1:${FAULT_API_PORT}`
const FAULT_WEB_URL = `http://127.0.0.1:${FAULT_WEB_PORT}`
/** Deadline the fault stack's client enforces; the slow fault waits longer than this, on purpose. */
const FAULT_TIMEOUT_MS = '3000'

const AUTH_API_PORT = Number(process.env.DRILLAI_E2E_AUTH_API_PORT ?? 8097)
const AUTH_WEB_PORT = Number(process.env.DRILLAI_E2E_AUTH_WEB_PORT ?? 5197)
const AUTH_API_URL = `http://127.0.0.1:${AUTH_API_PORT}`
const AUTH_WEB_URL = `http://127.0.0.1:${AUTH_WEB_PORT}`

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
      // The product's own journeys, against the development-identity service.
      name: 'chromium',
      testIgnore: /error-(faults|auth)\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, baseURL: WEB_URL },
    },
    {
      // The failures a healthy server cannot produce on request: a real deadline exceeded, a body
      // that is not the contract, an unhandled fault injected into a request the interface makes.
      name: 'chromium-faults',
      testMatch: /error-faults\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, baseURL: FAULT_WEB_URL },
    },
    {
      // Authentication enabled: the deployment where "you are not signed in" is a real state.
      name: 'chromium-auth',
      testMatch: /error-auth\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, baseURL: AUTH_WEB_URL },
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
    {
      // The same API with fault injection on, seeded like the main one so its journeys can run real
      // work first and fail a real request afterwards.
      command: 'node e2e/start-api.mjs',
      url: `${FAULT_API_URL}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_E2E_API_PORT: String(FAULT_API_PORT),
        DRILLAI_E2E_DATA_DIR: '.e2e-faults',
        DRILLAI_E2E_FAULTS: 'true',
      },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${FAULT_WEB_PORT} --strictPort`,
      url: `${FAULT_WEB_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_API_URL: FAULT_API_URL,
        VITE_API_TIMEOUT_MS: FAULT_TIMEOUT_MS,
      },
    },
    {
      // The same API with authentication enabled: every request without a credential is refused by
      // the real authorization path. No fixtures — the 401 journey never gets far enough to need data.
      command: 'node e2e/start-api.mjs',
      url: `${AUTH_API_URL}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_E2E_API_PORT: String(AUTH_API_PORT),
        DRILLAI_E2E_DATA_DIR: '.e2e-auth',
        DRILLAI_E2E_AUTH: 'true',
        DRILLAI_E2E_SEED: 'false',
      },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${AUTH_WEB_PORT} --strictPort`,
      url: `${AUTH_WEB_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_API_URL: AUTH_API_URL,
      },
    },
  ],
})
