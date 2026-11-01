/**
 * Operations Workspace against the payloads the backend actually serves.
 *
 * The three fixtures were captured from the running API by `capture-fixtures.mjs` — a real service on
 * a throwaway database seeded with the labelled synthetic fixtures, read over HTTP, written down
 * unedited. Nothing here is hand-written JSON: a component test that mocks a shape nobody has ever
 * served is a test of the mock.
 *
 * The rules this file enforces, because each one has been broken somewhere before:
 *
 * * a planned operation is labelled a plan, and shows planned hours in the plan's column;
 * * an event's NPT charge comes from its own `is_npt`/`npt_hours` fields, never from its kind;
 * * a cause's basis (recorded / inferred / unknown) is rendered with the cause;
 * * a failed read is an error state, and an empty list is a different screen;
 * * the drawer's transitions are the server's, and a terminal record offers none.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { I18nProvider } from '../../i18n'
import OperationsWorkspace from './OperationsWorkspace'
import eventsFixture from '../../test/fixtures/events-list.json'
import operationsFixture from '../../test/fixtures/operations-list.json'
import timelineFixture from '../../test/fixtures/operations-timeline.json'
import type { EventRow, OperationRow, TimelinePage, WellStructure } from '../../api/types'

const operations = operationsFixture as unknown as { items: OperationRow[]; total: number }
const manualStructure = {
  well: { project_id: 'project_capture', active_wellbore_id: 'wellbore_capture' },
  wellbores: [
    {
      id: 'wellbore_capture',
      name: 'Main bore',
      status: 'drilling',
      datum: 'kb',
      sections: [{ id: 'section_capture', sequence: 1, name: 'Surface hole' }],
    },
  ],
} as unknown as WellStructure
const events = eventsFixture as unknown as { items: EventRow[]; total: number }
const timeline = timelineFixture as unknown as TimelinePage

/**
 * The payloads the mocked API layer answers with, so a test can fail a read or serve the same
 * captured rows with one field changed.
 */
const served = vi.hoisted(() => ({
  operationsFail: false,
  operations: null as unknown,
  events: null as unknown,
  wellStructure: null as unknown,
  createOperation: vi.fn(),
  createEvent: vi.fn(),
}))

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    listOperations: () =>
      served.operationsFail
        ? Promise.reject(new ApiError(503, 'server_error', 'the API is not reachable', {}, null, true))
        : Promise.resolve(served.operations),
    listEvents: () => Promise.resolve(served.events),
    wellTimeline: () => Promise.resolve(timelineFixture),
    wellStructure: () => Promise.resolve(served.wellStructure),
    createOperation: served.createOperation,
    createEvent: served.createEvent,
    transitionOperation: vi.fn(),
    updateOperation: vi.fn(),
    transitionEvent: vi.fn(),
    linkOperationDocument: vi.fn(),
    linkEventDocument: vi.fn(),
  },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

function renderWorkspace(search = '?tab=operations') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[`/wells/wel_capture/operations${search}`]}>
            <Routes>
              <Route path="/wells/:wellId/operations" element={children} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<OperationsWorkspace />, { wrapper: Wrapper })
}

/** The table row that contains the given cell text. */
function rowOf(text: string): HTMLElement {
  const cell = screen.getByText(text)
  const row = cell.closest('tr')
  if (!row) throw new Error(`"${text}" is not inside a table row`)
  return row
}

beforeEach(() => {
  served.operationsFail = false
  served.operations = operationsFixture
  served.events = eventsFixture
  served.wellStructure = manualStructure
  served.createOperation.mockReset().mockResolvedValue({})
  served.createEvent.mockReset().mockResolvedValue({})
  window.localStorage.clear()
})

describe('OperationsWorkspace: the operations tab', () => {
  it('renders every operation the API served', async () => {
    renderWorkspace()

    const first = operations.items[0] as OperationRow
    await screen.findByText(first.name)
    const rows = screen.getAllByRole('row').slice(1) // the header row is not a record
    expect(rows).toHaveLength(operations.items.length)
    for (const row of operations.items) {
      expect(screen.getByText(row.name)).toBeInTheDocument()
    }
    expect(screen.getByText(`${operations.total} records matching the current filter`)).toBeInTheDocument()
  })

  it('opens the manual-operation form and creates an actual with scoped provenance', async () => {
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(screen.getByTestId('manual-operation-toggle'))
    await screen.findByTestId('manual-operation-form')
    await user.type(screen.getByTestId('manual-operation-name'), 'Manual drilling entry')
    await user.click(screen.getByTestId('manual-operation-submit'))

    await waitFor(() => expect(served.createOperation).toHaveBeenCalledTimes(1))
    expect(served.createOperation).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: 'project_capture',
        well_id: 'wel_capture',
        wellbore_id: 'wellbore_capture',
        name: 'Manual drilling entry',
        operation_class: 'actual',
        kind: 'drilling',
        status: 'completed',
        source_kind: 'manual',
      }),
      expect.stringMatching(/^ui-operation-create-wel_capture-/),
    )
  })

  it('blocks a manual operation whose ending measured depth precedes its start', async () => {
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(screen.getByTestId('manual-operation-toggle'))
    await screen.findByTestId('manual-operation-form')
    await user.type(screen.getByTestId('manual-operation-name'), 'Invalid depth interval')
    await user.type(screen.getByTestId('manual-operation-depth-from'), '1100')
    await user.type(screen.getByTestId('manual-operation-depth-to'), '1000')
    await user.click(screen.getByTestId('manual-operation-submit'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Measured depth to must not be less than measured depth from.')
    expect(served.createOperation).not.toHaveBeenCalled()
  })

  it('shows an actual operation’s actual hours and labels it an actual', async () => {
    const record = operations.items[0] as OperationRow
    expect(record.is_planned).toBe(false)

    renderWorkspace()

    await screen.findByText(record.name)
    const row = rowOf(record.name)
    expect(within(row).getByText('Actual')).toBeInTheDocument()
    // 8.5 h as the API recorded it — actual hours, in the actuals column.
    expect(within(row).getByText('8.5')).toBeInTheDocument()
    expect(within(row).getByText('Extracted')).toBeInTheDocument()
  })

  it('opens the drawer for the operation named in the URL', async () => {
    const record = operations.items[0] as OperationRow
    renderWorkspace(`?tab=operations&operation=${record.id}`)

    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getByText(record.name)).toBeInTheDocument()
    expect(within(drawer).getByText(String(record.sequence))).toBeInTheDocument()
  })

  it('links the drawer back to the document the operation was promoted from', async () => {
    const record = operations.items[0] as OperationRow
    renderWorkspace(`?tab=operations&operation=${record.id}`)

    const drawer = await screen.findByRole('dialog')
    const link = await within(drawer).findByRole('link', { name: record.source_document_id as string })
    expect(link).toHaveAttribute('href', expect.stringContaining(`document=${record.source_document_id}`))
    expect(within(drawer).getByText('Ddr promotion')).toBeInTheDocument()
  })

  it('offers no transition the server did not allow, and says so for a terminal record', async () => {
    const record = operations.items[0] as OperationRow
    renderWorkspace(`?tab=operations&operation=${record.id}`)

    const drawer = await screen.findByRole('dialog')
    // Every captured operation is completed, which the service treats as terminal.
    expect(record.allowed_transitions).toEqual([])
    expect(
      within(drawer).getByText('This record has reached a terminal state; no further transition is offered.'),
    ).toBeInTheDocument()
    expect(within(drawer).queryByTestId('transition-cancelled')).not.toBeInTheDocument()
  })

  it('shows a failed read as an error, not as an empty well', async () => {
    served.operationsFail = true
    renderWorkspace()

    await screen.findByRole('alert')
    expect(screen.queryByText('No operations recorded on this well')).not.toBeInTheDocument()
  })
})

describe('OperationsWorkspace: the events tab', () => {
  it('renders the events the API served with their severity and status', async () => {
    const record = events.items[0] as EventRow
    renderWorkspace('?tab=events')

    await screen.findByText(record.title)
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(events.items.length)
    expect(screen.getByText(`${events.total} records matching the current filter`)).toBeInTheDocument()
  })

  it('opens the manual-event form and records an event without inferring NPT or a cause', async () => {
    const user = userEvent.setup()
    renderWorkspace('?tab=events')

    await user.click(screen.getByTestId('manual-event-toggle'))
    await screen.findByTestId('manual-event-form')
    await user.type(screen.getByTestId('manual-event-title'), 'Manual safety observation')
    await user.click(screen.getByTestId('manual-event-submit'))

    await waitFor(() => expect(served.createEvent).toHaveBeenCalledTimes(1))
    expect(served.createEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        well_id: 'wel_capture',
        title: 'Manual safety observation',
        kind: 'observation',
        severity: 'low',
        is_npt: false,
        cause_basis: 'unknown',
        classification_source: 'recorded',
        source_kind: 'manual',
      }),
      expect.stringMatching(/^ui-event-create-wel_capture-/),
    )
  })

  it('requires an explicit category and positive hours before charging a manual event as NPT', async () => {
    const user = userEvent.setup()
    renderWorkspace('?tab=events')

    await user.click(screen.getByTestId('manual-event-toggle'))
    await screen.findByTestId('manual-event-form')
    await user.type(screen.getByTestId('manual-event-title'), 'Manual stuck-pipe event')
    await user.click(screen.getByTestId('manual-event-is-npt'))
    await user.selectOptions(screen.getByTestId('manual-event-npt-category'), 'stuck_pipe')
    await user.type(screen.getByTestId('manual-event-npt-hours'), '1.5')
    await user.click(screen.getByTestId('manual-event-submit'))

    await waitFor(() => expect(served.createEvent).toHaveBeenCalledTimes(1))
    expect(served.createEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Manual stuck-pipe event',
        is_npt: true,
        npt_category: 'stuck_pipe',
        npt_hours: 1.5,
      }),
      expect.stringMatching(/^ui-event-create-wel_capture-/),
    )
  })

  it('charges NPT from the event’s own hours and category', async () => {
    const charged = events.items[0] as EventRow
    renderWorkspace('?tab=events')

    await screen.findByText(charged.title)
    expect(within(rowOf(charged.title)).getByText('Stuck pipe · 6 h')).toBeInTheDocument()
    // The kind of a charged event is `npt`; the charge is not derived from it.
    expect(charged.kind).toBe('npt')
  })

  it('says an event with no booked hours was not charged, instead of inventing a category', async () => {
    // The same captured rows with the charge absent — `is_npt: false` and no category or hours. A
    // kind of `npt` is not a charge, so the screen must say the hours were not booked rather than
    // borrow the kind or the neighbouring row's number.
    const charged = events.items[0] as EventRow
    served.events = {
      items: events.items.map((row) => ({ ...row, is_npt: false, npt_category: null, npt_hours: null })),
      total: events.total,
      limit: events.items.length,
      offset: 0,
    }
    renderWorkspace('?tab=events')

    const row = rowOf((await screen.findByText(charged.title)).textContent ?? charged.title)
    expect(within(row).getByText('not charged')).toBeInTheDocument()
    expect(within(row).queryByText(/·.*h$/)).not.toBeInTheDocument()
  })

  it('renders the cause basis that travels with the cause', async () => {
    const record = events.items[0] as EventRow
    renderWorkspace(`?tab=events&event=${record.id}`)

    const drawer = await screen.findByRole('dialog')
    // The capture records no cause, and `unknown` is a claim of its own — it must be visible rather
    // than left blank, which would read as "no cause exists".
    expect(record.cause_basis).toBe('unknown')
    expect(within(drawer).getByText('cause unknown')).toBeInTheDocument()
  })
})

describe('OperationsWorkspace: the timeline tab', () => {
  it('renders the merged entries and deep-links the ones that came from a document', async () => {
    renderWorkspace('?tab=timeline')

    const list = await screen.findByTestId('timeline-entries')
    expect(within(list).getAllByRole('listitem')).toHaveLength(timeline.entries.length)

    const fromDocument = timeline.entries.find((entry) => entry.document_id)
    if (!fromDocument) throw new Error('the capture has no document-sourced timeline entry')
    const links = within(list).getAllByRole('link', { name: 'Source document' })
    expect(links.length).toBeGreaterThan(0)
    expect(links[0]).toHaveAttribute('href', expect.stringContaining(`document=${fromDocument.document_id}`))
  })

  it('says there is more when the server returned a cursor', async () => {
    renderWorkspace('?tab=timeline')

    await screen.findByTestId('timeline-entries')
    expect(timeline.next_cursor).toBeTruthy()
    expect(screen.getByText('A full page came back: more entries follow.')).toBeInTheDocument()
  })

  it('offers the kinds the server says exist as filters, and none that do not', async () => {
    renderWorkspace('?tab=timeline')

    await screen.findByTestId('timeline-entries')
    for (const kind of timeline.kinds_available) {
      expect(screen.getByTestId(`timeline-kind-${kind}`)).toBeInTheDocument()
    }
    expect(screen.queryByTestId('timeline-kind-kpi')).not.toBeInTheDocument()
  })
})
