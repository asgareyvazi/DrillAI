import { existsSync, lstatSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export interface CP150WellFixture {
  well_id: string
  well_name: string
  wellbore_id: string
  wellbore_name: string
}

export interface CP150PilotFixtures {
  database_url: string
  classification: 'synthetic'
  fixture_id: 'cp150-two-well-pilot-v1'
  org_slug: 'cp150-synthetic-pilot'
  project_id: string
  project_name: 'CP150 Isolated Two-Well Synthetic Pilot'
  wells: [CP150WellFixture, CP150WellFixture]
  reset_scope: string
}

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

/** Load only the dedicated CP150 fixture file; it never falls back to the shared `.e2e` path. */
export function cp150Fixtures(): CP150PilotFixtures {
  const dataDir = path.resolve(frontendRoot, process.env.DRILLAI_E2E_DATA_DIR ?? '.e2e')
  const relative = path.relative(frontendRoot, dataDir)
  if (
    path.dirname(dataDir) !== frontendRoot ||
    !path.basename(dataDir).startsWith('.e2e-cp150-') ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative) ||
    (existsSync(dataDir) && lstatSync(dataDir).isSymbolicLink())
  ) {
    throw new Error('CP150 fixture loading is restricted to a dedicated frontend/.e2e-cp150-* directory')
  }
  const fixturePath = path.join(dataDir, 'fixtures.json')
  if (!existsSync(fixturePath)) throw new Error(`missing isolated CP150 fixture file: ${fixturePath}`)
  const raw: unknown = JSON.parse(readFileSync(fixturePath, 'utf8'))
  if (typeof raw !== 'object' || raw === null || !('cp150_pilot' in raw)) {
    throw new Error('the isolated API seed did not produce a CP150 fixture envelope')
  }
  const value = (raw as { cp150_pilot?: unknown }).cp150_pilot
  if (typeof value !== 'object' || value === null) {
    throw new Error('the isolated API seed did not produce a CP150 pilot object')
  }
  const pilot = value as Record<string, unknown>
  if (
    pilot.classification !== 'synthetic' ||
    pilot.fixture_id !== 'cp150-two-well-pilot-v1' ||
    pilot.org_slug !== 'cp150-synthetic-pilot' ||
    pilot.project_name !== 'CP150 Isolated Two-Well Synthetic Pilot' ||
    !isNonEmptyString(pilot.database_url) ||
    !pilot.database_url.startsWith('sqlite+aiosqlite:///') ||
    !isNonEmptyString(pilot.project_id) ||
    !isNonEmptyString(pilot.reset_scope) ||
    !Array.isArray(pilot.wells) ||
    pilot.wells.length !== 2
  ) {
    throw new Error('the CP150 seed metadata is incomplete or not classified as exactly two synthetic wells')
  }
  const wells = pilot.wells.map((row) => {
    if (typeof row !== 'object' || row === null) throw new Error('a CP150 well fixture is malformed')
    const well = row as Record<string, unknown>
    if (
      !isNonEmptyString(well.well_id) ||
      !isNonEmptyString(well.well_name) ||
      !isNonEmptyString(well.wellbore_id) ||
      !isNonEmptyString(well.wellbore_name)
    ) {
      throw new Error('a CP150 well fixture is missing its well/wellbore scope')
    }
    return {
      well_id: well.well_id,
      well_name: well.well_name,
      wellbore_id: well.wellbore_id,
      wellbore_name: well.wellbore_name,
    }
  })
  const sortedNames = wells.map((well) => well.well_name).sort()
  if (
    new Set(wells.map((well) => well.well_id)).size !== 2 ||
    new Set(wells.map((well) => well.wellbore_id)).size !== 2 ||
    sortedNames[0] !== 'CP150-SYNTHETIC-WELL-A' ||
    sortedNames[1] !== 'CP150-SYNTHETIC-WELL-B'
  ) {
    throw new Error('CP150 requires the exact two distinct, named synthetic well scopes')
  }
  return {
    database_url: pilot.database_url,
    classification: 'synthetic',
    fixture_id: 'cp150-two-well-pilot-v1',
    org_slug: 'cp150-synthetic-pilot',
    project_id: pilot.project_id,
    project_name: 'CP150 Isolated Two-Well Synthetic Pilot',
    wells: [wells[0]!, wells[1]!],
    reset_scope: pilot.reset_scope,
  }
}
