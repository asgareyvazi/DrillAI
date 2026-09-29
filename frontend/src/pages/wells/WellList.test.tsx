/**
 * The wells list, when one of its two reads fails.
 *
 * The wells themselves and the project breakdown are separate requests with different importance:
 * the list is the page, the breakdown is enrichment. What must never happen is the page *saying*
 * something it does not know — the subtitle used to print `0 wells` for a failed well read, and the
 * project card used to vanish, which reads as "there are no projects" when the truth is that nobody
 * asked successfully.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import WellList from './WellList'

vi.mock('../../api/endpoints', () => ({
  drillingApi: { listWells: vi.fn(), listProjects: vi.fn() },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

const api = vi.mocked(drillingApi)

const wellsPage = {
  items: [
    {
      id: 'wel_1',
      name: 'SYNTH-DEMO-01 (synthetic data)',
      well_type: 'development',
      status: 'planned',
      operator: 'DrillAI (synthetic)',
      uwi: null,
    },
  ],
  total: 1,
  limit: 200,
  offset: 0,
}

const projectsPage = {
  items: [{ id: 'prj_1', name: 'Synthetic Development Project', code: 'SDP', phase: 'engineering', well_count: 1 }],
  total: 1,
  limit: 100,
  offset: 0,
}

function renderList() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={['/wells']}>
          <WellList />
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.listWells.mockResolvedValue(wellsPage as never)
  api.listProjects.mockResolvedValue(projectsPage as never)
})

describe('the wells list', () => {
  it('names both counts when both reads answered', async () => {
    renderList()

    await waitFor(() => expect(screen.getByText(/1 wells across 1 projects/)).toBeInTheDocument())
  })

  it('does not turn a failed project read into "no projects"', async () => {
    api.listProjects.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderList()

    const notice = await screen.findByTestId('projects-unavailable')
    expect(notice).toHaveTextContent(/project list could not be read/i)
    // The wells are unaffected and stay on screen, with their own count still stated.
    expect(screen.getByText(/1 wells$/)).toBeInTheDocument()
    expect(screen.getByText('SYNTH-DEMO-01 (synthetic data)')).toBeInTheDocument()
    // The failure is the server's, and it is named as the server's — not as a lost connection.
    expect(notice).toHaveTextContent(/server failed/i)
    expect(notice).not.toHaveTextContent(/unreachable/i)
    // The project table itself is absent: there is no data to put in it, and an empty table would be
    // the "no projects" claim this test exists to prevent.
    expect(screen.queryByText('Synthetic Development Project')).toBeNull()
  })

  it('retries only the project read', async () => {
    api.listProjects.mockRejectedValueOnce(new ApiError(0, 'network.unreachable', 'down'))
    renderList()

    const notice = await screen.findByTestId('projects-unavailable')
    await userEvent.click(within(notice).getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(screen.queryByTestId('projects-unavailable')).toBeNull())
    expect(api.listProjects).toHaveBeenCalledTimes(2)
    // The wells list is not re-read: it was never the thing that failed.
    expect(api.listWells).toHaveBeenCalledTimes(1)
  })

  it('never prints a count for a read that failed', async () => {
    api.listWells.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderList()

    await screen.findByTestId('error-state')
    expect(screen.queryByText(/0 wells/)).toBeNull()
    // And the wells count is not borrowed from the project read either.
    expect(screen.queryByText(/wells across/)).toBeNull()
  })
})
