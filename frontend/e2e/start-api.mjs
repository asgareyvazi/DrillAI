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
const dataDir = path.join(frontendRoot, '.e2e')
const port = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)

mkdirSync(path.join(dataDir, 'blobs'), { recursive: true })

const python = process.env.DRILLAI_PYTHON ?? path.join(backendRoot, '.venv', 'bin', 'python')
const databaseUrl = `sqlite+aiosqlite:///${path.join(dataDir, 'e2e.db')}`

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
const json = seeded.stdout.slice(seeded.stdout.indexOf('{'))
const fixtures = JSON.parse(json)
writeFileSync(path.join(dataDir, 'fixtures.json'), JSON.stringify(fixtures, null, 2))

const env = {
  ...process.env,
  DRILLAI_ENVIRONMENT: 'development',
  DRILLAI_AUTH_ENABLED: 'false',
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
