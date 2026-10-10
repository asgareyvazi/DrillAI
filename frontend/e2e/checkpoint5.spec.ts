/**
 * Checkpoint 5, certified together: identity, context, direction and accessibility in one pass.
 *
 * The four batches each proved their own area. This spec is the integrated evidence the checkpoint
 * asks for: the areas are exercised *against each other*, in the real browser, against the real
 * backend, because that is where they interact and where a gap between them would show. A refusal
 * explained only in English, a deep link that survives a reload only in one locale, a keyboard journey
 * that stops working once the page is right-to-left — none of those would be caught by a per-area spec,
 * and all three would be shipped.
 *
 * Three journeys, each crossing at least two of the four areas:
 *
 * 1. identity + context + direction — a run opened from a deep link keeps its own scope, in Persian,
 *    across a reload, and its log is still the server's log;
 * 2. identity + direction — a refusal is explained with the server's own numbers in the second locale,
 *    and the same control changes state in place when the identity changes, with no page reload;
 * 3. context + accessibility — a deep link that names something the page must not show fails safely and
 *    is announced as an error rather than rendered as emptiness, with no retry offered for an answer.
 */

import {
  apiGet,
  appConsoleErrors,
  clearPendingApproval,
  expect,
  fixtures,
  ROLES,
  selectRole,
  startRunFromStudio,
  test,
  waitForLoaded,
} from './fixtures'

/**
 * The engine the engineering workspace runs, and the action level the server assigns to running it.
 *
 * The key is the registry's own key for the operational-readiness engine; the inputs are the engine's
 * inputs — a fixed `as_of` and a scope — written out so the same request the button sends can be made
 * directly and compared with what the screen says. They are the engine's inputs, not expected outputs:
 * nothing here asserts a computed value.
 */
const ENGINE_KEY = 'readiness.assessment'

const ENGINE_INPUTS = {
  as_of: '2026-01-01',
  scope: 'spud',
  requirements: [
    {
      item_id: 'RIG-01',
      name: 'Drilling rig',
      category: 'equipment',
      quantity_required: 1,
      quantity_available: 1,
      status: 'available',
      criticality: 'critical',
      required_by: '2026-01-01',
      lead_time_days: 0,
    },
  ],
}

function asciiDigits(text: string): string {
  const persian = '۰۱۲۳۴۵۶۷۸۹'
  const arabic = '٠١٢٣٤٥٦٧٨٩'
  return text.replace(/[۰-۹٠-٩]/g, (digit) => {
    const index = persian.indexOf(digit)
    return String(index >= 0 ? index : arabic.indexOf(digit))
  })
}

test.describe('checkpoint 5, integrated', () => {
  test('a run from a deep link keeps its scope in Persian, across a reload, with the server’s log', async ({
    page,
    request,
    wellId,
  }) => {
    const consoleErrors: string[] = []
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })

    // A real run, started the way an operator starts one.
    const runId = await startRunFromStudio(page, fixtures().workflow_id, wellId)
    try {
      const envelope = await apiGet<{ run: { well_id: string | null; workflow_key: string | null } }>(
        request,
        `/runs/${runId}`,
        'supervisor',
      )

      // The page is now in the second locale, and stays right-to-left.
      await page.getByTestId('locale-switch').selectOption('fa')
      await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
      await expect(page.locator('html')).toHaveAttribute('lang', 'fa')
      await waitForLoaded(page)

      // Identity + context: the run in the address is the run on screen, and the scope it shows is the
      // scope the server recorded for it — not the well that happens to be selected elsewhere.
      await expect(page.getByTestId('run-status')).toBeVisible()
      await expect(page.locator('main')).toContainText(runId)
      const scope = page.getByTestId('run-scope')
      await expect(scope).toContainText(String(envelope.run.well_id))

      // Direction inside the same screen: the identifiers stay identifiers while the prose is Persian.
      const directions = await page
        .locator('.font-mono:visible')
        .evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).direction))
      expect(directions.length).toBeGreaterThan(0)
      expect(new Set(directions)).toEqual(new Set(['ltr']))

      // And engineering figures on that screen are still figures: whatever the locale renders, the
      // digits map back to the same ASCII string, so no number is silently re-written by translation.
      const mainText = await page.locator('main').innerText()
      expect(asciiDigits(mainText)).toContain(String(envelope.run.well_id))

      // Reload: same run, same scope, same direction. A deep link that only works on the first visit
      // is a deep link that does not work.
      await page.reload()
      await waitForLoaded(page)
      await expect(page.getByTestId('run-status')).toBeVisible()
      await expect(page.locator('main')).toContainText(runId)
      await expect(scope).toContainText(String(envelope.run.well_id))
      await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')

      // Context: the log on the screen is the log the server holds for this run. This is the part that
      // would break if the run's identity were reconstructed from the previous screen's state.
      await page.getByRole('tab', { name: /رویداد|Event/i }).click()
      const events = await apiGet<{ items: Array<{ seq: number }> }>(
        request,
        `/runs/${runId}/events`,
        'supervisor',
      )
      const rendered = await page.getByTestId('run-events').locator('li').evaluateAll((nodes) =>
        nodes.map((node) => (node as HTMLElement).dataset.seq ?? ''),
      )
      for (const seq of events.items.map((event) => String(event.seq))) {
        expect(rendered, `event ${seq} of the run in the address is on screen`).toContain(seq)
      }

      expect(appConsoleErrors(consoleErrors)).toEqual([])
    } finally {
      await clearPendingApproval(
        request,
        runId,
        'e2e checkpoint-5 certification: closed to leave the seeded state clean',
      )
    }
  })

  test('a refusal is explained in the server’s own numbers, in the second locale, and changes in place', async ({
    page,
    request,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/engineering`)
    await waitForLoaded(page)
    const navigations: string[] = []
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) navigations.push(frame.url())
    })

    // The second locale first, so the refusal is read by someone whose interface is Persian.
    await page.getByTestId('locale-switch').selectOption('fa')
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await selectRole(page, 'viewer')
    await waitForLoaded(page)

    await page.getByRole('button', { name: /ارزیابی آمادگی|Operational readiness assessment/i }).click()
    const runButton = page.getByTestId('engine-run')
    await expect(runButton).toHaveAttribute('data-gate-state', 'level-below')
    await expect(runButton).toBeDisabled()

    // The explanation is the server's, and the numbers in it are the server's numbers: the level codes
    // are not "translated", because a level is an identifier, not a word.
    const refused = await request.post(`/api/v1/registry/engines/${ENGINE_KEY}/run`, {
      headers: { 'X-Dev-Roles': ROLES.viewer },
      data: { inputs: ENGINE_INPUTS, well_id: wellId, persist: false },
    })
    expect(refused.status(), 'an L0 identity may not run an L2 action').toBe(403)
    const envelope = (await refused.json()) as {
      error: { details: { required_level?: string; ceiling?: string } }
    }
    const explanation = page.getByTestId('engine-run-gate')
    await expect(explanation).toContainText(String(envelope.error.details.required_level))
    await expect(explanation).toContainText(String(envelope.error.details.ceiling))

    // Identity changes in place: no reload, and the same control now reports a different state. The
    // screen is Persian throughout — an identity switch is not a locale change.
    const before = navigations.length
    await selectRole(page, 'supervisor')
    await expect(page.getByTestId('engine-run')).toHaveAttribute('data-gate-state', 'ready')
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    expect(navigations.length, 'switching identity does not reload the page').toBe(before)

    // The UI did not grant the authority: it is the server that decides, and it agrees with the screen
    // for this identity too — the same request the button would send is authorised, not merely drawn
    // as authorised.
    const permitted = await request.post(`/api/v1/registry/engines/${ENGINE_KEY}/run`, {
      headers: { 'X-Dev-Roles': ROLES.supervisor },
      data: { inputs: ENGINE_INPUTS, well_id: wellId, persist: false },
    })
    expect(permitted.status(), 'the identity the screen reports as ready is authorised by the server').toBe(200)
  })

  test('a deep link to another well’s document fails safely and reaches the accessibility tree', async ({
    page,
    request,
  }) => {
    // The seeded document belongs to the seeded well; pointing at it from another well's workspace is
    // the mismatch. It is the direction the context spec uses, and the provenance is checked here so
    // the journey cannot pass because both wells happened to be the same one.
    const documentId = fixtures().document_ids[0] as string
    const document = await apiGet<{ document: { id: string; well_id: string; title: string } }>(
      request,
      `/documents/${documentId}`,
      'engineer',
    )
    expect(document.document.well_id).toBe(fixtures().well_id)

    await page.goto(`/wells/wel_not_a_well/documents?document=${encodeURIComponent(documentId)}`)
    await waitForLoaded(page)

    const refusal = page.getByTestId('document-other-well')
    await expect(refusal).toBeVisible()
    // The refusal states both facts: which document, and which well it actually belongs to. Inventing
    // either one — or guessing — is what this guards against.
    await expect(refusal).toContainText(documentId)
    await expect(refusal).toContainText(document.document.well_id)

    // Accessibility: the refusal is in the accessibility tree, not merely on the canvas. `ariaSnapshot`
    // is what the browser exposes to assistive technology, so a reader is told the same thing a sighted
    // reader is — and told it about this document, not about "nothing selected".
    const spoken = await refusal.ariaSnapshot()
    expect(spoken).toContain(documentId)
    expect(spoken).toContain(document.document.well_id)
    expect(spoken).not.toMatch(/select a document to inspect/i)

    // The document's own content is nowhere on the page: no title, no extraction, no evidence card.
    await expect(page.getByText(document.document.title)).toHaveCount(0)

    // And it is an answer, not a fault: there is nothing to retry.
    await expect(page.getByRole('button', { name: /Retry/i })).toHaveCount(0)
  })
})
