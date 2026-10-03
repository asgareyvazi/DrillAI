/**
 * The shell's header, where the well of the screen is named.
 *
 * Three states have to stay apart, because two of them look identical if the header is written as a
 * fallback: no well is open at all, a well is open and its details were read, and a well is open and
 * its details could not be read. The last one used to render the application's own name, which told
 * the reader there was no well context while the URL, the navigation and every panel below were
 * scoped to one.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import { AppShell } from './AppShell'

vi.mock('../../api/endpoints', () => ({
  drillingApi: { getWell: vi.fn(), healthReady: vi.fn(), identity: vi.fn() },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

const api = vi.mocked(drillingApi)

function renderShell(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/wells/:wellId" element={<AppShell />} />
            <Route path="/wells" element={<AppShell />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

const wellPayload = {
  id: 'wel_1',
  name: 'SYNTH-DEMO-01 (synthetic data)',
  well_type: 'development_producer',
  status: 'planned',
  operator: 'DrillAI (synthetic)',
}

beforeEach(() => {
  vi.clearAllMocks()
  api.healthReady.mockResolvedValue({ status: 'ready' } as never)
  // The identity the shell reports on: the server's own catalogue, not a list kept here.
  api.identity.mockResolvedValue(identityPayload as never)
})

const identityPayload = {
  principal_id: 'usr_dev_drilling_supervisor',
  principal_kind: 'user',
  org_id: 'org_1',
  role_keys: ['drilling_supervisor'],
  roles: [
    {
      key: 'drilling_supervisor',
      name: 'Drilling supervisor',
      description: 'Approves L4 execution.',
      max_action_level: 'L4',
    },
  ],
  available_roles: [
    { key: 'viewer', name: 'Viewer', description: 'read', max_action_level: 'L0', permissions: ['well.read'] },
    {
      key: 'drilling_supervisor',
      name: 'Drilling supervisor',
      description: 'approve',
      max_action_level: 'L4',
      permissions: ['workflow.**'],
    },
  ],
  permissions: ['workflow.**'],
  max_action_level: 'L4',
  auth_enabled: false,
  identity_source: 'development_header',
  development_presets: ['viewer', 'engineer', 'drilling_supervisor'],
  locale: 'en',
  note: 'the server decides',
}

describe('the application shell header', () => {
  it('names the well the screen is scoped to', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    renderShell('/wells/wel_1')

    await waitFor(() => expect(screen.getByTestId('shell-well-name')).toHaveTextContent('SYNTH-DEMO-01'))
    expect(screen.getByTestId('shell-well-detail')).toHaveTextContent(/development/)
  })

  it('falls back to the product name when no well is open', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    renderShell('/wells')

    await waitFor(() => expect(screen.getByTestId('shell-well-name')).toHaveTextContent('DrillAI'))
    expect(screen.getByTestId('shell-well-detail')).toHaveTextContent(/Well Engineering/)
    // No well is open, so no well read happens at all.
    expect(api.getWell).not.toHaveBeenCalled()
  })

  it('says the well could not be read instead of pretending none is open', async () => {
    api.getWell.mockRejectedValue(new ApiError(503, 'platform.unavailable', 'busy'))
    renderShell('/wells/wel_1')

    // The id comes from the route, so it is still shown — it is known, not read.
    await waitFor(() => expect(screen.getByTestId('shell-well-name')).toHaveTextContent('wel_1'))
    await waitFor(() =>
      expect(screen.getByTestId('shell-well-detail')).toHaveTextContent(/could not be read/i),
    )
    // The lie this replaces: the header no longer claims there is no well context.
    expect(screen.getByTestId('shell-well-name')).not.toHaveTextContent('DrillAI')
  })
})

/**
 * Who the interface says it is acting as, and how far that identity may act.
 *
 * The switcher's options are the server's `development_presets`. A list kept in the frontend had
 * already drifted from the server's: the backend advertises eight catalogued roles and the copy
 * offered six, so an auditor, a data manager and an integrity engineer could not be exercised from
 * the interface at all. These tests hold the list to the server's answer.
 */
describe('the identity controls', () => {
  it('offers exactly the roles the server advertises, and none it does not', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    renderShell('/wells')

    const select = await screen.findByTestId('shell-role-switch')
    const options = () => Array.from(select.querySelectorAll('option')).map((option) => option.value)
    // Before the read answers, the control shows the session's own selection and nothing else —
    // that is the only thing known at that moment, and it is still true. The server's list replaces
    // it when it arrives.
    expect(options()).toEqual(['engineer,admin'])
    await waitFor(() =>
      expect(options()).toEqual(['engineer,admin', 'viewer', 'engineer', 'drilling_supervisor']),
    )
  })

  it('does not invent a role the server never advertised', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    renderShell('/wells')

    const select = await screen.findByTestId('shell-role-switch')
    const values = () => Array.from(select.querySelectorAll('option')).map((option) => option.value)
    await waitFor(() => expect(values()).toContain('viewer'))
    // `well_manager` is a catalogued role but not a preset in this payload; the interface must show
    // what the server sent, not what a previous version of the interface believed.
    expect(values()).not.toContain('well_manager')
  })

  it('reports the acting roles and the ceiling the server resolved', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    renderShell('/wells')

    const identity = await screen.findByTestId('shell-identity')
    // Both values are the server's: the role's own name and the action-level ceiling.
    expect(identity).toHaveTextContent('Drilling supervisor')
    expect(identity).toHaveAttribute('data-ceiling', 'L4')
    expect(identity).toHaveTextContent(/L4/)
  })

  it('says the roles could not be read instead of showing an empty list', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    api.identity.mockRejectedValue(new ApiError(403, 'security.permission_denied', 'refused'))
    renderShell('/wells')

    await waitFor(() => expect(screen.getByTestId('roles-not-read')).toBeVisible())
    // The selection is a fact about this session, so it is still offered and still shown.
    const select = screen.getByTestId('shell-role-switch')
    expect(select).toHaveValue('engineer,admin')
    expect(screen.queryByTestId('shell-identity')).toBeNull()
  })
})

/**
 * The health badge is the only error surface present on *every* screen, so what it claims about the
 * backend is read more often than any panel's own error state. A single "API unreachable" for every
 * failure sends an operator to check connectivity for a server fault or a deadline.
 */
describe('the health badge', () => {
  it('says the API is unreachable when it really is', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    api.healthReady.mockRejectedValue(new ApiError(0, 'network.unreachable', 'offline'))
    renderShell('/wells')

    await waitFor(() => expect(screen.getByTestId('shell-health')).toHaveTextContent(/unreachable/i))
  })

  it('does not blame the connection for a server fault, and says which failure it is', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    api.healthReady.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderShell('/wells')

    await waitFor(() => expect(screen.getByTestId('shell-health')).toHaveTextContent(/API error/i))
    expect(screen.getByTestId('shell-health')).not.toHaveTextContent(/unreachable/i)
    expect(screen.getByTestId('shell-health')).not.toHaveTextContent(/not answering/i)
  })

  it('keeps a deadline apart from an unreachable backend', async () => {
    api.getWell.mockResolvedValue(wellPayload as never)
    api.healthReady.mockRejectedValue(
      new ApiError(0, 'network.timeout', 'too slow', {}, null, true, { kind: 'timeout' }),
    )
    renderShell('/wells')

    await waitFor(() => expect(screen.getByTestId('shell-health')).toHaveTextContent(/not answering/i))
    expect(screen.getByTestId('shell-health')).not.toHaveTextContent(/unreachable/i)
  })
})
