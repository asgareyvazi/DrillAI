/**
 * Checkpoint 4 — the failures a healthy server *can* produce, in a real browser.
 *
 * These journeys are the product's own error paths, driven the way an operator reaches them: a deep
 * link to a run that does not exist, an identity that may not take an action the interface still
 * offers, a genuinely invalid upload, a decision somebody else already took, and a network that goes
 * away under a working page.
 *
 * Nothing here is intercepted. The 404, the 403, the 422 and the 409 are the real answers of the real
 * server; the only thing a journey controls is what the operator does and, for the last one, whether
 * the browser has a network at all.
 *
 * Every assertion is about what the screen *says*, because that is where a wrong error model does its
 * damage: "the backend is unreachable" over a 404 is a lie that sends an operator to check a service
 * that is running perfectly well.
 */

import {
  apiGet,
  appConsoleErrors,
  clearPendingApproval,
  fixtures,
  selectRole,
  startRunFromStudio,
  test,
  expect,
  waitForLoaded,
} from './fixtures'
import type { Page } from '@playwright/test'

type ApprovalEnvelope = {
  approval: { id: string; status: string; decided_by: string | null }
  run?: { id: string; status: string }
}

const errorState = (page: Page) => page.getByTestId('error-state')

const well = () => fixtures().well_id

test.describe('the error matrix: what the interface says when a request fails', () => {
  test('a deep link to a run that does not exist is "not found", not an outage', async ({ page, request }) => {
    const missing = 'run_e2e_does_not_exist'
    const answer = await request.get(`/api/v1/runs/${missing}`, { headers: { 'X-Dev-Roles': 'engineer' } })
    expect(answer.status(), 'the server must really answer 404 for this journey to mean anything').toBe(404)

    await page.goto(`/runs?run=${missing}`)

    const state = errorState(page)
    await expect(state).toBeVisible()
    await expect(state).toHaveAttribute('data-error-kind', 'not_found')
    await expect(state).toHaveAttribute('data-http-status', '404')
    await expect(state).toContainText(/not found/i)
    await expect(state).not.toContainText(/unreachable/i)
    await expect(state).not.toContainText(/timed out/i)
    // Retrying cannot conjure a run that does not exist, so no retry is offered.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)
    // The list the run would have belonged to is still usable: one missing run is not a broken page.
    await expect(page.getByRole('table').or(page.getByText(/no workflow runs yet/i)).first()).toBeVisible()

    // And the way out is the one the page already offers: clearing the selection returns to the list.
    await page.getByRole('button', { name: /clear selection/i }).click()
    await expect(errorState(page)).toHaveCount(0)
    await expect(page.getByText(/select a run to inspect it/i)).toBeVisible()
    expect(page.url()).not.toContain('run=')
  })

  test('an identity that may not decide an approval is told so, with the resource still on screen', async ({
    page,
    request,
    consoleErrors,
  }) => {
    // A run parked at its gate, started the way an operator starts one.
    const runId = await startRunFromStudio(page, fixtures().workflow_id, well())
    const envelope = await apiGet<{ run: { status: string } }>(request, `/runs/${runId}`, 'supervisor')
    expect(envelope.run.status).toBe('waiting_approval')

    await page.goto(`/runs?run=${runId}`)
    // The viewer may read everything and decide nothing: an identity that is understood and still
    // not permitted, which must read differently from "not signed in".
    await selectRole(page, 'viewer')

    const decide = page.getByTestId('approval-approve')
    await expect(decide).toBeVisible()
    await decide.click()

    const state = errorState(page)
    await expect(state).toBeVisible()
    await expect(state).toHaveAttribute('data-error-kind', 'forbidden')
    await expect(state).toHaveAttribute('data-http-status', '403')
    await expect(state).toContainText(/permission/i)
    await expect(state).not.toContainText(/unreachable/i)
    await expect(state).not.toContainText(/not signed in/i)
    // No blind retry: the identity will not acquire the permission by asking again, and there is
    // nothing to re-read either — the refusal was about the identity, not about a stale screen.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)
    await expect(state.getByTestId('error-reconcile')).toHaveCount(0)
    // The approval is still on screen with its run — the context survived the refusal.
    await expect(page.getByTestId('approval-approve')).toBeVisible()
    await expect(page.getByText(runId, { exact: false }).first()).toBeVisible()

    await clearPendingApproval(request, runId, 'Cleanup after the permission journey.')
    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('an upload the server rejects says what was wrong with it, not that the backend is down', async ({
    page,
    consoleErrors,
  }) => {
    await page.goto(`/wells/${well()}/documents`)
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()

    // A genuinely invalid payload through the interface's own form: an empty file. The server answers
    // 422 with the reason ("the uploaded file is empty"), and that reason is what the screen shows.
    await page.setInputFiles('input[type="file"]', {
      name: 'empty-ddr.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(''),
    })

    const state = errorState(page)
    await expect(state).toBeVisible()
    await expect(state).toHaveAttribute('data-error-kind', 'validation')
    await expect(state).toHaveAttribute('data-http-status', '422')
    await expect(state).toContainText(/empty/i)
    await expect(state).not.toContainText(/unreachable/i)
    // A rejected payload is about the payload: the same file will be rejected again.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)
    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a decision somebody else already took reads as a conflict and reconciles to the record', async ({
    page,
    context,
    request,
    consoleErrors,
  }) => {
    const runId = await startRunFromStudio(page, fixtures().workflow_id, well())
    const envelope = await apiGet<{ run: { status: string }; pending_approval?: { id: string } | null }>(
      request,
      `/runs/${runId}`,
      'supervisor',
    )
    expect(envelope.run.status).toBe('waiting_approval')
    const approvalId = envelope.pending_approval?.id as string

    // Two operators, one run, the same approval open on both screens.
    const other = await context.newPage()
    await page.goto(`/runs?run=${runId}`)
    await selectRole(page, 'wellManager')
    await other.goto(`/runs?run=${runId}`)
    await selectRole(other, 'wellManager')
    await expect(other.getByTestId('approval-approve')).toBeVisible()

    // The first operator decides it.
    await page.getByTestId('approval-approve').click()
    await expect(page.getByTestId('approval-approve')).toHaveCount(0, { timeout: 20_000 })

    // The second operator's screen is now stale, and its decision is refused by the server.
    await other.getByTestId('approval-approve').click()
    const state = errorState(other)
    await expect(state).toBeVisible()
    await expect(state).toHaveAttribute('data-error-kind', 'conflict')
    await expect(state).toHaveAttribute('data-http-status', '409')
    await expect(state).toContainText(/already|conflict|state/i)
    await expect(state).not.toContainText(/unreachable/i)

    // A conflict is not fixed by sending the decision again, so there is no retry — but the screen is
    // out of date, and the one useful action is to read the record that actually exists. It is the
    // operator who asks for that; the page does not silently rewrite itself underneath them.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)
    const reconcile = state.getByTestId('error-reconcile')
    await expect(reconcile).toBeVisible()
    await reconcile.click()

    await expect(other.getByTestId('approval-record')).toBeVisible({ timeout: 20_000 })
    await expect(other.getByText(/approved/i).first()).toBeVisible()
    await expect(other.getByTestId('approval-approve')).toHaveCount(0)

    // The decision that stands is the first one, and nothing the second operator sent changed it.
    const decided = await apiGet<ApprovalEnvelope>(request, `/approvals/${approvalId}`, 'wellManager')
    expect(decided.approval.status).toBe('approved')

    await other.close()
    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a network that goes away is a connection problem, and it recovers when the network returns', async ({
    page,
    context,
    consoleErrors,
  }) => {
    // Both screens are visited while the network is up, so the failure under test is a data read and
    // not a module the browser could not fetch.
    await page.goto('/runs')
    await waitForLoaded(page)
    await page.goto('/wells')
    await waitForLoaded(page)

    // With no network at all, a read has no HTTP status and no response body. The interface must not
    // invent either: the request never reached anything.
    await context.setOffline(true)
    await page.getByRole('link', { name: 'Runs' }).click()

    const state = errorState(page)
    await expect(state).toBeVisible({ timeout: 30_000 })
    await expect(state).toHaveAttribute('data-error-kind', 'network')
    await expect(state).toContainText(/unreachable|connection/i)
    // The retry is the point of this state: a connection can come back.
    await expect(state.getByRole('button', { name: /retry/i })).toBeVisible()
    // It is not reported as anything the server said.
    await expect(state).not.toContainText(/not found/i)
    await expect(state).not.toContainText(/timed out/i)

    await context.setOffline(false)
    await state.getByRole('button', { name: /retry/i }).click()
    await expect(errorState(page)).toHaveCount(0, { timeout: 30_000 })
    await expect(page.getByRole('table').or(page.getByText(/no workflow runs yet/i)).first()).toBeVisible()
    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
