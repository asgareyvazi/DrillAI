import { randomUUID } from 'node:crypto'
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, devices } from '@playwright/test'

/** Dedicated, fresh-path real-browser stack for the CP150 two-well API/DB pilot. */
const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const listingOnly = process.env.DRILLAI_CP150_LIST_ONLY === 'true'
const runId = process.env.DRILLAI_CP150_RUN_ID ?? `${new Date().toISOString().replace(/\D/g, '')}-${randomUUID()}`
if (!/^[A-Za-z0-9_-]{1,80}$/.test(runId)) {
  throw new Error('DRILLAI_CP150_RUN_ID may contain only letters, numbers, underscores, and hyphens')
}
const firstConfigLoad = !process.env.DRILLAI_CP150_INVOCATION_TOKEN
const invocationToken = process.env.DRILLAI_CP150_INVOCATION_TOKEN ?? randomUUID()
process.env.DRILLAI_CP150_RUN_ID = runId
process.env.DRILLAI_CP150_INVOCATION_TOKEN = invocationToken

const dataPath = process.env.DRILLAI_E2E_DATA_DIR ?? `.e2e-cp150-${runId}`
const reportPath = process.env.DRILLAI_E2E_OUTPUT_DIR ?? `.e2e-cp150-results-${runId}`

function reserveIsolatedPath(value: string, prefix: string, label: string): string {
  const resolved = path.resolve(frontendRoot, value)
  const relative = path.relative(frontendRoot, resolved)
  if (
    relative === '' ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative) ||
    path.dirname(resolved) !== frontendRoot ||
    !path.basename(resolved).startsWith(prefix)
  ) {
    throw new Error(`${label} must be a direct child of frontend/ whose name starts with ${prefix}`)
  }
  if (listingOnly) return relative
  if (existsSync(resolved) && lstatSync(resolved).isSymbolicLink()) {
    throw new Error(`${label} must not be a symbolic link: ${resolved}`)
  }

  const markerPath = label === 'CP150 output directory'
    ? path.join(frontendRoot, dataDir, '.cp150-output-run.json')
    : path.join(resolved, '.cp150-run.json')
  const marker = () => {
    if (!existsSync(markerPath) || lstatSync(markerPath).isSymbolicLink()) return null
    try {
      return JSON.parse(readFileSync(markerPath, 'utf8')) as { invocation_token?: string }
    } catch {
      return null
    }
  }
  const currentMarker = marker()
  if ((existsSync(resolved) || existsSync(markerPath)) && currentMarker?.invocation_token !== invocationToken) {
    throw new Error(
      `${label} already exists: ${resolved}. Choose a fresh DRILLAI_CP150_RUN_ID; ` +
        'the CP150 runner never resets or reuses existing data or output paths.',
    )
  }
  // A matching marker is accepted only when Playwright reloads config within this invocation.
  if (!firstConfigLoad && currentMarker?.invocation_token !== invocationToken) {
    throw new Error(`${label} lost its invocation marker; refusing to use an unowned path.`)
  }
  mkdirSync(resolved, { recursive: true })
  if (currentMarker?.invocation_token !== invocationToken) {
    writeFileSync(
      markerPath,
      JSON.stringify({ run_id: runId, invocation_token: invocationToken, scope: label }, null, 2),
      'utf8',
    )
  }
  return relative
}

const dataDir = reserveIsolatedPath(dataPath, '.e2e-cp150-', 'CP150 data directory')
const outputDir = reserveIsolatedPath(reportPath, '.e2e-cp150-results-', 'CP150 output directory')
process.env.DRILLAI_E2E_DATA_DIR = dataDir
process.env.DRILLAI_E2E_OUTPUT_DIR = outputDir

const API_PORT = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
const WEB_PORT = Number(process.env.DRILLAI_E2E_WEB_PORT ?? 5199)
if (
  !Number.isInteger(API_PORT) || API_PORT < 1 || API_PORT > 65535 ||
  !Number.isInteger(WEB_PORT) || WEB_PORT < 1 || WEB_PORT > 65535 ||
  API_PORT === WEB_PORT
) {
  throw new Error('CP150 API and web ports must be distinct valid TCP ports')
}
const API_URL = `http://127.0.0.1:${API_PORT}`
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`
const chromiumPath = process.env.DRILLAI_CHROMIUM_PATH
const chromiumLibs = process.env.DRILLAI_CHROMIUM_LIBS
const containerArgs = chromiumPath && process.platform === 'linux'
  ? ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-setuid-sandbox']
  : []

export default defineConfig({
  testDir: './e2e',
  testMatch: /cp150-two-well-pilot\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['json', { outputFile: `${outputDir}/e2e-results.json` }]],
  outputDir,
  globalSetup: './e2e/global-setup.cp150.ts',
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
      name: 'chromium-cp150',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 }, baseURL: WEB_URL },
    },
  ],
  webServer: listingOnly ? undefined : [
    {
      command: 'node e2e/start-api.mjs',
      url: `${API_URL}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_E2E_API_PORT: String(API_PORT),
        DRILLAI_E2E_DATA_DIR: dataDir,
        DRILLAI_E2E_SEED: 'false',
        DRILLAI_E2E_CP131: 'false',
        DRILLAI_E2E_CP150: 'true',
        DRILLAI_CP150_RUN_ID: runId,
        DRILLAI_CP150_INVOCATION_TOKEN: invocationToken,
      },
    },
    {
      command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: `${WEB_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        DRILLAI_API_URL: API_URL,
        DRILLAI_E2E_DISABLE_HMR: 'true',
      },
    },
  ],
})
