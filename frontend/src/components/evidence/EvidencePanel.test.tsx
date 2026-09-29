/**
 * The evidence summary strip, and the panel it opens.
 *
 * The strip is the only way into the evidence drawer from the cockpit, so what it does when its read
 * fails is a product decision, not a styling one: it used to render nothing at all, which removed the
 * entry point as well as the number. These tests pin the behaviour that replaced it — the control
 * stays, the number is not invented, and the retry re-reads the summary and nothing else.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import { EvidenceSummaryStrip } from './EvidencePanel'

vi.mock('../../api/endpoints', () => ({
  drillingApi: { evidenceSummary: vi.fn(), listEvidence: vi.fn() },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

const api = vi.mocked(drillingApi)

function renderStrip(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>{node}</I18nProvider>
    </QueryClientProvider>,
  )
}

const summaryPayload = {
  well_id: 'wel_1',
  link_count: 4,
  verified_quotes: 2,
  documents_referenced: 1,
  by_kind: { quote: 3, measurement: 1 },
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('the cockpit evidence strip', () => {
  it('shows the counts the server returned', async () => {
    api.evidenceSummary.mockResolvedValue(summaryPayload as never)
    renderStrip(<EvidenceSummaryStrip wellId="wel_1" onOpen={() => {}} />)

    const strip = await screen.findByTestId('evidence-summary-strip')
    expect(strip).toHaveAttribute('data-summary-state', 'loaded')
    expect(strip).toHaveTextContent(/4 links/)
    expect(strip).toHaveTextContent(/2 verified/)
  })

  it('keeps the way into the evidence panel when the summary cannot be read', async () => {
    api.evidenceSummary.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    const onOpen = vi.fn()
    renderStrip(<EvidenceSummaryStrip wellId="wel_1" onOpen={onOpen} />)

    const strip = await screen.findByTestId('evidence-summary-strip')
    expect(strip).toHaveAttribute('data-summary-state', 'failed')
    // The failure is named, and nothing is invented: no counts, and no claim that there are none.
    expect(strip).toHaveTextContent(/could not be read/i)
    expect(strip).not.toHaveTextContent(/0 links/)
    expect(strip).not.toHaveTextContent(/unreachable/i)

    // The door still opens: the panel holds the citations attached to this screen, which were never
    // part of the summary read.
    await userEvent.click(screen.getByText(/show evidence/i))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('offers a retry that re-reads the summary, and succeeds when the server recovers', async () => {
    api.evidenceSummary.mockRejectedValueOnce(new ApiError(0, 'network.unreachable', 'down'))
    renderStrip(<EvidenceSummaryStrip wellId="wel_1" onOpen={() => {}} />)

    const retry = await screen.findByTestId('evidence-summary-retry')
    api.evidenceSummary.mockResolvedValue(summaryPayload as never)
    await userEvent.click(retry)

    await waitFor(() =>
      expect(screen.getByTestId('evidence-summary-strip')).toHaveAttribute('data-summary-state', 'loaded'),
    )
    expect(api.evidenceSummary).toHaveBeenCalledTimes(2)
  })
})
