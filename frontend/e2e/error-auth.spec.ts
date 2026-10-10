/**
 * Checkpoint 4 — journey B: the deployment where authentication is actually enforced.
 *
 * Every other journey runs against the development-identity service, where a role header is enough
 * for the server to know who is asking. That is not the deployment operators use, and it is where a
 * whole class of confusion hides: when every request is refused with 401, an interface that reports
 * "the backend is unreachable" sends people to look at a service that is running perfectly well.
 *
 * So this spec runs against a second instance of the same API with `DRILLAI_AUTH_ENABLED=true` and no
 * credential configured in the client. The refusals are the real authorization path's answers, and
 * the assertions are about what a signed-out operator reads. The dev-role header the client sends is
 * deliberately still sent: the point of the journey is that the server does not accept it, and that
 * the page does not behave as though it had been accepted.
 */

import { appConsoleErrors, test, expect } from './fixtures'
import type { Page } from '@playwright/test'

const errorState = (page: Page) => page.getByTestId('error-state')

test.describe('an authentication-enabled deployment', () => {
  test('an unauthenticated read reads as "not signed in", not as an outage', async ({
    page,
    request,
    consoleErrors,
  }) => {
    // First: the server really refuses an anonymous read. Without this the journey could pass against
    // a service that simply answers 200 to everything.
    const answer = await request.get('/api/v1/wells')
    expect(answer.status(), 'an anonymous read must be refused by the real authorization path').toBe(401)
    expect(answer.headers()['www-authenticate'], 'the refusal must advertise the scheme').toContain('Bearer')

    await page.goto('/wells')

    // The wells list is two reads (the wells and the project breakdown), and both are refused; the
    // assertions are made against the first of them, and then against the page as a whole.
    const state = errorState(page).first()
    await expect(state).toBeVisible({ timeout: 30_000 })
    const kinds = await errorState(page).evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute('data-error-kind')),
    )
    expect(kinds.length).toBeGreaterThan(0)
    expect(new Set(kinds), 'every refusal on the page must be the same refusal').toEqual(
      new Set(['unauthenticated']),
    )
    await expect(state).toHaveAttribute('data-error-kind', 'unauthenticated')
    await expect(state).toHaveAttribute('data-http-status', '401')
    await expect(state).toContainText(/signed in|sign in/i)
    // The three confusions this journey exists to prevent.
    await expect(state).not.toContainText(/unreachable/i)
    await expect(state).not.toContainText(/not found/i)
    await expect(state).not.toContainText(/permission/i)
    // Repeating the request without a credential changes nothing, so no retry is offered — and the
    // refusal names the request so somebody can look it up.
    await expect(state.getByRole('button', { name: /retry/i })).toHaveCount(0)
    await expect(state.getByTestId('error-request-id')).not.toBeEmpty()

    // Another screen says the same thing rather than inventing a different story.
    await page.goto('/workflows')
    await expect(errorState(page)).toHaveAttribute('data-error-kind', 'unauthenticated')

    // And the Well Cockpit Operational Monitor (`?tab=live`) surfaces 401 unauthenticated + ERROR
    // stream transport state (4401 refusal) without claiming the backend is unreachable.
    await page.goto('/wells/wel_unauth_probe/cockpit?tab=live')
    await expect(errorState(page).first()).toHaveAttribute('data-error-kind', 'unauthenticated')
    await expect(page.getByTestId('live-transport-state')).toHaveText(/ERROR/i)

    expect(appConsoleErrors(consoleErrors), `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})
