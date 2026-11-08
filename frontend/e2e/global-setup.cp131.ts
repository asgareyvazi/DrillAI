/**
 * Fail closed unless the isolated CP131 database contains exactly the synthetic hierarchy that its
 * browser journey will use. This setup deliberately does not require the unrelated demo fixtures.
 */

import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import type { FullConfig } from '@playwright/test'

interface PilotHierarchy {
  classification: string
  fixture_id: string
  project_id: string
  well_id: string
  well_name: string
  wellbore_id: string
  wellbore_name: string
  section_id: string
  section_name: string
}

const dataDir = path.resolve(process.cwd(), process.env.DRILLAI_E2E_DATA_DIR ?? '.e2e')
const fixturesPath = path.join(dataDir, 'fixtures.json')

async function getJson<T>(apiPort: number, pathname: string): Promise<T> {
  const response = await fetch(`http://127.0.0.1:${apiPort}/api/v1${pathname}`, {
    headers: { 'X-Dev-Roles': 'engineer' },
  })
  if (!response.ok) {
    throw new Error(`the isolated CP131 API returned HTTP ${response.status} for ${pathname}`)
  }
  return (await response.json()) as T
}

async function globalSetup(_config: FullConfig): Promise<void> {
  if (!existsSync(fixturesPath)) {
    throw new Error(`no isolated CP131 fixture file at ${fixturesPath}; the API seed must create it`)
  }
  const fixtures = JSON.parse(readFileSync(fixturesPath, 'utf8')) as {
    cp131_pilot?: PilotHierarchy
  }
  const pilot = fixtures.cp131_pilot
  if (
    !pilot ||
    pilot.classification !== 'synthetic' ||
    pilot.fixture_id !== 'cp131-synthetic-pilot-v1' ||
    pilot.well_name !== 'CP131-SYNTHETIC-PILOT-01' ||
    !pilot.project_id ||
    !pilot.well_id ||
    !pilot.wellbore_id ||
    !pilot.section_id
  ) {
    throw new Error('the CP131 seed did not produce its labelled isolated synthetic well hierarchy')
  }

  const apiPort = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
  await getJson<unknown>(apiPort, `/wells/${pilot.well_id}/state`)
  const wellbores = await getJson<{
    items: Array<{ id: string; name: string; well_id: string }>
  }>(apiPort, `/wells/${pilot.well_id}/wellbores`)
  const matchingWellbores = wellbores.items.filter((row) => row.name === pilot.wellbore_name)
  if (
    matchingWellbores.length !== 1 ||
    matchingWellbores[0]?.id !== pilot.wellbore_id ||
    matchingWellbores[0]?.well_id !== pilot.well_id
  ) {
    throw new Error('the seeded CP131 wellbore does not match the running isolated API database')
  }

  const sections = await getJson<{
    items: Array<{ id: string; name: string; wellbore_id: string }>
  }>(apiPort, `/wellbores/${pilot.wellbore_id}/sections`)
  const matchingSections = sections.items.filter((row) => row.name === pilot.section_name)
  if (
    matchingSections.length !== 1 ||
    matchingSections[0]?.id !== pilot.section_id ||
    matchingSections[0]?.wellbore_id !== pilot.wellbore_id
  ) {
    throw new Error('the seeded CP131 section does not match the running isolated API database')
  }
}

export default globalSetup
