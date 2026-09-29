/**
 * Journey 5 — the live event stream, in a real browser, against a real run.
 *
 * The unit suites prove the client's rules with a deterministic socket; this file proves the thing
 * they cannot: that a browser talking to this server, over this URL, with this handshake, receives the
 * events a real run produces and reconstructs the same history the API holds.
 *
 * The disconnect is forced through Playwright's WebSocket routing, which is the important detail. The
 * socket under test is still the real one, to the real backend — the harness only gets to drop it, the
 * way a flaky network would. Nothing here simulates the *server*: the events that arrive after the
 * drop were produced by the server while the browser was not listening.
 *
 * The journey is deliberately sequenced so the reconnect has something to recover:
 *
 *   1. a run stops at its approval gate; the monitor streams it and reports `live`;
 *   2. the socket is dropped while every event so far is on screen;
 *   3. an operator decides the approval in another identity, so the server resumes and finishes the
 *      run — appending events the page never saw;
 *   4. the client reconnects, states its cursor, and receives exactly the events it missed;
 *   5. the page must end up showing exactly the API's log: no gap, no repeat, and the terminal status
 *      the API reports.
 */

import {
  apiGet,
  apiPost,
  clearPendingApproval,
  fixtures,
  selectRole,
  startRunFromStudio,
  test,
  expect,
  waitForLoaded,
} from './fixtures'
import type { Page } from '@playwright/test'

type RunEventRow = { id: string; run_id: string; seq: number; type: string; message: string | null }

type RunEnvelope = {
  run: { id: string; status: string; pending_approval_id: string | null }
  events: RunEventRow[]
  pending_approval?: { id: string; title: string } | null
}

type Approval = { id: string; status: string }

/** One connection the page opened, as the harness saw it. */
interface ObservedSocket {
  url: string
  /** Drop this connection the way a network would: the page sees the socket close unexpectedly. */
  drop: (code?: number) => Promise<void>
  closed: Promise<{ code: number | undefined; reason: string | undefined }>
}

/**
 * Watch every event-stream socket the page opens, without standing in for the server.
 *
 * `routeWebSocket` intercepts the connection, connects it to the real backend and forwards in both
 * directions automatically; the only thing this does is keep a handle, so a test can state exactly
 * when the transport failed.
 */
async function observeStreamSockets(page: Page): Promise<ObservedSocket[]> {
  const observed: ObservedSocket[] = []
  await page.routeWebSocket(/\/runs\/[^/]+\/events\/stream/, (ws) => {
    const server = ws.connectToServer()
    const closed = new Promise<{ code: number | undefined; reason: string | undefined }>((resolve) => {
      ws.onClose((code, reason) => {
        // Not forwarded by default once a handler is set: the page-side close must reach the server,
        // and the server-side close must reach the page, or this would be a mock rather than a relay.
        void server.close({ code, reason })
        resolve({ code, reason })
      })
      server.onClose(async (code, reason) => {
        await ws.close({ code, reason })
        resolve({ code, reason })
      })
    })
    observed.push({
      url: ws.url(),
      drop: async (code = 1001) => {
        await ws.close({ code, reason: 'e2e: the transport dropped' })
      },
      closed,
    })
  })
  return observed
}

function streamCursor(url: string): number {
  return Number(new URL(url).searchParams.get('after_seq') ?? '-1')
}

/**
 * The sequences the page is showing in its event log, in the order it renders them.
 *
 * The log is one of the run's tabs, so the tab is opened first — reading `data-seq` off the rows is
 * reading what the operator sees, not an internal list.
 */
async function displayedSequences(page: Page): Promise<string[]> {
  const eventsTab = page.getByRole('tab', { name: /Event stream|جریان/i })
  if ((await eventsTab.getAttribute('aria-selected')) !== 'true') {
    await eventsTab.click()
  }
  const rows = page.getByTestId('run-events').locator('li')
  await rows.first().waitFor({ state: 'visible' })
  return rows.evaluateAll((elements) => elements.map((element) => element.getAttribute('data-seq') ?? ''))
}

function envelopeEvents(request: Parameters<typeof apiGet>[1], runId: string, role: Parameters<typeof apiGet>[2]) {
  return apiGet<RunEnvelope>(request, `/runs/${runId}`, role)
}

test.describe('run monitor: live events over the real socket', () => {
  test('the socket opens, streams, survives a drop, and ends up equal to the API log', async ({
    page,
    request,
    consoleErrors,
    wellId,
  }) => {
    const sockets = await observeStreamSockets(page)

    const runId = await startRunFromStudio(page, fixtures().workflow_id, wellId)

    // 1. The run parked at its gate and the monitor is streaming it.
    const parked = await envelopeEvents(request, runId, 'supervisor')
    expect(parked.run.status).toBe('waiting_approval')
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'live')

    const parkedSequences = parked.events.map((row) => row.seq)
    expect(parkedSequences.length).toBeGreaterThan(5)
    // The cursor the UI reports is the server's own last sequence, not an array length.
    await expect(page.getByTestId('run-stream-cursor')).toHaveText(`seq ${Math.max(...parkedSequences)}`)

    // Every durable event this run has so far is on screen, in the server's order.
    const before = await displayedSequences(page)
    expect(before.map(Number)).toEqual(parkedSequences)
    expect(sockets.length, 'the page must have opened the stream').toBeGreaterThan(0)

    // 2. The transport fails — for real: the page's socket is closed from under it.
    const first = sockets[0] as ObservedSocket
    expect(new URL(first.url).pathname).toContain(`/runs/${runId}/events/stream`)
    await first.drop()

    // The indicator says what happened, and does not blame the backend: REST is still answering and
    // the saved history is still on screen.
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'reconnecting')
    await expect(page.getByTestId('run-stream-status')).toContainText('Reconnecting')
    await expect(page.getByTestId('run-status')).toHaveText(/waiting approval/i)
    expect(await displayedSequences(page)).toEqual(before)

    // 3. While nobody is listening, the run moves: the approval is decided by another identity and the
    //    server resumes and finishes it, appending events this page has never seen.
    const approval = parked.pending_approval
    expect(approval, 'the parked run must have an approval to decide').toBeTruthy()
    const decided = await apiPost<{ approval: Approval; resumed_run: { status: string } }>(
      request,
      `/approvals/${approval?.id}/decide`,
      { decision: 'approved', note: 'Streamed journey: decided while the socket was down.', resume: true },
      'wellManager',
    )
    expect(decided.approval.status).toBe('approved')

    const finished = await envelopeEvents(request, runId, 'supervisor')
    expect(finished.run.status).toBe('succeeded')
    const finishedSequences = finished.events.map((row) => row.seq)
    expect(finishedSequences.length).toBeGreaterThan(parkedSequences.length)
    expect(finishedSequences).toEqual(parkedSequences.concat(finishedSequences.slice(parkedSequences.length)))
    expect(new Set(finishedSequences).size).toBe(finishedSequences.length)

    // 4. The client reconnects on its own, stating the sequence it actually applied.
    await expect
      .poll(() => sockets.length, { message: 'the client must reconnect without being told to' })
      .toBeGreaterThan(1)
    const resumed = sockets[1] as ObservedSocket
    expect(streamCursor(resumed.url)).toBe(Math.max(...parkedSequences))

    // 5. The page converges on the API: every event, once, in order — including the ones produced
    //    while the socket was down — and the terminal status the server reports.
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'closed')
    await expect(page.getByTestId('run-status')).toHaveText(/succeeded/i)
    await expect
      .poll(async () => (await displayedSequences(page)).map(Number))
      .toEqual(finishedSequences)

    const shown = await displayedSequences(page)
    expect(shown, 'no event may be shown twice').toEqual([...new Set(shown)])
    expect(shown.length).toBe(finishedSequences.length)

    // 6. A reload reconstructs the same history from REST, because the history was never the socket's.
    await page.reload()
    await waitForLoaded(page)
    await expect(page.getByTestId('run-status')).toHaveText(/succeeded/i)
    // `displayedSequences` opens the event tab itself and reads the rows, so this is the same check a
    // person makes: reload, open the log, compare it with the API.
    expect((await displayedSequences(page)).map(Number)).toEqual(finishedSequences)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('leaving the run closes the socket instead of leaving it watching', async ({
    page,
    request,
    consoleErrors,
    wellId,
  }) => {
    const sockets = await observeStreamSockets(page)
    const runId = await startRunFromStudio(page, fixtures().workflow_id, wellId)
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'live')
    const first = sockets[0] as ObservedSocket

    // Leave the monitor through a link it renders itself: RunDetailView unmounts, so its stream must
    // end. (A full page load would close the socket by destroying the document, which proves less.)
    await page.getByRole('link', { name: /Workflow studio|استودیو/i }).first().click()
    await waitForLoaded(page)
    await expect(page).toHaveURL(/\/workflows/)

    const closed = await Promise.race([
      first.closed,
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 5000)),
    ])
    expect(closed, 'the client must close its socket when the run monitor goes away').not.toBeNull()
    // And it must not keep opening new ones for a screen that is no longer there.
    await page.waitForTimeout(1500)
    expect(sockets.length).toBe(1)

    await clearPendingApproval(request, runId, 'Cleanup after the closed-socket journey.')
    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('changing identity replaces the socket rather than reusing another principal’s stream', async ({
    page,
    request,
    consoleErrors,
    wellId,
  }) => {
    const sockets = await observeStreamSockets(page)
    const runId = await startRunFromStudio(page, fixtures().workflow_id, wellId)
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'live')

    await selectRole(page, 'viewer')

    await expect
      .poll(() => sockets.length, { message: 'the new identity must open its own stream' })
      .toBeGreaterThan(1)
    const closed = await Promise.race([
      (sockets[0] as ObservedSocket).closed,
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 5000)),
    ])
    expect(closed, 'the previous identity’s socket must be closed, not reused').not.toBeNull()

    // The run is still the run: a viewer may read it, and the page says so from REST.
    const shownRunId = new URL(page.url()).searchParams.get('run') ?? ''
    expect(shownRunId).toBe(runId)
    const envelope = await envelopeEvents(request, shownRunId, 'viewer')
    expect(envelope.run.status).toBe('waiting_approval')

    await clearPendingApproval(request, runId, 'Cleanup after the identity-switch journey.')
    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('moving to another run closes the first socket and never mixes the two logs', async ({
    page,
    request,
    consoleErrors,
    wellId,
  }) => {
    const sockets = await observeStreamSockets(page)

    // Run A: parks at its gate and starts streaming.
    const runA = await startRunFromStudio(page, fixtures().workflow_id, wellId)
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'live')
    const logA = (await envelopeEvents(request, runA, 'supervisor')).events.map((row) => row.seq)

    // Run B: the same definition, a different run, selected within the same screen.
    const runB = await startRunFromStudio(page, fixtures().workflow_id, wellId)
    expect(runB).not.toBe(runA)
    await expect(page.getByTestId('run-stream-cursor')).toBeVisible()

    // A's stream ends when the screen moves on, and B's stream is B's.
    await expect
      .poll(() => sockets.length, { message: 'the second run must open its own stream' })
      .toBeGreaterThan(1)
    const openForB = sockets.filter((socket) => socket.url.includes(runB))
    expect(openForB.length, "B's socket must name B").toBeGreaterThan(0)
    const closedA = await Promise.race([
      (sockets[0] as ObservedSocket).closed,
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 5000)),
    ])
    expect(closedA, "run A's socket must be closed, not left attached to another run").not.toBeNull()

    // B's log is B's: the events of A cannot appear on this page, however similar the two runs are.
    const eventsB = (await envelopeEvents(request, runB, 'supervisor')).events.map((row) => row.seq)
    await expect.poll(async () => (await displayedSequences(page)).map(Number)).toEqual(eventsB)
    const shown = (await displayedSequences(page)).map(Number)
    expect(shown.length).toBeLessThan(logA.length + eventsB.length)
    expect(shown).toEqual([...new Set(shown)])

    await clearPendingApproval(request, runA, 'Cleanup after the run-switch journey (A).')
    await clearPendingApproval(request, runB, 'Cleanup after the run-switch journey (B).')
    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a run that has already finished is not streamed at all', async ({ page, request, consoleErrors, wellId }) => {
    const sockets = await observeStreamSockets(page)
    const runId = await startRunFromStudio(page, fixtures().workflow_id, wellId)

    // Leave the monitor before the run can finish. Starting a run navigates to the run, so the screen
    // is watching a run that is about to end: its socket opens when the read that said "parked" was
    // answered, which can be *after* the moment the run finishes. A count taken then would be that
    // screen's socket, and the assertion below would be about the wrong page. On the list no run is
    // open, so nothing is streaming and nothing is pending.
    await page.goto('/runs')
    await waitForLoaded(page)

    // Finish the run from the API, then open the monitor on it: there is nothing left to listen for.
    const parked = await envelopeEvents(request, runId, 'supervisor')
    await apiPost(
      request,
      `/approvals/${parked.pending_approval?.id}/decide`,
      { decision: 'approved', note: 'Finished before the page opened.', resume: true },
      'wellManager',
    )
    const finished = await envelopeEvents(request, runId, 'supervisor')
    expect(finished.run.status).toBe('succeeded')

    // What matters is what this load does, on a run that cannot change any more.
    const socketsBefore = sockets.length
    await page.goto(`/runs?run=${runId}`)
    await waitForLoaded(page)
    await expect(page.getByTestId('run-status')).toHaveText(/succeeded/i)
    await expect(page.getByTestId('run-stream-status')).toHaveAttribute('data-stream-state', 'idle')
    // A socket that opened late would still be a socket, so the page is given time to open one before
    // the count is compared. The register only grows, so nothing can open and go unnoticed.
    await page.waitForTimeout(1500)
    expect(sockets.length, 'a finished run needs no socket').toBe(socketsBefore)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
