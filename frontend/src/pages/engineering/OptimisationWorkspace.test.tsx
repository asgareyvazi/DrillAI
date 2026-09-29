/**
 * The optimisation workspace, when the section read fails.
 *
 * `section_id` in the request comes from that read. With the read failed it used to be silently
 * `null`: the optimiser then ranked candidates for whatever scope the absence implied, and the page
 * presented the result as the answer to the question the engineer had asked. These tests pin the
 * replacement — the request's real scope is stated before the run, and only that read is retried.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import OptimisationWorkspace from './OptimisationWorkspace'

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    optimisationObjectives: vi.fn(),
    listWellbores: vi.fn(),
    listSections: vi.fn(),
    optimise: vi.fn(),
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

const objectives = {
  computable: [
    { key: 'ecd_margin_si', name: 'ECD margin', engine_key: 'hydraulics', direction: 'maximise' },
  ],
  not_evaluated: {},
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={['/wells/wel_1/optimisation']}>
          <Routes>
            <Route path="/wells/:wellId/optimisation" element={<OptimisationWorkspace />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.optimisationObjectives.mockResolvedValue(objectives as never)
  api.listWellbores.mockResolvedValue({ items: [{ id: 'wlb_1' }], total: 1, limit: 100, offset: 0 } as never)
})

describe('the optimisation workspace scope', () => {
  it('says the request would be sent without a section when that read failed', async () => {
    api.listSections.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderWorkspace()

    const failure = await screen.findByTestId('optimise-scope-failure')
    expect(failure).toHaveTextContent(/section scope could not be read/i)
    expect(failure).toHaveTextContent(/without one/i)
    expect(failure).not.toHaveTextContent(/unreachable/i)
  })

  it('re-reads the section list when the operator retries, and clears the notice', async () => {
    api.listSections.mockRejectedValueOnce(new ApiError(0, 'network.unreachable', 'down'))
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    renderWorkspace()

    const failure = await screen.findByTestId('optimise-scope-failure')
    await userEvent.click(within(failure).getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(screen.queryByTestId('optimise-scope-failure')).toBeNull())
    expect(api.listSections).toHaveBeenCalledTimes(2)
  })

  it('says nothing when the section list was read', async () => {
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    renderWorkspace()

    await waitFor(() => expect(api.listSections).toHaveBeenCalled())
    expect(screen.queryByTestId('optimise-scope-failure')).toBeNull()
  })
})
