/**
 * Checkpoint 4 — the failures a healthy server cannot produce on request, in a real browser.
 *
 * These journeys run against a second instance of the same API, configured with deterministic fault
 * injection (`DRILLAI_E2E_FAULTS`), and a second build of the same client with a short request
 * deadline. Nothing is intercepted in the browser: the page issues ordinary requests and the server
 * answers them badly, or does not answer them at all. What the journeys assert is what an operator
 * would read on the screen.
 *
 * The three failures here are the ones where a wrong classification is most expensive:
 *
 *   * a real deadline exceeded while the server is still working — must read as *timed out*, and the
 *     abandoned request must be abandoned at the connection, not merely ignored;
 *   * a response that arrives but is not the contract — must read as an unreadable response with a
 *     request id, never as an outage;
 *   * an unhandled server fault — must read as a server failure, name the request, and offer a retry.
 *
 * The last journey is the other direction: a request the *page* abandons on purpose. A cancellation
 * is not a failure, and the assertions are the absence of the things a failure would produce — an
 * error state, a console error, and a stale response replacing what the operator is now looking at.
 */

import {
  apiPost,
  appConsoleErrors,
  clearPendingApproval,
  seededIds,
  startRunFromStudio,
  test,
  expect,
} from './fixtures'
import type { APIRequestContext, Page } from '@playwright/test'

type FaultRule = { method: string; path: string; mode: string; delay_ms?: number }

const errorState = (page: Page) => page.getByTestId('error-state')

async function arm(request: APIRequestContext, rule: FaultRule): Promise<void> {
  const response = await apiPost<{ armed: FaultRule[] }>(request, '/__faults/arm', rule)
  expect(response.armed.some((entry) => entry.path === rule.path && entry.mode === rule.mode)).toBeTruthy()
}

async function disarm(request: APIRequestContext): Promise<void> {
  await apiPost(request, '/__faults/disarm', {})
}

/**
 * Every request the page sent to a path, in order.
 *
 * This is a listener, not an interception: the page's requests are untouched, and the count is
 * evidence about the client's retry policy — bounded, or a storm.
 */
function countRequests(page: Page, path: string): { count: () => number } {
  let count = 0
  page.on('request', (sent) => {
    if (new URL(sent.url()).pathname === path) count += 1
  })
  return { count: () => count }
}

// Two journeys here start real runs, wait out a six-second hold and then wait again past the point
// the abandoned response would have arrived. That is longer than a normal interaction budget, and it
// is on purpose: the timing *is* the thing being checked.
test.describe.configure({ timeout: 180_000 })

test.describe('failures the server must be asked to produce', () => {
  test.afterEach(async ({ request }) => {
    await disarm(request)
  })

  test('a deadline the server is still holding reads as "timed out", and the request was really cancelled', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const wellsPath = '/api/v1/wells'
    const requests = countRequests(page, wellsPath)
    // What the browser's network stack says about the requests the page gave up on. This is the part
    // a passing UI assertion cannot show: a client that merely stopped listening would leave the
    // requests as completed, and this list would be empty.
    const abandoned: string[] = []
    page.on('requestfailed', (sent) => {
      if (new URL(sent.url()).pathname === wellsPath) abandoned.push(sent.failure()?.errorText ?? '')
    })

    // The server is slower than this stack's deadline (three seconds), and stays slower: the answer it
    // eventually sends is one nobody is waiting for any more.
    await arm(request, { method: 'GET', path: wellsPath, mode: 'slow', delay_ms: 20_000 })
    await page.goto('/wells')

    const state = errorState(page)
    await expect(state).toBeVisible({ timeout: 30_000 })
    await expect(state).toHaveAttribute('data-error-kind', 'timeout')
    await expect(state).toContainText(/timed out/i)
    await expect(state).not.toContainText(/unreachable/i)
    // A deadline is about time, so trying again is sensible.
    await expect(state.getByRole('button', { name: /retry/i })).toBeVisible()

    expect(abandoned.length, 'the deadline must cancel the request it abandoned').toBeGreaterThan(0)
    expect(abandoned.some((text) => /ABORT|ERR_/.test(text)), `browser said: ${abandoned.join(', ')}`).toBeTruthy()

    // Retrying a deadline is reasonable; retrying it for ever is not.
    const attemptsWhenSettled = requests.count()
    await page.waitForTimeout(4_000)
    expect(requests.count(), 'automatic retries must stop once the failure has settled').toBe(
      attemptsWhenSettled,
    )

    // Past the point where the abandoned responses arrive, the screen still shows the deadline: an
    // answer nobody is waiting for must not replace what the operator is reading.
    await page.waitForTimeout(16_000)
    await expect(state).toHaveAttribute('data-error-kind', 'timeout')
    await expect(state).toBeVisible()

    // And the page recovers for real once the server answers in time again.
    await disarm(request)
    await state.getByRole('button', { name: /retry/i }).click()
    await expect(errorState(page)).toHaveCount(0, { timeout: 30_000 })
    await expect(page.getByRole('table').or(page.getByText(/no wells/i)).first()).toBeVisible()
    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a response that is not the contract is a protocol failure with a request id, not an outage', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const wellsPath = '/api/v1/wells'
    await arm(request, { method: 'GET', path: wellsPath, mode: 'malformed' })

    await page.goto('/wells')
    const state = errorState(page)
    await expect(state).toBeVisible({ timeout: 30_000 })

    await expect(state).toHaveAttribute('data-error-kind', 'malformed')
    await expect(state).toContainText(/could not read|contract/i)
    await expect(state).not.toContainText(/unreachable/i)
    // The response arrived, so it has a request id: reacting to a protocol disagreement without one
    // would leave an operator with nothing to quote to whoever can look the request up.
    await expect(state.getByTestId('error-request-id')).not.toBeEmpty()

    // Nothing is fixed by asking again in the same way, and the page must not pretend otherwise.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)

    await disarm(request)
    await page.reload()
    await expect(errorState(page)).toHaveCount(0, { timeout: 30_000 })
    await expect(page.getByRole('table').or(page.getByText(/no wells/i)).first()).toBeVisible()
    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('an unhandled server fault names the request, says it was the server, and can be retried', async ({
    page,
    request,
    consoleErrors,
  }) => {
    const wellsPath = '/api/v1/wells'
    await arm(request, { method: 'GET', path: wellsPath, mode: 'unhandled' })

    await page.goto('/wells')
    const state = errorState(page)
    await expect(state).toBeVisible({ timeout: 30_000 })

    await expect(state).toHaveAttribute('data-error-kind', 'server')
    await expect(state).toHaveAttribute('data-http-status', '500')
    await expect(state).toContainText(/server/i)
    // The distinction that matters: a 500 is the server failing, not the network being gone.
    await expect(state).not.toContainText(/unreachable/i)
    await expect(state).not.toContainText(/timed out/i)
    await expect(state.getByTestId('error-request-id')).not.toBeEmpty()
    await expect(state.getByRole('button', { name: /retry/i })).toBeVisible()

    await disarm(request)
    await state.getByRole('button', { name: /retry/i }).click()
    await expect(errorState(page)).toHaveCount(0, { timeout: 30_000 })
    await expect(page.getByRole('table').or(page.getByText(/no wells/i)).first()).toBeVisible()
    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('a slow read the operator navigates away from is cancelled, not reported, and cannot replace the screen', async ({
    page,
    request,
    consoleErrors,
  }) => {
    // Two real runs, started the way an operator starts them, so there is something to switch between.
    const { workflowId, wellId } = await seededIds(request)
    const first = await startRunFromStudio(page, workflowId, wellId)
    await clearPendingApproval(request, first, 'Finished so the second run can start.')
    const second = await startRunFromStudio(page, workflowId, wellId)
    await clearPendingApproval(request, second, 'Cleanup after the cancellation journey.')

    // What the browser's network stack says about the requests the page gave up on. This is the
    // evidence that the abandonment reached the connection rather than being merely ignored: a client
    // that only stopped listening would leave the requests as completed.
    const abandoned: string[] = []
    page.on('requestfailed', (sent) => {
      if (sent.url().includes(`/api/v1/runs/${first}`)) abandoned.push(sent.failure()?.errorText ?? '')
    })

    // The delay is deliberately *shorter* than this stack's three-second deadline, so the deadline
    // cannot be what ends the request: only the operator navigating away can. Nothing else is
    // touched, so the list and the second run answer normally.
    await arm(request, { method: 'GET', path: `/api/v1/runs/${first}`, mode: 'slow', delay_ms: 2_000 })

    await page.goto(`/runs?run=${first}`)
    // The read of the first run is in flight and will not answer for two seconds.
    await expect(page.getByRole('link', { name: second })).toBeVisible({ timeout: 30_000 })
    // Switching to the second run abandons it.
    await page.getByRole('link', { name: second }).click()

    await expect(page.getByText(second, { exact: false }).first()).toBeVisible()
    await expect(page.getByText(/selected run/i).first()).toBeVisible()
    // A cancelled request is silent: no error state, no outage wording.
    await expect(errorState(page)).toHaveCount(0)

    // And it is really cancelled: the transport reports it as failed, not as a completed request that
    // was quietly dropped.
    await expect
      .poll(() => abandoned.length, {
        timeout: 15_000,
        message: 'switching runs must cancel the request it abandoned',
      })
      .toBeGreaterThan(0)
    expect(abandoned.some((text) => /ABORT|ERR_/.test(text)), `browser said: ${abandoned.join(', ')}`).toBeTruthy()

    // Wait past the point where the abandoned response would have arrived, and check it did not
    // replace what the operator is looking at.
    await page.waitForTimeout(8_000)
    await expect(errorState(page)).toHaveCount(0)
    await expect(page.getByText(second, { exact: false }).first()).toBeVisible()
    await expect(page.getByText(/selected run/i).first()).toBeVisible()

    // Leaving the screen with a read in flight is the other cancellation: it must not leave an error
    // behind either.
    await arm(request, { method: 'GET', path: `/api/v1/runs/${second}`, mode: 'slow', delay_ms: 6_000 })
    await page.goto(`/runs?run=${second}`)
    await page.waitForTimeout(500)
    await page.goto('/workflows')
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
    await page.waitForTimeout(7_000)
    await expect(errorState(page)).toHaveCount(0)

    await disarm(request)
    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
