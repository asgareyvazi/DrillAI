import type { FullConfig } from '@playwright/test'
import { cp150Fixtures } from './cp150-fixtures'

async function getJson<T>(apiPort: number, pathname: string): Promise<T> {
  const response = await fetch(`http://127.0.0.1:${apiPort}/api/v1${pathname}`, {
    headers: { 'X-Dev-Roles': 'engineer' },
  })
  if (!response.ok) throw new Error(`the isolated CP150 API returned HTTP ${response.status} for ${pathname}`)
  return (await response.json()) as T
}

async function globalSetup(_config: FullConfig): Promise<void> {
  const pilot = cp150Fixtures()
  const apiPort = Number(process.env.DRILLAI_E2E_API_PORT ?? 8099)
  if (!Number.isInteger(apiPort) || apiPort < 1 || apiPort > 65535) {
    throw new Error('DRILLAI_E2E_API_PORT must be a valid TCP port for the CP150 API')
  }

  const projects = await getJson<{ items: Array<{ id: string; name: string }> }>(apiPort, '/projects')
  if (
    projects.items.length !== 1 ||
    projects.items[0]?.id !== pilot.project_id ||
    projects.items[0]?.name !== pilot.project_name
  ) {
    throw new Error('the CP150 project readback does not match the isolated API fixture')
  }

  const allWells = await getJson<{
    items: Array<{ id: string; name: string; project_id: string }>
    total: number
  }>(apiPort, '/wells?limit=500')
  if (allWells.total !== 2 || allWells.items.length !== 2) {
    throw new Error('the isolated CP150 API database must contain exactly its two seeded wells')
  }

  const projectWells = await getJson<{
    items: Array<{ id: string; name: string; project_id: string }>
    total: number
  }>(apiPort, `/wells?project_id=${encodeURIComponent(pilot.project_id)}&limit=500`)
  const expectedWells = new Map(pilot.wells.map((well) => [well.well_id, well]))
  if (
    projectWells.total !== 2 ||
    projectWells.items.length !== 2 ||
    projectWells.items.some((row) => {
      const fixture = expectedWells.get(row.id)
      return !fixture || row.project_id !== pilot.project_id || row.name !== fixture.well_name
    })
  ) {
    throw new Error('the CP150 two-well project scope differs between fixture and real API readback')
  }

  for (const fixture of pilot.wells) {
    const well = await getJson<{
      id: string
      name: string
      project_id: string
    }>(apiPort, `/wells/${encodeURIComponent(fixture.well_id)}`)
    if (well.id !== fixture.well_id || well.name !== fixture.well_name || well.project_id !== pilot.project_id) {
      throw new Error(`the CP150 API did not return the expected well ${fixture.well_name}`)
    }
    const wellbores = await getJson<{
      items: Array<{ id: string; name: string; well_id: string }>
      total: number
    }>(apiPort, `/wells/${encodeURIComponent(fixture.well_id)}/wellbores`)
    if (
      wellbores.total !== 1 ||
      wellbores.items.length !== 1 ||
      wellbores.items[0]?.id !== fixture.wellbore_id ||
      wellbores.items[0]?.name !== fixture.wellbore_name ||
      wellbores.items[0]?.well_id !== fixture.well_id
    ) {
      throw new Error(`the CP150 API wellbore scope does not match ${fixture.well_name}`)
    }
    const reports = await getJson<{ items: unknown[]; total: number }>(
      apiPort,
      `/wells/${encodeURIComponent(fixture.well_id)}/daily-reports?limit=50`,
    )
    if (reports.total !== 0 || reports.items.length !== 0) {
      throw new Error(`the fresh CP150 database already has daily reports for ${fixture.well_name}`)
    }
  }
}

export default globalSetup
