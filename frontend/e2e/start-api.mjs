/**
 * Start the real API for the E2E suite, on a database seeded with the labelled synthetic fixtures.
 *
 * The order matters and is why this lives here rather than in `globalSetup`: the fixtures are created
 * *before* the service opens the database. Seeding afterwards leaves a service that is already
 * connected to a schema it did not see created — the suite would then pass an empty database and every
 * assertion would be about "nothing", which is exactly the kind of false green this mission exists to
 * remove.
 *
 * The service runs on a caller-isolated throwaway SQLite database (the regular suite uses `.e2e/`;
 * the CP131 pilot uses a unique `.e2e-cp131-*` directory). The regular demo seed may rebuild its own
 * disposable database; the CP131-only seed creates or reuses only its named hierarchy and never resets
 * an existing database. No production database or production credentials are used.
 *
 * Authentication is *disabled* on purpose: this is a development/test environment and the development
 * role header is how the suite (and the in-app role switcher) exercises the authorization model. The
 * backend refuses that mode outright when `DRILLAI_ENVIRONMENT=production`, and a backend test asserts
 * it — see `backend/tests/api/test_auth_api.py`.
 */

import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
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
const seedCp131Pilot = process.env.DRILLAI_E2E_CP131 === 'true'

mkdirSync(path.join(dataDir, 'blobs'), { recursive: true })

function resolvePython(backendDir) {
  if (process.env.DRILLAI_PYTHON && existsSync(process.env.DRILLAI_PYTHON)) {
    return process.env.DRILLAI_PYTHON
  }
  const candidates =
    process.platform === 'win32'
      ? [
          path.join(backendDir, '.venv', 'Scripts', 'python.exe'),
          path.join(backendDir, '.venv', 'bin', 'python'),
        ]
      : [
          path.join(backendDir, '.venv', 'bin', 'python'),
          path.join(backendDir, '.venv', 'Scripts', 'python.exe'),
        ]
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return candidate
    }
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

const python = resolvePython(backendRoot)
const dbFilePosix = path.join(dataDir, 'e2e.db').replace(/\\/g, '/')
const databaseUrl = `sqlite+aiosqlite:///${dbFilePosix}`

/**
 * The fixtures the seed produced, when this instance seeds at all.
 *
 * The regular stack writes the demo fixtures and may add the CP131 hierarchy; the dedicated CP131
 * stack writes only `cp131_pilot` to its isolated fixture file. The fault stack seeds its own demo
 * database; the authentication-enabled stack has its own auth-focused setup.
 */
let fixtures = {}

function parseSeedOutput(stdout, label) {
  const normalized = stdout.replace(/\r\n/g, '\n')
  const start = normalized.lastIndexOf('\n{\n')
  const json = start === -1 ? normalized.slice(normalized.indexOf('{')) : normalized.slice(start + 1)
  if (!json || !json.trimStart().startsWith('{')) throw new Error(`${label} printed no JSON fixture summary`)
  return JSON.parse(json)
}

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
  // The seed prints a pretty-printed JSON object. A logging line can precede it, so take the last
  // column-zero JSON object after normalizing Windows CRLF line endings.
  fixtures = parseSeedOutput(seeded.stdout, 'demo seed')
}

if (seedCp131Pilot) {
  const pilot = spawnSync(
    python,
    [
      path.join(repoRoot, 'scripts', 'seed_cp131_pilot.py'),
      '--database-url',
      databaseUrl,
      '--blob-dir',
      path.join(dataDir, 'blobs'),
    ],
    { cwd: backendRoot, env: { ...process.env, PYTHONUNBUFFERED: '1' }, encoding: 'utf8' },
  )
  if (pilot.status !== 0) {
    console.error(pilot.stdout)
    console.error(pilot.stderr)
    throw new Error(`seeding the isolated CP131 pilot hierarchy failed (exit ${pilot.status})`)
  }
  fixtures.cp131_pilot = parseSeedOutput(pilot.stdout, 'CP131 synthetic pilot seed')
}

if (seed || seedCp131Pilot) {
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
  DRILLAI_CONNECTOR_ALLOW_LOOPBACK: 'false',
  DRILLAI_CONNECTOR_ALLOW_PRIVATE_NETWORKS: 'false',
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
