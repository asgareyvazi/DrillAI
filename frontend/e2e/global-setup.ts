/**
 * Verify that the suite really has fixtures to talk about before it runs.
 *
 * The API server creates the fixtures (see `start-api.mjs`), because they must exist before it opens
 * the database. This step is the assertion that they do: the fixture file exists, it carries the
 * identifiers the specs use, and the running API serves the well it names. A suite that silently runs
 * against an empty database is worse than one that does not run at all.
 */

import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { FullConfig } from '@playwright/test'

export type E2EFixtures = {
  database_url: string
  project_id: string
  well_id: string
  wellbore_id: string
  section_id: string
  document_ids: string[]
  hydraulics_engine_run_id: string
  optimization_run_id: string
  recommendation_id: string
  workflow_id: string
  workflow_key: string
  workflow_version: number
  published: { id: string; version: number; graph_hash: string }
  failing_workflow_id: string
  npt_total_hours: number
  timeline_entries: number
  state_counts: Record<string, number>
}

const here = path.dirname(fileURLToPath(import.meta.url))
const dataDir = path.join(path.resolve(here, '..'), '.e2e')
export const FIXTURES_PATH = path.join(dataDir, 'fixtures.json')

const REQUIRED: (keyof E2EFixtures)[] = [
  'well_id',
  'wellbore_id',
  'section_id',
  'document_ids',
  'workflow_id',
  'failing_workflow_id',
  'recommendation_id',
  'optimization_run_id',
]

async function globalSetup(_config: FullConfig): Promise<void> {
  if (!existsSync(FIXTURES_PATH)) {
    throw new Error(
      `no E2E fixtures at ${FIXTURES_PATH}; the API server command in playwright.config.ts creates them`,
    )
  }
  const fixtures = JSON.parse(readFileSync(FIXTURES_PATH, 'utf8')) as E2EFixtures
  const missing = REQUIRED.filter((key) => {
    const value = fixtures[key]
    return value === undefined || value === null || (Array.isArray(value) && value.length === 0)
  })
  if (missing.length > 0) {
    throw new Error(`the fixture seed did not produce: ${missing.join(', ')}`)
  }

  const apiPort = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
  const response = await fetch(`http://127.0.0.1:${apiPort}/api/v1/wells/${fixtures.well_id}/state`, {
    headers: { 'X-Dev-Roles': 'engineer' },
  })
  if (!response.ok) {
    throw new Error(
      `the API did not serve the seeded well ${fixtures.well_id} (HTTP ${response.status}); ` +
        'the service and the fixtures are not looking at the same database',
    )
  }
}

export default globalSetup
