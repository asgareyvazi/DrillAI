/**
 * Start the real API for the E2E suite, on a database seeded with the labelled synthetic fixtures.
 *
 * The order matters and is why this lives here rather than in `globalSetup`: the fixtures are created
 * *before* the service opens the database. Seeding afterwards leaves a service that is already
 * connected to a schema it did not see created — the suite would then pass an empty database and every
 * assertion would be about "nothing", which is exactly the kind of false green this mission exists to
 * remove.
 *
 * The service runs on a throwaway SQLite database in `.e2e/` rebuilt from scratch on every run. No
 * production database, no production credentials, no network access.
 *
 * Authentication is *disabled* on purpose: this is a development/test environment and the development
 * role header is how the suite (and the in-app role switcher) exercises the authorization model. The
 * backend refuses that mode outright when `DRILLAI_ENVIRONMENT=production`, and a backend test asserts
 * it — see `backend/tests/api/test_auth_api.py`.
 */

import { spawn, spawnSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')
const repoRoot = path.resolve(frontendRoot, '..')
const backendRoot = path.join(repoRoot, 'backend')
const dataDir = path.resolve(frontendRoot, process.env.DRILLAI_E2E_DATA_DIR ?? '.e2e')
const port = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
// Two more instances of the same service run beside this one, and both differ only in configuration:
// `DRILLAI_E2E_FAULTS` gives the browser a server that can really fail (an armed fault, a real
// timeout), and `DRILLAI_E2E_AUTH` serves the authentication-enabled deployment the 401 journey needs.
// They are the same application and the same seed — no mock backend anywhere in the suite.
const authEnabled = process.env.DRILLAI_E2E_AUTH === 'true'
const faultsEnabled = process.env.DRILLAI_E2E_FAULTS === 'true'
const seed = process.env.DRILLAI_E2E_SEED !== 'false'

mkdirSync(path.join(dataDir, 'blobs'), { recursive: true })

const python = process.env.DRILLAI_PYTHON ?? path.join(backendRoot, '.venv', 'bin', 'python')
const databaseUrl = `sqlite+aiosqlite:///${path.join(dataDir, 'e2e.db')}`

/**
 * The fixtures the seed produced, when this instance seeds at all.
 *
 * The main stack seeds and writes `fixtures.json`; the fault stack seeds its own database and writes
 * its own copy; the authentication-enabled stack seeds nothing — it exists so the client can be
 * refused, and it has no data to be refused about.
 */
let fixtures = {}
if (seed) {
  const seeded = spawnSync(
    python,
    [
      path.join(repoRoot, 'scripts', 'seed_demo.py'),
      '--reset',
      '--database-url',
      databaseUrl,
      '--blob-dir',
      path.join(dataDir, 'blobs'),
    ],
    { cwd: backendRoot, env: { ...process.env, PYTHONUNBUFFERED: '1' }, encoding: 'utf8' },
  )

  if (seeded.status !== 0) {
    console.error(seeded.stdout)
    console.error(seeded.stderr)
    throw new Error(`seeding the E2E fixtures failed (exit ${seeded.status})`)
  }
  // The seed prints the fixtures as pretty-printed JSON, but a logging line can precede them (the
  // logger emits one JSON object per line). The fixtures object is the last one printed at column 0,
  // so it is found by its own indentation rather than by the first brace in the output.
  const start = seeded.stdout.lastIndexOf('\n{\n')
  const json = start === -1 ? seeded.stdout.slice(seeded.stdout.indexOf('{')) : seeded.stdout.slice(start + 1)
  fixtures = JSON.parse(json)
  writeFileSync(path.join(dataDir, 'fixtures.json'), JSON.stringify(fixtures, null, 2))
}

const env = {
  ...process.env,
  DRILLAI_ENVIRONMENT: 'development',
  DRILLAI_AUTH_ENABLED: authEnabled ? 'true' : 'false',
  DRILLAI_E2E_FAULTS: faultsEnabled ? 'true' : 'false',
  DRILLAI_DATABASE_URL: databaseUrl,
  DRILLAI_BLOB_BACKEND: 'filesystem',
  DRILLAI_BLOB_ROOT: path.join(dataDir, 'blobs'),
  DRILLAI_SCHEDULER_ENABLED: 'false',
  DRILLAI_DEV_ORG_SLUG: fixtures.org_slug ?? 'demo-operator',
  DRILLAI_LOG_JSON: 'false',
  PYTHONUNBUFFERED: '1',
}

const child = spawn(
  python,
  [
    '-m',
    'uvicorn',
    '--factory',
    'drillai.api.app:create_app',
    '--host',
    '127.0.0.1',
    '--port',
    String(port),
    '--log-level',
    'warning',
  ],
  { cwd: backendRoot, env, stdio: 'inherit' },
)

const shutdown = (signal) => child.kill(signal)
process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))
child.on('exit', (code) => process.exit(code ?? 0))
