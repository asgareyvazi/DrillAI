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
