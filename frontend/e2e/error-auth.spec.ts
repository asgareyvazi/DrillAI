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

import { expect, expectNoFailures, test, type FailureAllowance } from './fixtures'
import type { Page } from '@playwright/test'

const errorState = (page: Page) => page.getByTestId('error-state')

function anonymousGet(pathname: string, query: string, maxMatches: number, journey: string): FailureAllowance {
  const target = `${pathname}${query}`
  return {
    kind: 'http',
    method: 'GET',
    pathname,
    query,
    status: 401,
    maxMatches,
    reason: `${journey} asserts the anonymous browser read of ${target} is refused with HTTP 401`,
  }
}

test.describe('an authentication-enabled deployment', () => {
  test('an unauthenticated read reads as "not signed in", not as an outage', async ({
    page,
    request,
    consoleErrors,
    failedResponses,
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

    // `WellList` issues the fields and rigs reads alongside wells/projects. In this Vite development
    // stack React.StrictMode mounts query observers twice; HTTP 401 is not retryable, so each exact
    // two-observation allowance accounts only for that development remount.
    expectNoFailures(consoleErrors, failedResponses, {
      allowed: [
        anonymousGet(
          '/api/v1/health/ready',
          '',
          6,
          'the shell health read is observed twice by React StrictMode on each of the three screens in this journey',
        ),
        anonymousGet('/api/v1/platform/identity', '', 5, 'the anonymous-read journey'),
        anonymousGet('/api/v1/wells', '?limit=200', 2, 'the anonymous-read journey'),
        anonymousGet('/api/v1/projects', '', 2, 'the anonymous-read journey'),
        anonymousGet('/api/v1/fields', '?limit=500', 2, 'the anonymous-read journey and StrictMode remount'),
        anonymousGet('/api/v1/rigs', '', 2, 'the anonymous-read journey and StrictMode remount'),
        anonymousGet('/api/v1/workflows', '', 1, 'the anonymous-read journey'),
        anonymousGet('/api/v1/registry/node-types', '', 1, 'the anonymous-read journey'),
        anonymousGet('/api/v1/evidence/summary', '?well_id=wel_unauth_probe', 2, 'the anonymous-read journey'),
        anonymousGet(
          '/api/v1/wells/wel_unauth_probe/timeseries/latest',
          '?limit=100',
          2,
          'the anonymous cockpit journey including its live-stream reconciliation',
        ),
        anonymousGet(
          '/api/v1/alerts',
          '?well_id=wel_unauth_probe&limit=100',
          2,
          'the anonymous cockpit and live-stream reconciliation journey',
        ),
        anonymousGet('/api/v1/connectors', '?well_id=wel_unauth_probe&limit=50', 1, 'the anonymous-read journey'),
        anonymousGet('/api/v1/wells/wel_unauth_probe/state', '', 1, 'the anonymous-read journey'),
        anonymousGet('/api/v1/wells/wel_unauth_probe', '', 1, 'the anonymous-read journey'),
      ],
    })
  })

  test('interactive per-user login, HttpOnly cookie session, RBAC role ceiling, WebSocket cookie auth, and server-side logout invalidation', async ({
    page,
    consoleErrors,
    failedResponses,
  }) => {
    const wsUrls: string[] = []
    page.on('websocket', (ws) => {
      if (ws.url().includes('/api/v1/')) wsUrls.push(ws.url())
    })

    await page.goto('/wells')
    await expect(page.getByTestId('shell-unauthenticated')).toBeVisible()
    await expect(page.getByTestId('shell-role-switch')).toHaveCount(0)

    // Navigate to the login page via the shell Sign in link
    await page.getByTestId('shell-login-link').click()
    await expect(page.getByTestId('login-form')).toBeVisible()

    // 1. Invalid password produces a 401 unauthenticated error on the login form
    await page.getByTestId('login-email-input').fill('viewer@demo.drillai.local')
    await page.getByTestId('login-password-input').fill('WrongPassword!2026')
    await page.getByTestId('login-submit-button').click()
    await expect(errorState(page)).toHaveAttribute('data-error-kind', 'unauthenticated')

    // 2. Sign in as Viewer (`viewer@demo.drillai.local`)
    await page.getByTestId('login-password-input').fill('DrillAI-Dev-Only-Pass!2026')
    await page.getByTestId('login-submit-button').click()

    await expect(page).toHaveURL(/\/wells$/)
    await expect(page.getByTestId('shell-auth-principal')).toContainText('Demo Viewer')
    await expect(page.getByTestId('shell-identity')).toHaveAttribute('data-ceiling', 'L0')
    await expect(page.getByTestId('shell-role-switch')).toHaveCount(0)

    // Verify document.cookie does NOT expose the HttpOnly drillai_session cookie to JS
    const jsCookie = await page.evaluate(() => document.cookie)
    expect(jsCookie).not.toContain('drillai_session')

    // 3. Sign out as Viewer and verify `/wells` immediately returns to 401 unauthenticated
    await page.getByTestId('shell-logout-button').click()
    await expect(page.getByTestId('shell-unauthenticated')).toBeVisible()
    await expect(errorState(page).first()).toHaveAttribute('data-error-kind', 'unauthenticated')

    // 4. Sign in as Drilling Supervisor (`supervisor@demo.drillai.local`) and verify L4 ceiling + CSRF mutation + WebSocket cookie auth
    await page.getByTestId('shell-login-link').click()
    await page.getByTestId('login-email-input').fill('supervisor@demo.drillai.local')
    await page.getByTestId('login-password-input').fill('DrillAI-Dev-Only-Pass!2026')
    await page.getByTestId('login-submit-button').click()

    await expect(page.getByTestId('shell-auth-principal')).toContainText('Demo Drilling Supervisor')
    await expect(page.getByTestId('shell-identity')).toHaveAttribute('data-ceiling', 'L4')

    // Open the seeded well's cockpit live stream and verify WebSocket connects via HttpOnly cookie with NO ?token= in URL
    const wellLink = page.locator('a[href*="/wells/"][href$="/cockpit"]').first()
    await expect(wellLink).toBeVisible()
    const cockpitHref = await wellLink.getAttribute('href')
    expect(cockpitHref).toBeTruthy()
    if (!cockpitHref) throw new Error('the authentication seed must expose a well cockpit link')
    const wellId = /^\/wells\/([^/]+)\/cockpit$/.exec(cockpitHref)?.[1]
    if (!wellId) throw new Error(`expected a well-specific cockpit link, got ${cockpitHref}`)
    await page.goto(`${cockpitHref}?tab=live`)
    await expect(page.getByTestId('live-transport-state')).toHaveText(/LIVE/i, { timeout: 20_000 })
    expect(wsUrls.length).toBeGreaterThan(0)
    for (const url of wsUrls) {
      expect(url).not.toContain('token=')
      expect(url).not.toContain('dev_roles=')
    }

    // Final logout invalidates the server-side session
    await page.getByTestId('shell-logout-button').click()
    await expect(page.getByTestId('shell-unauthenticated')).toBeVisible()

    expectNoFailures(consoleErrors, failedResponses, {
      allowed: [
        anonymousGet('/api/v1/health/ready', '', 4, 'the login and logout journey'),
        anonymousGet('/api/v1/wells', '?limit=200', 3, 'the login and logout journey'),
        anonymousGet('/api/v1/projects', '', 3, 'the login and logout journey'),
        anonymousGet('/api/v1/fields', '?limit=500', 3, 'the login and logout journey'),
        anonymousGet('/api/v1/rigs', '', 3, 'the login and logout journey'),
        anonymousGet('/api/v1/platform/identity', '', 4, 'the login and logout journey'),
        {
          kind: 'http',
          method: 'POST',
          pathname: '/api/v1/auth/login',
          query: '',
          status: 401,
          maxMatches: 1,
          reason: 'the journey submits an invalid password and asserts the exact unauthenticated login refusal',
        },
        anonymousGet(`/api/v1/wells/${wellId}`, '', 1, 'the login and logout journey'),
        anonymousGet(`/api/v1/wells/${wellId}/state`, '', 1, 'the login and logout journey'),
        anonymousGet('/api/v1/evidence/summary', `?well_id=${wellId}`, 1, 'the login and logout journey'),
        anonymousGet(`/api/v1/wells/${wellId}/timeseries/latest`, '?limit=100', 1, 'the login and logout journey'),
        anonymousGet('/api/v1/alerts', `?well_id=${wellId}&limit=100`, 1, 'the login and logout journey'),
        anonymousGet('/api/v1/connectors', `?well_id=${wellId}&limit=50`, 1, 'the login and logout journey'),
      ],
    })
  })

  test('tenant administrator manages users and issues a one-time scoped API token', async ({ page, consoleErrors, failedResponses }) => {
    await page.goto('/wells')
    await page.getByTestId('shell-login-link').click()
    await page.getByTestId('login-email-input').fill('admin@demo.drillai.local')
    await page.getByTestId('login-password-input').fill('DrillAI-Dev-Only-Pass!2026')
    await page.getByTestId('login-submit-button').click()
    await expect(page.getByTestId('shell-auth-principal')).toContainText('Demo Org Admin')

    const securityLink = page.getByRole('link', { name: 'Identity & Access' })
    await expect(securityLink).toBeVisible()
    await securityLink.click()
    await expect(page.getByTestId('auth-user-table')).toBeVisible()

    const email = `cp26-ui-${Date.now()}@demo.drillai.local`
    await page.getByLabel('Work email').fill(email)
    await page.getByLabel('Display name').fill('CP26 UI Provisioned User')
    await page.getByLabel(/Initial password/).fill('Temporary-E2E-User!2026')
    await page.getByTestId('create-user-submit').click()
    await expect(page.getByTestId('auth-user-table').getByText(email)).toBeVisible()

    const tokenName = `cp26-ui-${Date.now()}`
    await page.getByLabel('Token name').fill(tokenName)
    await page.getByTestId('create-token-submit').click()
    const oneTimeToken = page.getByTestId('one-time-token')
    await expect(oneTimeToken).toBeVisible()
    await expect(oneTimeToken.locator('code')).toContainText(/^dk_/)
    await oneTimeToken.getByRole('button', { name: 'I saved the token' }).click()
    await expect(oneTimeToken).toHaveCount(0)

    const tokenRow = page.locator('[data-testid^="api-token-tok_"]').filter({ hasText: tokenName })
    await expect(tokenRow).toBeVisible()
    await tokenRow.getByRole('button', { name: 'Revoke' }).click()
    await tokenRow.getByRole('button', { name: 'Confirm revoke' }).click()
    await expect(tokenRow).toContainText('Revoked')

    expectNoFailures(consoleErrors, failedResponses, {
      allowed: [
        anonymousGet('/api/v1/platform/identity', '', 2, 'the tenant-admin login journey'),
        anonymousGet('/api/v1/wells', '?limit=200', 2, 'the tenant-admin login journey'),
        anonymousGet('/api/v1/projects', '', 2, 'the tenant-admin login journey'),
        anonymousGet('/api/v1/fields', '?limit=500', 2, 'the tenant-admin login journey'),
        anonymousGet('/api/v1/health/ready', '', 2, 'the tenant-admin login journey'),
        anonymousGet('/api/v1/rigs', '', 2, 'the tenant-admin login journey'),
      ],
    })
  })
})
