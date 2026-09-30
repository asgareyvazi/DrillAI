/**
 * Context, deep links and reloads — against the real API and the real database.
 *
 * Six statements, each of which the interface must be able to make truthfully:
 *
 * 1. **A deep link opens the thing it names, and a reload reproduces it.** The URL carries the
 *    selection (`?run=`, `?workflow=`, `?document=`), so what a link shows and what a reload shows are
 *    the same screen.
 * 2. **Changing the parameter changes the subject.** `?run=A` and `?run=B` are the same mounted page,
 *    so a change of run must change everything that belongs to a run — including the cursor the live
 *    stream resumes from. The previous run's position in *its* log is not a position in the new one.
 * 3. **A context mismatch fails safely.** A URL that names one well while the record belongs to
 *    another must not present that record as this well's: the well a document belongs to is read from
 *    the document itself, and the page says what it found instead of showing somebody else's data.
 * 4. **A deep link that names nothing says so.** `?document=…` for a document that does not exist, and
 *    `?workflow=…` for a workflow that is not in the caller's list, are not "nothing is selected" —
 *    the reader selected something, by URL, and the screen has to account for it.
 * 5. **A run's scope is historical.** The monitor shows the scope recorded on the run, not the context
 *    of whatever page the reader came from.
 * 6. **A workflow is not bound to a well.** A workflow can be opened and edited with no well in the
 *    URL at all; the well a run will execute against is chosen explicitly, at the moment of running.
 *
 * Nothing is intercepted. The documents, the runs and the workflow are the seeded ones, and every
 * comparison is against what the API returns for the same identifier.
 */

import {
  apiGet,
  clearPendingApproval,
  appConsoleErrors,
  expect,
  fixtures,
  startRunFromStudio,
  test,
  waitForLoaded,
} from './fixtures'

interface RunEnvelope {
  run: { id: string; well_id: string | null; status: string; workflow_id: string }
  events: Array<{ seq: number; run_id: string }>
}

test.describe('context, deep links and reloads', () => {
  test('a deep link to a run opens it, and a reload reproduces the same screen', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const workflowId = fixtures().workflow_id
    const runId = await startRunFromStudio(page, workflowId, wellId)

    // Away from the run entirely, then back to the list — the selection must come from the URL, not
    // from anything the studio left in memory.
    await page.goto('/runs')
    await waitForLoaded(page)
    await page.getByRole('row', { name: new RegExp(runId) }).click()
    await expect(page).toHaveURL(new RegExp(`run=${runId}`))
    await expect(page.getByText(new RegExp(`Run ${runId}`))).toBeVisible()

    // The reload is the same URL, so it is the same screen: the run is opened from the identifier in
    // the address, which is what makes a link to a run worth copying.
    await page.reload()
    await waitForLoaded(page)
    await expect(page).toHaveURL(new RegExp(`run=${runId}`))
    await expect(page.getByText(new RegExp(`Run ${runId}`))).toBeVisible()

    // And it is the run the server has, not a plausible reconstruction of it.
    const envelope = await apiGet<RunEnvelope>(request, `/runs/${runId}`, 'engineer')
    await expect(page.getByTestId('run-scope')).toContainText(envelope.run.well_id ?? '')
    expect(appConsoleErrors(consoleErrors)).toEqual([])

    // The suite shares one seeded database, and an approval left pending is not inert: it appears in
    // the inbox a later journey asserts on. A journey that starts runs closes the gates it opened.
    await clearPendingApproval(request, runId, 'e2e context journey: closed to leave the seeded state clean')
  })

  test('switching the run in the address re-reads that run, live', async ({
    page,
    request,
    wellId,
  }) => {
    const workflowId = fixtures().workflow_id
    const first = await startRunFromStudio(page, workflowId, wellId)
    const second = await startRunFromStudio(page, workflowId, wellId)

    // Open the second run, then go back to the first through the list: two different runs, one page.
    await page.goto(`/runs?run=${second}`)
    await waitForLoaded(page)
    await expect(page.getByText(new RegExp(`Run ${second}`))).toBeVisible()

    const sockets: string[] = []
    page.on('websocket', (socket) => sockets.push(socket.url()))

    await page.getByRole('row', { name: new RegExp(first) }).click()
    await expect(page.getByText(new RegExp(`Run ${first}`))).toBeVisible()

    // The stream for the newly selected run is opened for *that* run, and the events rendered are that
    // run's own. A cursor left over from the previous run would ask for a sequence the new run never
    // had and drop its whole log — the screen would look connected and show nothing.
    await expect
      .poll(() => sockets.filter((url) => url.includes(`/runs/${first}/events/stream`)).length, {
        timeout: 15_000,
      })
      .toBeGreaterThan(0)

    const envelope = await apiGet<RunEnvelope>(request, `/runs/${first}`, 'engineer')
    expect(envelope.events.length, 'the seeded workflow produces events for a run').toBeGreaterThan(0)
    // The log is one of the run's tabs, so the tab is opened first: reading `data-seq` off the rows is
    // reading what the operator sees, and a tab that was never opened is not on screen.
    const eventsTab = page.getByRole('tab', { name: /Event stream/i })
    if ((await eventsTab.getAttribute('aria-selected')) !== 'true') await eventsTab.click()
    await page.getByTestId('run-events').locator('li').first().waitFor({ state: 'visible' })
    const rendered = await page
      .getByTestId('run-events')
      .locator('li')
      .evaluateAll((rows) => rows.map((row) => row.getAttribute('data-seq')))
    const fromApi = envelope.events.map((event) => String(event.seq))
    // Every sequence the server holds for this run is on screen. The list is the run's log, not a
    // mixture with the run that was open a moment ago.
    expect(fromApi.filter((seq) => !rendered.includes(seq))).toEqual([])

    // Both runs this journey started are closed, so the shared approval inbox is as it was found.
    for (const runId of [first, second]) {
      await clearPendingApproval(request, runId, 'e2e context journey: closed to leave the seeded state clean')
    }
  })

  test('a document that belongs to another well is not shown as this well’s', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const documentId = fixtures().document_ids[0]
    const document = await apiGet<{ document: { id: string; well_id: string; title: string } }>(
      request,
      `/documents/${documentId}`,
      'engineer',
    )
    expect(document.document.well_id, 'the seeded document belongs to the seeded well').toBe(
      fixtures().well_id,
    )

    // The address says one well; the record belongs to another. Nothing about the document may be
    // presented under the wrong well, and the screen must say which of the two facts it found.
    await page.goto(`/wells/wel_not_a_well/documents?document=${documentId}`)
    await waitForLoaded(page)

    const mismatch = page.getByTestId('document-other-well')
    await expect(mismatch).toBeVisible()
    await expect(mismatch).toContainText(documentId)
    await expect(mismatch).toContainText(document.document.well_id)
    // The document itself is nowhere on the page: no title, no extraction card, no evidence.
    await expect(page.getByText(document.document.title)).toHaveCount(0)
    await expect(page.getByText(/Extraction|provenance|evidence links/i)).toHaveCount(0)
    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('a document id that names nothing is reported, not shown as “nothing selected”', async ({
    page,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/documents?document=doc_does_not_exist`)
    await waitForLoaded(page)

    const absent = page.getByTestId('document-not-in-list')
    await expect(absent).toBeVisible()
    await expect(absent).toContainText('doc_does_not_exist')
    // The distinction this pins: the reader *did* select something — by URL — and telling them to
    // "select a document" would ignore that.
    await expect(page.getByText(/select a document to inspect/i)).toHaveCount(0)
  })

  test('a workflow id that is not in the list is reported, and a workflow is not bound to a well', async ({
    page,
    consoleErrors,
  }) => {
    await page.goto('/workflows?workflow=wfl_not_a_workflow')
    await waitForLoaded(page)

    const absent = page.getByTestId('workflow-not-in-list')
    await expect(absent).toBeVisible()
    await expect(absent).toContainText('wfl_not_a_workflow')
    await expect(page.getByText(/select or create a workflow to edit its graph/i)).toHaveCount(0)

    // With the real workflow, and with no well anywhere in the URL: the studio opens, because a
    // workflow definition is not scoped to a well. The well a run executes against is chosen here,
    // explicitly, at the moment of running.
    await page.goto(`/workflows?workflow=${fixtures().workflow_id}`)
    await waitForLoaded(page)
    expect(new URL(page.url()).pathname).toBe('/workflows')
    await expect(page.getByLabel('Run context')).toBeVisible()
    await expect(page.getByRole('button', { name: /start run/i })).toBeVisible()
    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('a run that does not exist reads as not found, and offers no false retry', async ({ page }) => {
    // The identity is the session's development default; the read is refused by nobody, the row simply
    // is not there — which is the state this journey is about.
    await page.goto('/runs?run=run_does_not_exist')
    await waitForLoaded(page)

    const state = page.getByTestId('error-state').first()
    await expect(state).toBeVisible()
    await expect(state).toHaveAttribute('data-error-kind', 'not_found')
    await expect(state).toHaveAttribute('data-http-status', '404')
    // Asking again cannot make the row exist; the address is what has to change.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)
  })
})
