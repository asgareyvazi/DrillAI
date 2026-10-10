/**
 * The engine workspace, when the read that gives a run its scope fails.
 *
 * The wellbore and section lists are not decoration here: they are the *context* an engine run is sent
 * with. A failure in that read used to be entirely invisible — the runner simply sent the request
 * without them, so the engine computed a real number at a different scope from the one the page
 * appeared to be working at. These tests pin the replacement: the failure is shown where the run is
 * started, the run's actual context is stated, and the retry re-reads the scope.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { engineRunEnvelope } from '../../api/engineRun'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import EngineeringWorkspace from './EngineeringWorkspace'

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    listEngines: vi.fn(),
    listWellbores: vi.fn(),
    listSections: vi.fn(),
    wellEngineRuns: vi.fn(),
    runEngine: vi.fn(),
    dependencies: vi.fn(),
    identity: vi.fn(),
    actions: vi.fn(),
  },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

const api = vi.mocked(drillingApi)

const engine = {
  key: 'hydraulics',
  name: 'Hydraulics',
  version: '1.0.0',
  category: 'hydraulics',
  domain_pack: 'drilling',
  summary: 'Annular and bit hydraulics.',
  action_level: 'L2',
  validation_status: 'validated',
  deterministic: true,
  requires_well_context: true,
  consumes: [],
  produces: [],
  assumptions: [],
  limitations: [],
  references: [],
  input_schema: { required: [] },
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={['/wells/wel_1/engineering']}>
          <Routes>
            <Route path="/wells/:wellId/engineering" element={<EngineeringWorkspace />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

/** Open the engine's card, which is where a run is started from. */
async function openEngine() {
  await userEvent.click(await screen.findByRole('button', { name: /hydraulics/i }))
}

beforeEach(() => {
  vi.clearAllMocks()
  api.listEngines.mockResolvedValue({ items: [engine], total: 1, limit: 100, offset: 0 } as never)
  api.wellEngineRuns.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 } as never)
  api.listWellbores.mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 } as never)
  // The action catalogue as the server serves it: `engine.run` is registered at L2 and requires the
  // permission of the same name.
  api.actions.mockResolvedValue({
    items: [{ key: 'engine.run', level: 'L2', description: 'run an engine', permission: 'engine.run' }],
    total: 1,
    levels: {},
  } as never)
  api.identity.mockResolvedValue(identity({ permissions: ['engine.*'], ceiling: 'L2' }) as never)
})

function identity({
  permissions,
  ceiling,
  roles = ['engineer'],
}: {
  permissions: string[]
  ceiling: string
  roles?: string[]
}) {
  return {
    principal_id: 'usr_dev',
    principal_kind: 'user',
    role_keys: roles,
    roles: [],
    available_roles: [],
    permissions,
    max_action_level: ceiling,
    auth_enabled: false,
    identity_source: 'development_header',
    development_presets: [],
    locale: 'en',
    note: '',
  }
}

/**
 * The engine runner's capability gate.
 *
 * Running an engine is the L2 action `engine.run`. Two different refusals arrive the same way — a 403 —
 * and they are not the same fact: the identity may not act at that level at all, or it may act at that
 * level and not hold the permission. `authorize()` checks the ceiling first, so the ceiling is what a
 * person has to be told about first; asking for a permission that would not help is the defect these
 * tests exist to prevent.
 */
describe('the engine run gate', () => {
  it('offers the run when the ceiling and the permission both allow it', async () => {
    renderWorkspace()
    await openEngine()

    const run = await screen.findByTestId('engine-run')
    await waitFor(() => expect(run).toHaveAttribute('data-gate-state', 'ready'))
    expect(run).toBeEnabled()
    expect(screen.queryByTestId('engine-run-gate')).toBeNull()
  })

  it('withholds the run and names the ceiling when the identity may not act at that level', async () => {
    // A viewer: an L0 ceiling. `authorize()` refuses on the ceiling and returns `{required_level,
    // ceiling}`; the screen must say that rather than claim a missing permission.
    api.identity.mockResolvedValue(identity({ permissions: ['engine.*'], ceiling: 'L0' }) as never)
    renderWorkspace()
    await openEngine()

    const run = await screen.findByTestId('engine-run')
    await waitFor(() => expect(run).toHaveAttribute('data-gate-state', 'level-below'))
    expect(run).toBeDisabled()
    expect(screen.getByTestId('engine-run-gate')).toHaveTextContent(/up to L0/)
    expect(screen.getByTestId('engine-run-gate')).toHaveTextContent(/L2/)
    expect(screen.getByTestId('engine-run-gate')).not.toHaveTextContent(/does not hold/)
  })

  it('withholds the run and names the permission when the ceiling is sufficient and the permission is not held', async () => {
    // An administrator: L4 ceiling, no `engine.run` permission. The server refuses on the permission,
    // and asking for a higher ceiling would change nothing.
    api.identity.mockResolvedValue(
      identity({ permissions: ['admin.**', '*.read'], ceiling: 'L4', roles: ['admin'] }) as never,
    )
    renderWorkspace()
    await openEngine()

    const run = await screen.findByTestId('engine-run')
    await waitFor(() => expect(run).toHaveAttribute('data-gate-state', 'no-permission'))
    expect(run).toBeDisabled()
    expect(screen.getByTestId('engine-run-gate')).toHaveTextContent(/does not hold 'engine.run'/)
    expect(screen.getByTestId('engine-run-gate')).toHaveTextContent(/admin/)
    expect(screen.getByTestId('engine-run-gate')).not.toHaveTextContent(/up to L/)
  })

  it('renders the result of a successful run rather than crashing on the body it was sent', async () => {
    // The live run's body calls the result `result` and the violations `violations`; the panel reads
    // them as `outputs` and `constraint_violations`. Reading the first as the second threw
    // `undefined.length` on a *successful* run, and the page replaced a real result with an error
    // boundary. The body below is the server's own, and the mapper is the one the endpoint uses.
    const serverBody = {
      engine_key: 'hydraulics',
      engine_version: '1.0.0',
      result: { ecd_kg_m3: 1250, flow_rate_l_s: 32 },
      is_feasible: true,
      warnings: ['annular velocity below the recommended range'],
      violations: [{ code: 'ecd_below_pore_pressure' }],
      assumptions: ['steady-state flow'],
      limitations: ['no surge modelling'],
      engine_run_id: 'ege_1',
      inputs_hash: 'a'.repeat(40),
      outputs_hash: 'b'.repeat(40),
      engine: { key: 'hydraulics', version: '1.0.0', validation_status: 'validated' },
    }
    api.runEngine.mockImplementation(async () => engineRunEnvelope(serverBody))

    renderWorkspace()
    await openEngine()
    const run = await screen.findByTestId('engine-run')
    await waitFor(() => expect(run).toBeEnabled())
    await userEvent.click(run)

    // The result the server computed, with the engine and version that produced it.
    await waitFor(() => expect(screen.getByText(/hydraulics@1\.0\.0/)).toBeInTheDocument())
    expect(screen.getByText(/1,250|1250/)).toBeInTheDocument()
    expect(screen.getByText(/annular velocity below the recommended range/)).toBeInTheDocument()
    expect(screen.getByText(/ecd_below_pore_pressure/)).toBeInTheDocument()
  })

  it('claims nothing about the identity when the permissions could not be read', async () => {
    api.identity.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderWorkspace()
    await openEngine()

    const run = await screen.findByTestId('engine-run')
    await waitFor(() => expect(run).toHaveAttribute('data-gate-state', 'unknown'))
    expect(run).toBeDisabled()
    expect(screen.getByTestId('engine-run-gate')).toHaveTextContent(/could not be read/)
    expect(screen.getByTestId('engine-run-gate')).not.toHaveTextContent(/does not hold/)
  })
})

describe('the engine workspace scope', () => {
  it('says the run would go out without a wellbore or section when that read failed', async () => {
    api.listWellbores.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderWorkspace()
    await openEngine()

    const failure = await screen.findByTestId('engine-scope-failure')
    expect(failure).toHaveTextContent(/wellbore and section scope could not be read/i)
    // The truthful part: what the run will actually carry, not what the page wished for.
    expect(failure).toHaveTextContent(/against the well alone/i)
    // The section list is not even asked for without a wellbore, so no second failure is invented.
    expect(api.listSections).not.toHaveBeenCalled()
  })

  it('re-reads both scope queries when the operator retries, and clears the notice', async () => {
    api.listWellbores.mockRejectedValueOnce(new ApiError(0, 'network.unreachable', 'down'))
    api.listWellbores.mockResolvedValue({ items: [{ id: 'wlb_1' }], total: 1, limit: 100, offset: 0 } as never)
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    renderWorkspace()
    await openEngine()

    const failure = await screen.findByTestId('engine-scope-failure')
    await userEvent.click(within(failure).getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(screen.queryByTestId('engine-scope-failure')).toBeNull())
    expect(api.listWellbores).toHaveBeenCalledTimes(2)
    expect(api.listSections).toHaveBeenCalledTimes(1)
  })

  it('says nothing about the scope when the scope was read', async () => {
    api.listWellbores.mockResolvedValue({ items: [{ id: 'wlb_1' }], total: 1, limit: 100, offset: 0 } as never)
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    renderWorkspace()
    await openEngine()

    await waitFor(() => expect(api.listSections).toHaveBeenCalled())
    expect(screen.queryByTestId('engine-scope-failure')).toBeNull()
  })
})
