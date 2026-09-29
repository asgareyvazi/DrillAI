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
  drillingApi: { getWell: vi.fn(), healthReady: vi.fn() },
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
  well_type: 'development',
  status: 'planned',
  operator: 'DrillAI (synthetic)',
}

beforeEach(() => {
  vi.clearAllMocks()
  api.healthReady.mockResolvedValue({ status: 'ready' } as never)
})

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
