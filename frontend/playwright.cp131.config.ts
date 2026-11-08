import { randomUUID } from 'node:crypto'
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, devices } from '@playwright/test'

/**
 * Isolated, real-service configuration for the CP131 synthetic-well browser pilot.
 *
 * The default data and report paths are unique per invocation. The API seed creates only the named
 * CP131 hierarchy in that fresh directory; no demo fixtures or pre-existing `.e2e` data are read,
 * reset, or reused. Any existing CP131 database or report path is rejected; use a fresh run ID.
 */
const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const runId = process.env.DRILLAI_CP131_RUN_ID ?? `${new Date().toISOString().replace(/\D/g, '')}-${randomUUID()}`
if (!/^[A-Za-z0-9_-]{1,80}$/.test(runId)) {
  throw new Error('DRILLAI_CP131_RUN_ID may contain only letters, numbers, underscores, and hyphens')
}
// Playwright may reload its config in a worker after webServer has created the data directory. This
// private token distinguishes the same invocation from a previous run that chose the same run ID.
const firstConfigLoad = !process.env.DRILLAI_CP131_INVOCATION_TOKEN
const invocationToken = process.env.DRILLAI_CP131_INVOCATION_TOKEN ?? randomUUID()
process.env.DRILLAI_CP131_RUN_ID = runId
process.env.DRILLAI_CP131_INVOCATION_TOKEN = invocationToken

const dataPath = process.env.DRILLAI_E2E_DATA_DIR ?? `.e2e-cp131-${runId}`
const reportPath = process.env.DRILLAI_E2E_OUTPUT_DIR ?? `.e2e-cp131-results-${runId}`

function isolatedChildPath(value: string, prefix: string, label: string): string {
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
  if (existsSync(resolved) && lstatSync(resolved).isSymbolicLink()) {
    throw new Error(`${label} must not be a symbolic link: ${resolved}`)
  }

  const markerPath = path.join(resolved, '.cp131-run.json')
  const existingMarker = () => {
    if (!existsSync(markerPath)) return null
    try {
      return JSON.parse(readFileSync(markerPath, 'utf8')) as { invocation_token?: string }
    } catch {
      return null
    }
  }
  let marker = existingMarker()
  if (existsSync(resolved) && marker?.invocation_token !== invocationToken) {
    const activeOutputDirectory =
      !firstConfigLoad &&
      path.basename(resolved).startsWith('.e2e-cp131-results-') &&
      process.env.DRILLAI_E2E_DATA_DIR !== undefined &&
      (() => {
        const activeDataMarker = path.join(process.env.DRILLAI_E2E_DATA_DIR!, '.cp131-run.json')
        if (!existsSync(activeDataMarker)) return false
        try {
          return (
            (JSON.parse(readFileSync(activeDataMarker, 'utf8')) as { invocation_token?: string })
              .invocation_token === invocationToken
          )
        } catch {
          return false
        }
      })()
    if (!activeOutputDirectory) {
      throw new Error(
        `${label} already exists: ${resolved}. Choose a fresh DRILLAI_CP131_RUN_ID; ` +
          'the CP131 runner never resets or reuses existing database or report paths.',
      )
    }
  }

// Reserve paths before webServer starts. A matching token is accepted only as another Playwright
// process in this run; an earlier directory with the same human-readable run ID requires a fresh ID.
// There is deliberately no reset opt-in.
  mkdirSync(resolved, { recursive: true })
  marker = existingMarker()
  if (marker?.invocation_token !== invocationToken) {
    writeFileSync(
      markerPath,
      JSON.stringify({ run_id: runId, invocation_token: invocationToken, scope: label }, null, 2),
      'utf8',
    )
  }
  return path.relative(frontendRoot, resolved)
}

const dataDir = isolatedChildPath(dataPath, '.e2e-cp131-', 'CP131 data directory')
const outputDir = isolatedChildPath(reportPath, '.e2e-cp131-results-', 'CP131 output directory')

// The global setup resolves fixtures.json from this variable. webServer receives the same value.
process.env.DRILLAI_E2E_DATA_DIR = dataDir
process.env.DRILLAI_E2E_OUTPUT_DIR = outputDir

const API_PORT = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
const WEB_PORT = Number(process.env.DRILLAI_E2E_WEB_PORT ?? 5199)
const API_URL = `http://127.0.0.1:${API_PORT}`
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`
const chromiumPath = process.env.DRILLAI_CHROMIUM_PATH
const chromiumLibs = process.env.DRILLAI_CHROMIUM_LIBS
const containerArgs =
  chromiumPath && process.platform === 'linux'
    ? ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-setuid-sandbox']
    : []

export default defineConfig({
  testDir: './e2e',
  testMatch: /cp131-synthetic-pilot\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 150_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['json', { outputFile: `${outputDir}/e2e-results.json` }]],
  outputDir,
  globalSetup: './e2e/global-setup.cp131.ts',
  use: {
    baseURL: WEB_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 25_000,
    launchOptions: {
      ...(chromiumPath ? { executablePath: chromiumPath } : {}),
      args: containerArgs,
      ...(chromiumLibs
        ? { env: { ...process.env, LD_LIBRARY_PATH: chromiumLibs } as Record<string, string> }
        : {}),
    },
  },
  projects: [
    {
      name: 'chromium-cp131',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 1000 },
        baseURL: WEB_URL,
      },
    },
  ],
  webServer: [
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
        DRILLAI_E2E_CP131: 'true',
        DRILLAI_E2E_SEED: 'false',
      },
    },
    {
      command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: `${WEB_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: { DRILLAI_API_URL: API_URL },
    },
  ],
})
