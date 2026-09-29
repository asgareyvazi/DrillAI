/**
 * Run Monitor against the payloads the backend actually serves.
 *
 * The fixtures are captured from the running API (see `src/test/fixtures/README.md`), so this file
 * fails when the monitor reads a field the server does not send — which is how it previously showed
 * the node id instead of `node_name`, missed the run envelope entirely, and sent the decision note
 * under a name the API silently ignores.
 *
 * The behaviours asserted here are the ones a person relying on the run monitor depends on:
 *   * the run's own scope is shown, and a value the server did not return is named as such;
 *   * a run parked at an approval says so, and shows what the approver needs to decide;
 *   * the decision note goes out as `note` with conditions as a list, and a recorded note is visible
 *     afterwards because the server returned it;
 *   * a rejection carries a reason;
 *   * the primary statuses render distinguishably.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { drillingApi } from '../../api/endpoints'
import type { RunDetail, RunSummary } from '../../api/types'
import { I18nProvider } from '../../i18n'
import RunMonitor from './RunMonitor'
import approvedListFixture from '../../test/fixtures/approvals-list-any.json'
import pendingListFixture from '../../test/fixtures/approvals-list.json'
import runSucceededFixture from '../../test/fixtures/run-succeeded.json'
import runWaitingFixture from '../../test/fixtures/run-waiting-approval.json'
import runsListFixture from '../../test/fixtures/runs-list.json'

/**
 * The stream is the one thing a jsdom component test cannot drive honestly — there is no server and
 * no socket to connect to — so it is replaced here, and the *real* class is exercised against a
 * deterministic socket by `src/lib/runEvents.test.ts` and `src/hooks/useRunEventStream.test.tsx`.
 * What this file checks about it is what the screen renders from its state.
 */
const streamControls = vi.hoisted(() => {
  const instances: Array<{ options: Record<string, unknown>; connect: () => void; dispose: () => void }> = []
  return {
    instances,
    reset: () => {
      instances.length = 0
    },
  }
})

vi.mock('../../lib/runEvents', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/runEvents')>()
  class FakeRunEventStream {
    private status = {
      state: 'idle' as const,
      runId: '',
      cursor: 0,
      runStatus: null,
      closedStatus: null,
      attempts: 0,
      closeCode: null,
      lastError: null,
    }

    constructor(readonly options: Record<string, unknown>) {
      streamControls.instances.push(this as unknown as (typeof streamControls.instances)[number])
    }

    getStatus() {
      return { ...this.status, runId: String(this.options.runId ?? '') }
    }

    connect(): void {
      // The screen connected: report the state an honest transport would report while the handshake
      // is in flight, and let each test move it on from there.
      this.emit({ state: 'connecting' })
    }

    dispose(): void {
      this.emit({ state: 'closed' })
    }

    /** Used by the tests below to act like the server. */
    emit(patch: Record<string, unknown>): void {
      this.status = { ...this.status, ...patch, runId: String(this.options.runId ?? '') } as typeof this.status
      ;(this.options.onStatus as ((status: unknown) => void) | undefined)?.(this.getStatus())
    }

    frame(decode: unknown): void {
      ;(this.options.onFrame as ((frame: unknown) => void) | undefined)?.(decode)
    }

    terminal(status: string): void {
      ;(this.options.onTerminal as ((status: string) => void) | undefined)?.(status)
    }
  }
  return { ...actual, RunEventStream: FakeRunEventStream }
})

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    listRuns: vi.fn(),
    listApprovals: vi.fn(),
    getRun: vi.fn(),
    decideApproval: vi.fn(),
    resumeRun: vi.fn(),
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
const waiting = runWaitingFixture as unknown as RunDetail
const succeeded = runSucceededFixture as unknown as RunDetail
const runsPage = runsListFixture as unknown as { items: RunSummary[]; total: number; limit: number; offset: number }
const approvals = (approvedListFixture as unknown as { items: Array<Record<string, unknown>> }).items

/**
 * Reads a fixture entry that must exist.
 *
 * `noUncheckedIndexedAccess` is on for this repository, which is the right setting: an index into a
 * fixture is a place where a silently missing row would turn an assertion into a no-op. Failing
 * loudly here is the point.
 */
function at<T>(rows: T[], index: number, what: string): T {
  const value = rows[index]
  if (value === undefined) throw new Error(`fixture ${what}[${index}] is missing`)
  return value
}

function renderMonitor(initialUrl = '/runs') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const node: ReactNode = (
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[initialUrl]}>
          <RunMonitor />
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>
  )
  return render(node)
}

/** The run's own approvals, as the run monitor asks for them: `run_id` + `status=any`. */
function approvalsForRun(items: unknown[]) {
  return { items, total: items.length, limit: 50, offset: 0 }
}

beforeEach(() => {
  vi.clearAllMocks()
  streamControls.reset()
  api.listRuns.mockResolvedValue(runsPage)
  api.getRun.mockResolvedValue(waiting)
  // One mock that answers the way the API does: scoped by run when a run is being shown, otherwise
  // filtered by the status the inbox asked for.
  api.listApprovals.mockImplementation((params) => {
    if (params?.run_id) {
      return Promise.resolve(
        approvalsForRun(params.run_id === waiting.run.id ? [waiting.pending_approval!] : []) as never,
      )
    }
    return Promise.resolve((params?.status === 'any' ? approvedListFixture : pendingListFixture) as never)
  })
})

describe('run monitor: the run list', () => {
  it('shows the runs the server returned, with the scope each run actually has', async () => {
    renderMonitor()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(runsPage.items.length + 1) // header + one row per run

    const first = at(runsPage.items, 0, 'runs')
    const row = at(rows, 1, 'rendered rows')
    expect(within(row).getByText(first.id)).toBeInTheDocument()
    expect(within(row).getByText(first.workflow_key as string)).toBeInTheDocument()
    expect(within(row).getByText(first.well_id as string)).toBeInTheDocument()
    expect(within(row).getByText(first.trigger_type)).toBeInTheDocument()
  })

  it('names a scope the run does not have instead of leaving a blank cell', async () => {
    // A synthetic variant of the captured payload: same run, with the scope fields the API returns
    // as null when a run was started without a well. This is the branch where a page is most likely
    // to borrow a well from somewhere else.
    api.listRuns.mockResolvedValue({
      ...runsPage,
      items: [{ ...at(runsPage.items, 0, 'runs'), well_id: null, wellbore_id: null, section_id: null }],
    })
    renderMonitor()
    const table = await screen.findByRole('table')
    const row = at(within(table).getAllByRole('row'), 1, 'rendered rows')
    expect(within(row).getByText('—')).toBeInTheDocument()
    expect(
      within(row).queryByText(at(runsPage.items, 0, 'runs').well_id as string),
    ).not.toBeInTheDocument()
  })

  it('opens a run from its row and keeps it in the URL', async () => {
    renderMonitor()
    const table = await screen.findByRole('table')
    await userEvent.click(at(within(table).getAllByRole('row'), 1, 'rendered rows'))
    await waitFor(() =>
      expect(api.getRun).toHaveBeenCalledWith(at(runsPage.items, 0, 'runs').id, expect.any(AbortSignal)),
    )
  })
})

describe('run monitor: a run waiting for a human', () => {
  const url = `/runs?run=${waiting.run.id}`

  it('reconstructs the paused state from the run envelope, not from React memory', async () => {
    renderMonitor(url)
    expect(await screen.findByTestId('run-status')).toHaveTextContent(/waiting approval/i)
    expect(screen.getByTestId('run-awaiting-approval')).toBeInTheDocument()
    // The state came from the server: a reload is another fetch of the same envelope.
    expect(api.getRun).toHaveBeenCalledWith(waiting.run.id, expect.any(AbortSignal))
  })

  it('shows the scope the run was started against, naming what was not returned', async () => {
    renderMonitor(url)
    const scope = await screen.findByTestId('run-scope')
    expect(within(scope).getByText(waiting.run.well_id as string)).toBeInTheDocument()
    // The captured run has no project, wellbore, section or operation; those are named, not blank.
    expect(within(scope).getAllByText('None').length).toBe(4)
  })

  it('shows the node that is waiting, by the name the server gave it', async () => {
    renderMonitor(url)
    const pausedNode = waiting.node_runs.find((row) => row.status === 'waiting_approval')
    expect(pausedNode).toBeDefined()
    expect(await screen.findByText(pausedNode!.node_name as string)).toBeInTheDocument()
    // The status is shown as words, not as the wire value with underscores in it.
    expect(screen.getAllByText(/waiting approval/i).length).toBeGreaterThan(0)
  })

  it('shows the event log in the order the server recorded it', async () => {
    renderMonitor(url)
    await userEvent.click(await screen.findByRole('tab', { name: /event/i }))
    const events = await screen.findAllByTestId('run-event')
    const sequences = events.map((node) => Number(node.getAttribute('data-seq')))
    expect(sequences.length).toBeGreaterThan(2)
    expect([...sequences].sort((a, b) => a - b)).toEqual(sequences)
  })

  it('tells the approver what they are deciding, and blocks resuming an undecided approval', async () => {
    renderMonitor(url)
    const card = await screen.findByTestId('approval-card')
    const approval = waiting.pending_approval!
    expect(within(card).getByText(approval.title)).toBeInTheDocument()
    expect(within(card).getByText(String(approval.description))).toBeInTheDocument()
    expect(within(card).getAllByText(/L3/).length).toBeGreaterThan(0)
    expect(within(card).getByText(String(approval.required_role))).toBeInTheDocument()
    // Shown as the requester's risk note (and again inside the request payload, which is why this
    // asserts presence rather than uniqueness).
    expect(within(card).getAllByText(new RegExp(String(approval.risk_notes))).length).toBeGreaterThan(0)
    expect(screen.getByTestId('resume-run')).toBeDisabled()
  })

  it('sends the decision note as `note` and the conditions as a list', async () => {
    api.decideApproval.mockResolvedValue({
      approval: { ...waiting.pending_approval!, status: 'approved' },
      resumed_run: succeeded.run,
    })
    renderMonitor(url)

    await userEvent.type(await screen.findByTestId('approval-note'), 'Checked against the DDR')
    await userEvent.type(screen.getByTestId('approval-conditions'), 'Re-issue if NPT changes')
    await userEvent.click(screen.getByTestId('approval-approve'))

    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledTimes(1))
    const call = api.decideApproval.mock.calls[0]
    if (call === undefined) throw new Error('decideApproval was not called')
    const [approvalId, body] = call
    expect(approvalId).toBe(waiting.pending_approval!.id)
    expect(body).toMatchObject({
      decision: 'approved',
      note: 'Checked against the DDR',
      conditions: ['Re-issue if NPT changes'],
      resume: true,
    })
    // The name the API ignores must not appear: the justification was silently dropped when it did.
    expect(body).not.toHaveProperty('comment')
  })

  it('requires a reason before a rejection can be recorded', async () => {
    renderMonitor(url)
    const reject = await screen.findByTestId('approval-reject')
    expect(reject).toBeDisabled()
    expect(screen.getByText(/needs a reason/i)).toBeInTheDocument()

    await userEvent.type(screen.getByTestId('approval-note'), 'NPT attribution is wrong')
    await waitFor(() => expect(reject).toBeEnabled())
    await userEvent.click(reject)
    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledTimes(1))
    expect(api.decideApproval.mock.calls[0]?.[1]).toMatchObject({ decision: 'rejected' })
  })
})

describe('run monitor: after a decision', () => {
  it('keeps the decision on the run after it is taken, note and conditions included', async () => {
    const decided = at(approvals, 0, 'approvals')
    api.getRun.mockResolvedValue({ ...succeeded, pending_approval: null })
    api.listApprovals.mockImplementation((params) =>
      Promise.resolve(
        approvalsForRun(params?.run_id ? (approvedListFixture as { items: unknown[] }).items : []) as never,
      ),
    )
    renderMonitor(`/runs?run=${succeeded.run.id}`)

    expect(await screen.findByTestId('run-status')).toHaveTextContent(/succeeded/i)
    // The decision is part of the run's record, not only of the inbox: this is what survives a reload.
    const record = await screen.findByTestId('approval-record')
    expect(within(record).getByText(String(decided.decision_note))).toBeInTheDocument()
    expect(within(record).getByText(String(decided.decided_by))).toBeInTheDocument()
    expect(within(record).getByText(String((decided.conditions as string[])[0]))).toBeInTheDocument()
    // A decided approval is not offered for decision again.
    expect(within(record).queryByTestId('approval-approve')).not.toBeInTheDocument()
  })

  it('scopes the approval read to the run being shown', async () => {
    api.getRun.mockResolvedValue({ ...succeeded, pending_approval: null })
    renderMonitor(`/runs?run=${succeeded.run.id}`)
    await screen.findByTestId('run-status')
    await waitFor(() =>
      expect(api.listApprovals).toHaveBeenCalledWith(
        { run_id: succeeded.run.id, status: 'any' },
        expect.any(AbortSignal),
      ),
    )
  })

  it('shows no approval card on a run that has none', async () => {
    api.getRun.mockResolvedValue({ ...succeeded, pending_approval: null })
    renderMonitor(`/runs?run=${succeeded.run.id}`)
    expect(await screen.findByTestId('run-status')).toHaveTextContent(/succeeded/i)
    expect(screen.queryByTestId('approval-card')).not.toBeInTheDocument()
  })

  it('lets the inbox show decisions that were already taken', async () => {
    renderMonitor()
    await userEvent.click(await screen.findByRole('tab', { name: /approvals/i }))
    await screen.findByTestId('approval-card')
    // The filter is what makes the audit trail reachable; pending stays the default.
    const filter = screen.getByLabelText('Approval status')
    await userEvent.selectOptions(filter, 'any')
    await waitFor(() => expect(api.listApprovals).toHaveBeenCalledWith({ status: 'any' }, expect.any(AbortSignal)))
    const decided = at(approvals, 0, 'approvals')
    expect(await screen.findByText(String(decided.decision_note))).toBeInTheDocument()
  })

  it('renders the primary statuses distinguishably', async () => {
    // Three states derived from the captured run: what a monitor must never do is show them alike.
    const base = at(runsPage.items, 0, 'runs')
    api.listRuns.mockResolvedValue({
      ...runsPage,
      total: 3,
      items: [
        { ...base, id: 'run_a', status: 'succeeded' },
        { ...base, id: 'run_b', status: 'waiting_approval' },
        { ...base, id: 'run_c', status: 'failed' },
      ],
    })
    renderMonitor()
    const table = await screen.findByRole('table')
    const labels = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.querySelector('td:nth-child(4)')?.textContent ?? '')
    expect(labels).toEqual(['Succeeded', 'Waiting approval', 'Failed'])
    const tones = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.querySelector('td:nth-child(4) span')?.className ?? '')
    expect(new Set(tones).size).toBe(3)
  })
})

/**
 * The live transport, as the screen reports it.
 *
 * The indicator is the only part of this page that makes a claim about the *connection* rather than
 * about the run, so it is the part that must never overstate: "live" only once the protocol opened,
 * a dropped socket named as a dropped socket (REST is still working, and the saved history is still
 * on screen), and a refusal never presented as something to wait for.
 */
describe('run monitor: the live event stream', () => {
  const liveRun: RunDetail = {
    ...succeeded,
    run: { ...succeeded.run, status: 'running' },
  }
  const streamFor = () =>
    streamControls.instances[streamControls.instances.length - 1] as unknown as {
      emit: (patch: Record<string, unknown>) => void
      frame: (decode: unknown) => void
      terminal: (status: string) => void
      options: Record<string, unknown>
    }

  it('listens on a run that is waiting for a human, and says so', async () => {
    api.getRun.mockResolvedValue(waiting)
    renderMonitor('/runs?run=run_1')

    await waitFor(() => expect(screen.getByTestId('run-status')).toHaveTextContent('Waiting approval'))
    // A parked run can still change — a decision taken in the inbox, a resume by another operator —
    // so the page listens rather than waiting for somebody to reload it.
    await waitFor(() => expect(streamControls.instances).toHaveLength(1))

    act(() => streamFor().emit({ state: 'live', cursor: 12 }))
    await waitFor(() => expect(screen.getByTestId('run-stream-cursor')).toHaveTextContent('seq 12'))
  })

  it('does not listen on a run that has already finished', async () => {
    api.getRun.mockResolvedValue(succeeded)
    renderMonitor('/runs?run=run_1')

    await waitFor(() => expect(screen.getByTestId('run-status')).toHaveTextContent('Succeeded'))
    expect(streamControls.instances).toHaveLength(0)
    expect(screen.getByTestId('run-stream-status')).toHaveTextContent('Not streamed')
  })

  it('says it is connecting before it says it is live', async () => {
    api.getRun.mockResolvedValue(liveRun)
    renderMonitor('/runs?run=run_1')

    await waitFor(() => expect(streamControls.instances).toHaveLength(1))
    await waitFor(() => expect(screen.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'connecting'))
    expect(screen.getByTestId('run-stream-status')).toHaveTextContent('Connecting…')
  })

  it('reports live with the cursor it has actually applied', async () => {
    api.getRun.mockResolvedValue(liveRun)
    renderMonitor('/runs?run=run_1')
    await waitFor(() => expect(streamControls.instances).toHaveLength(1))

    act(() => streamFor().emit({ state: 'live', cursor: 27 }))

    await waitFor(() => expect(screen.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'live'))
    expect(screen.getByTestId('run-stream-status')).toHaveTextContent('Live')
    // "seq 27" is evidence; "Live" on its own is only a claim.
    expect(screen.getByTestId('run-stream-cursor')).toHaveTextContent('seq 27')
  })

  it('names a dropped connection as a dropped connection, not as an unreachable backend', async () => {
    api.getRun.mockResolvedValue(liveRun)
    renderMonitor('/runs?run=run_1')
    await waitFor(() => expect(streamControls.instances).toHaveLength(1))

    act(() => streamFor().emit({ state: 'disconnected', lastError: 'the connection failed' }))

    await waitFor(() =>
      expect(screen.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'disconnected'),
    )
    const badge = screen.getByTestId('run-stream-status')
    expect(badge).toHaveTextContent('Realtime disconnected')
    expect(badge).toHaveTextContent('showing saved history')
    expect(badge).not.toHaveTextContent(/unreachable/i)
    // The run itself is still on screen: REST answered, and history is not the transport's to lose.
    expect(screen.getByTestId('run-status')).toHaveTextContent('Running')
  })

  it('keeps a server-side stream error visible instead of hiding it', async () => {
    api.getRun.mockResolvedValue(liveRun)
    renderMonitor('/runs?run=run_1')
    await waitFor(() => expect(streamControls.instances).toHaveLength(1))

    act(() => streamFor().emit({ state: 'live', lastError: 'the log could not be read' }))

    await waitFor(() => expect(screen.getByTestId('run-stream-error')).toHaveTextContent('the log could not be read'))
  })

  it('does not poll: the run is fetched once and then carried by the stream', async () => {
    vi.useFakeTimers()
    try {
      api.getRun.mockResolvedValue(liveRun)
      renderMonitor('/runs?run=run_1')

      // Time is advanced inside `act` and nothing waits on real timers, so the assertion below is
      // about elapsed time, not about how fast the machine is.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50)
      })
      expect(api.getRun).toHaveBeenCalledTimes(1)

      // The stopgap this replaces asked the server every 2 seconds. Nine seconds must now produce
      // exactly no further requests: the socket is the transport.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(9000)
      })
      expect(api.getRun).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
