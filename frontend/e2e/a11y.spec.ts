/**
 * Accessibility: what has to work with a keyboard, in a real browser, and what has to be announced.
 *
 * Every journey below operates the control the way a person without a mouse does — real key presses in
 * a real Chromium against the real backend — and then reads the result out of the DOM the server
 * filled. Nothing here is a mocked RTL snapshot or an attribute assertion standing in for behaviour.
 *
 * The five claims:
 *
 * 1. **The critical journey can be walked without a mouse.** A tab group is entered once and moved
 *    through with the arrow keys, in the direction the page reads — which is not the same physical key
 *    in Persian as in English — and the panel a tab controls is the panel on screen.
 * 2. **A table row that opens a record is a control.** It is reachable by `Tab`, activates on `Enter`
 *    and on `Space`, and says which row is the current one.
 * 3. **A dialog takes focus, keeps it, and gives it back.** The evidence drawer moves focus inside,
 *    cycles `Tab`/`Shift+Tab` within the panel, closes on `Escape`, and returns focus to its opener.
 * 4. **Live data is announced once per burst, politely, without taking focus.**
 * 5. **Nothing interactive is nameless.** Measured through the browser's own accessibility tree — the
 *    name a screen reader would actually read — not through a hand-rolled attribute check.
 */

import {
  apiGet,
  apiPost,
  clearPendingApproval,
  expect,
  fixtures,
  startRunFromStudio,
  test,
  waitForLoaded,
  type Page,
} from './fixtures'

/**
 * Interactive nodes in the accessibility tree that carry no accessible name.
 *
 * `ariaSnapshot()` is the accessibility tree as the browser (through Playwright) computes it, so a
 * node listed here is a control a screen reader would announce as a bare role. Named nodes appear as
 * `- button "Save"`; unnamed ones as `- button`.
 */
const INTERACTIVE_ROLES = [
  'button',
  'link',
  'textbox',
  'searchbox',
  'combobox',
  'checkbox',
  'radio',
  'switch',
  'tab',
  'menuitem',
  'menuitemcheckbox',
  'menuitemradio',
  'slider',
  'spinbutton',
  'option',
  'progressbar',
]

async function unnamedInteractiveNodes(page: Page): Promise<string[]> {
  const snapshot = await page.locator('body').ariaSnapshot()
  const unnamed: string[] = []
  for (const line of snapshot.split('\n')) {
    const match = /^\s*-\s+([a-zA-Z]+)\s*$/.exec(line)
    if (match && INTERACTIVE_ROLES.includes(match[1] as string)) unnamed.push(match[1] as string)
  }
  return unnamed
}

/** Whether focus is currently held by an element inside the open dialog. */
function focusIsInsideDialog(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const active = document.activeElement
    const dialog = document.querySelector('[role="dialog"]')
    return Boolean(active && dialog && dialog.contains(active))
  })
}

test.describe('keyboard and assistive-technology behaviour', () => {
  test('a tab group is entered once, moved through with arrows, and names its panel', async ({
    page,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/cockpit`)
    await waitForLoaded(page)

    const tabs = page.getByRole('tab')
    const first = tabs.first()
    await expect(first).toBeVisible()
    await first.focus()
    await expect(first).toBeFocused()

    // Only the selected tab is in the tab order: a tab group is one stop for `Tab`, and the arrows
    // move inside it. That is what keeps a page with several groups navigable.
    await expect(first).toHaveAttribute('aria-selected', 'true')
    const tabId = await first.getAttribute('id')
    const panelId = await first.getAttribute('aria-controls')
    expect(panelId).toBeTruthy()
    const panel = page.locator(`[id="${panelId}"]`)
    await expect(panel).toBeVisible()
    await expect(panel).toHaveAttribute('role', 'tabpanel')
    await expect(panel).toHaveAttribute('aria-labelledby', tabId ?? '')
    expect(await tabs.nth(1).getAttribute('tabindex')).toBe('-1')

    // The arrow key that means "next" follows the reading direction: in English the next tab is drawn
    // to the right, so `ArrowRight` selects it.
    await tabs.first().focus()
    await page.keyboard.press('ArrowRight')
    const moved = tabs.nth(1)
    await expect(moved).toHaveAttribute('aria-selected', 'true')
    await expect(moved).toBeFocused()
    const movedPanelId = await moved.getAttribute('aria-controls')
    await expect(page.locator(`[id="${movedPanelId}"]`)).toBeVisible()
    await expect(page.locator(`[id="${panelId}"]`)).toHaveCount(0)

    // `End` goes to the last tab and `Home` returns to the first.
    await page.keyboard.press('End')
    await expect(tabs.last()).toHaveAttribute('aria-selected', 'true')
    await expect(tabs.last()).toBeFocused()
    await page.keyboard.press('Home')
    await expect(tabs.first()).toHaveAttribute('aria-selected', 'true')
    await expect(tabs.first()).toBeFocused()
  })

  test('in Persian, “next tab” follows the page’s direction, not the physical key', async ({
    page,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/cockpit`)
    await waitForLoaded(page)
    await page.getByTestId('locale-switch').selectOption('fa')
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await waitForLoaded(page)

    const tabs = page.getByRole('tab')
    await tabs.first().focus()
    await expect(tabs.first()).toHaveAttribute('aria-selected', 'true')

    // The tabs are drawn from right to left, so the tab *after* this one is the one to the left.
    // Answering `ArrowRight` here would move every Persian user the wrong way.
    await page.keyboard.press('ArrowLeft')
    await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true')
    await expect(tabs.nth(1)).toBeFocused()

    await page.keyboard.press('ArrowRight')
    await expect(tabs.first()).toHaveAttribute('aria-selected', 'true')
    await expect(tabs.first()).toBeFocused()
  })

  test('a table row that opens a record is reachable and activatable by keyboard', async ({
    page,
    request,
    wellId,
  }) => {
    // Two real runs, because the journey is about *choosing* a record: the row pressed must be the
    // record opened. The runs are started through the studio, so the rows come from the database.
    const workflowId = fixtures().workflow_id
    const firstRun = await startRunFromStudio(page, workflowId, wellId)
    const secondRun = await startRunFromStudio(page, workflowId, wellId)
    // `startRunFromStudio` approves only where a gate exists; the inbox is a shared resource, so a
    // journey that opens runs closes the gates it opened before it ends.
    const pending: string[] = [firstRun, secondRun]

    try {
      await page.goto('/runs')
      await waitForLoaded(page)
      const secondRow = page.getByRole('row').filter({ hasText: secondRun })
      await expect(secondRow).toBeVisible()

      // The row is a control: focusable, and it says so.
      await secondRow.focus()
      await expect(secondRow).toBeFocused()
      expect(await secondRow.getAttribute('tabindex')).toBe('0')

      // `Enter` opens the record the row names, and the row reports itself as the current one to
      // assistive technology — not by colour alone.
      await page.keyboard.press('Enter')
      await expect(page).toHaveURL(new RegExp(`run=${secondRun}`))
      await expect(page.getByRole('row', { name: new RegExp(secondRun) })).toHaveAttribute(
        'aria-current',
        'true',
      )

      // `Space` does the same, from the other row, and does not scroll the page instead.
      await page.goto('/runs')
      await waitForLoaded(page)
      const firstRow = page.getByRole('row').filter({ hasText: firstRun })
      await expect(firstRow).toBeVisible()
      const scrollBefore = await page.evaluate(() => window.scrollY)
      await firstRow.focus()
      await page.keyboard.press(' ')
      await expect(page).toHaveURL(new RegExp(`run=${firstRun}`))
      expect(await page.evaluate(() => window.scrollY)).toBe(scrollBefore)
      await expect(page.getByRole('row', { name: new RegExp(firstRun) })).toHaveAttribute(
        'aria-current',
        'true',
      )
    } finally {
      for (const runId of pending) {
        await clearPendingApproval(
          request,
          runId,
          'e2e a11y journey: closed to leave the seeded state clean',
        )
      }
      pending.length = 0
    }
  })

  test('the evidence drawer takes focus, keeps it, closes on Escape, and gives focus back', async ({
    page,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/cockpit`)
    await waitForLoaded(page)

    // The control that opens the panel; focus must return to exactly this element.
    const opener = page.getByRole('button', { name: /Show evidence/i }).first()
    await expect(opener).toBeVisible()
    await opener.click()

    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog).toHaveAttribute('aria-modal', 'true')

    // 1. Focus moved into the panel. A dialog that opens while focus stays on the page behind it is a
    //    dialog a keyboard user has to hunt for.
    expect(await focusIsInsideDialog(page), 'focus moves into the dialog when it opens').toBe(true)

    // 2. `Tab` cycles within the panel instead of walking into the page the dialog just declared
    //    inert. Six presses is more than the panel's control count, so a leak would show up.
    for (let press = 1; press <= 6; press += 1) {
      await page.keyboard.press('Tab')
      expect(await focusIsInsideDialog(page), `Tab press ${press} stays inside the dialog`).toBe(true)
    }
    await page.keyboard.press('Shift+Tab')
    expect(await focusIsInsideDialog(page), 'Shift+Tab stays inside the dialog').toBe(true)

    // 3. `Escape` closes it, and focus returns to the control that opened it — not to the body, which
    //    would send a keyboard user back to the top of the page.
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(opener).toBeFocused()
  })

  test('a run that produces events announces them once, politely, without taking focus', async ({
    page,
    request,
    wellId,
  }) => {
    const runId = await startRunFromStudio(page, fixtures().workflow_id, wellId)
    try {
      // The run this workflow starts stops at its human gate, which is what makes the announcement
      // testable: the events that are *news* are the ones produced after the gate is opened while the
      // monitor is watching. History (whatever already happened) is deliberately not announced.
      const pending = await apiGet<{ run: { status: string }; pending_approval?: { id: string } | null }>(
        request,
        `/runs/${runId}`,
        'supervisor',
      )
      const approval = pending.pending_approval
      expect(approval, 'the seeded workflow stops at a human gate').toBeTruthy()

      // A different principal decides — the server refuses a decision by the requester — and the
      // decision deliberately does *not* resume the run: resuming is the operator's next action, and
      // it is the action that produces the live events the region has to announce.
      await apiPost(
        request,
        `/approvals/${approval?.id}/decide`,
        {
          decision: 'approved',
          note: 'e2e a11y journey: approved, left for the operator to resume',
          // The default on this endpoint is to continue immediately. This journey is about what the
          // screen does when the run continues *while it is being watched*, so the decision is
          // recorded and the resumption is left as the next human action.
          resume: false,
        },
        'wellManager',
      )

      // A fresh look at the run, as a person returning to the screen would have: the run is still
      // suspended, and the monitor offers to resume it.
      await page.goto(`/runs?run=${runId}`)
      await waitForLoaded(page)
      const announcement = page.getByTestId('run-announcement')
      await expect(announcement).toBeAttached()
      // Polite and atomic: one sentence, read when it settles, never interrupting what is being read.
      await expect(announcement).toHaveAttribute('role', 'status')
      const live = await announcement.getAttribute('aria-live')
      expect(live === 'polite' || live === null, 'the region is polite, never assertive').toBe(true)
      await expect(announcement).toHaveAttribute('aria-atomic', 'true')
      // Nothing has happened since this screen opened, so it says nothing.
      expect((await announcement.innerText()).trim()).toBe('')

      // Resume by keyboard, as the operator would: focus the button and press it.
      const resume = page.getByTestId('resume-run')
      await expect(resume).toBeVisible()
      await resume.focus()
      await page.keyboard.press('Enter')

      // The runtime continues, the socket pushes the node events, and the region says so in words —
      // a bare number would be announced as a number.
      await expect
        .poll(async () => (await announcement.innerText()).trim(), { timeout: 60_000 })
        .toMatch(/event/i)
      const text = (await announcement.innerText()).trim()

      // One sentence for the burst, not one line per event: a live region that narrates every frame is
      // a live region people turn off. This is the whole point of the coalescing window.
      expect(text.split('\n').length).toBeLessThanOrEqual(2)

      // And it is a live region, not a focus target: announcing never moves the reader.
      expect(await announcement.evaluate((el) => document.activeElement === el)).toBe(false)
      expect(await announcement.getAttribute('tabindex')).toBeNull()

      // Resuming makes the run no longer resumable, so the button the operator pressed removes itself.
      // Focus must not fall to the document body — that drops a keyboard user at the top of the page —
      // it goes to the run's status badge, which is where the outcome is reported.
      await expect(resume).toHaveCount(0)
      const focusReport = await page.evaluate(() => {
        const active = document.activeElement
        const badge = document.querySelector('[data-testid="run-status"]')
        return {
          onBodyshell: active === document.body || active === document.documentElement,
          onStatus: Boolean(active && badge && active.contains(badge)),
        }
      })
      expect(focusReport.onBodyshell, 'focus is not dropped on the document body').toBe(false)
      expect(focusReport.onStatus, 'focus lands on the badge that reports the outcome').toBe(true)
    } finally {
      /*
       * Leave the shared database as the journey found it. By the end of the journey the run has
       * already continued, so this is normally a no-op; if the journey failed halfway the run is still
       * suspended and the gate is still open, and this closes it — through the API, with the identity
       * that is allowed to decide, never by writing the database directly.
       */
      const state = await apiGet<{ run: { status: string }; pending_approval?: { id: string; status?: string } | null }>(
        request,
        `/runs/${runId}`,
        'supervisor',
      )
      if (state.run.status === 'waiting_approval' || state.run.status === 'paused') {
        const openApproval = state.pending_approval
        if (openApproval && openApproval.status !== 'approved' && openApproval.status !== 'rejected') {
          await clearPendingApproval(
            request,
            runId,
            'e2e a11y journey: closed to leave the seeded state clean',
          )
        } else if (openApproval) {
          await apiPost(
            request,
            `/runs/${runId}/resume?approval_id=${encodeURIComponent(openApproval.id)}`,
            undefined,
            'supervisor',
          )
        }
      }
    }
  })

  test('no interactive control on the critical screens is nameless', async ({ page, wellId }) => {
    for (const path of [
      `/wells/${wellId}/cockpit`,
      `/wells/${wellId}/engineering`,
      '/runs',
      '/workflows',
      '/platform',
    ]) {
      await page.goto(path)
      await waitForLoaded(page)
      expect(await unnamedInteractiveNodes(page), `nameless controls on ${path}`).toEqual([])
    }
  })
})
