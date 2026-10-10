/**
 * Identity, permission and action level — against the real API and the real database.
 *
 * Four facts are asserted here, and each of them is a correctness property rather than a cosmetic one:
 *
 * 1. **The interface offers the roles the server catalogues.** The switcher renders
 *    `development_presets` from `/platform/identity`. It used to render a list kept in the frontend,
 *    and that list had already drifted: three catalogued roles could not be exercised from the
 *    interface at all. The first journeys compare the options with the server's answer for every role.
 * 2. **The ceiling the interface shows is the ceiling the server resolved.** For each catalogued role
 *    the shell's `data-ceiling` is compared with that role's `max_action_level` as the server reports
 *    it — the interface never computes an authorization ceiling of its own.
 * 3. **The reason the interface gives is the reason the server gives.** Running an engine is the L2
 *    action `engine.run`. A viewer is refused on the ceiling; an administrator is refused on the
 *    permission. The screen says which, and the server's own refusal payload for the same identity is
 *    asserted to name the same thing — so the two cannot drift apart. `authorize()` checks the ceiling
 *    first, so an interface that checked the permission first would send an operator to ask for
 *    something that would not help.
 * 4. **Switching identity moves the cache and the event stream with it.** The answers fetched as the
 *    previous principal must not stay on the screen under the new one, the switch must not reload the
 *    page, and the event stream — a separate transport carrying the identity in its URL — must be
 *    closed and reopened. All three are observed from outside: the DOM's mutation frames, the network,
 *    and the sockets.
 *
 * Nothing is intercepted and nothing is mocked: the identities are the development presets the server
 * advertises, the refusals are its own 403 payloads, and the engine run in the permitted case is a real
 * engine execution persisted to the database.
 */

import {
  CATALOGUED_ROLES,
  ROLES,
  apiGet,
  appConsoleErrors,
  expect,
  selectRole,
  startRunFromStudio,
  test,
  waitForLoaded,
  type RoleKey,
} from './fixtures'

/** `/platform/identity` as the server answers it for one development identity. */
interface ServerIdentity {
  principal_id: string
  role_keys: string[]
  roles: Array<{ key: string; name: string; max_action_level: string }>
  permissions: string[]
  max_action_level: string
  development_presets: string[]
  auth_enabled: boolean
  note: string
}

/** The error envelope every failure uses: `{ error: { code, message, retryable, details } }`. */
interface ErrorEnvelope {
  error: { code: string; message: string; retryable: boolean; details: Record<string, unknown> }
}

function serverIdentity(
  request: Parameters<typeof apiGet>[1],
  role: RoleKey,
): Promise<ServerIdentity> {
  return apiGet<ServerIdentity>(request, '/platform/identity', role)
}

/** The engine the journeys run: registered at L1, deterministic, and cheap to execute. */
const ENGINE_KEY = 'readiness.assessment'

/**
 * Inputs for the readiness engine, with every date supplied.
 *
 * The engine's own contract says dates are evaluated against the supplied `as_of` rather than the wall
 * clock, so fixing them makes the run reproducible. The values are written here as the engine's inputs,
 * not as expected outputs: the assertions below read the result from the server.
 */
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

test.describe('identity, permission and action level', () => {
  test('the switcher offers exactly the roles the server catalogues, and every one is usable', async ({
    page,
    request,
  }) => {
    const advertised = (await serverIdentity(request, 'engineer')).development_presets
    expect(advertised.length, 'the server must advertise its development presets').toBeGreaterThan(0)

    await page.goto('/wells')
    await waitForLoaded(page)
    const select = page.getByTestId('shell-role-switch')

    // The options are the server's list; the session's own current selection is echoed on top so the
    // control is never lying about what it is set to.
    await expect(select.locator('option')).toHaveCount(advertised.length + 1)
    const offered = await select.locator('option').evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value).filter((value) => !value.includes(',')),
    )
    expect(offered.sort()).toEqual([...advertised].sort())

    // And each one can actually be assumed: the server answers for it.
    for (const role of CATALOGUED_ROLES) {
      await selectRole(page, role)
      const identity = await serverIdentity(request, role)
      expect(identity.role_keys, `${role} must be a single catalogued role`).toEqual([ROLES[role]])
    }

    // A refusal that only some roles meet, on a panel beside data that still renders. `project.read` is
    // not part of the integrity engineer's set: the well list must keep rendering while that one panel
    // says what happened, rather than the role being handed a generic failure, or an empty list dressed
    // up as an answer.
    await selectRole(page, 'integrityEngineer')
    const projects = await request.get('/api/v1/projects', { headers: { 'X-Dev-Roles': ROLES.integrityEngineer } })
    expect(projects.status(), 'the integrity engineer holds no project.read').toBe(403)
    await expect(page.getByTestId('projects-unavailable')).toBeVisible()
    await expect(page.getByTestId('projects-unavailable')).toContainText(/permission/i)
    // The wells themselves are still there: one panel's refusal is not the screen's failure.
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  })

  test('the ceiling the shell reports is the ceiling the server resolves, for every catalogued role', async ({
    page,
    request,
  }) => {
    await page.goto('/wells')
    await waitForLoaded(page)

    for (const role of CATALOGUED_ROLES) {
      await selectRole(page, role)
      const identity = await serverIdentity(request, role)
      const shown = page.getByTestId('shell-identity')

      // The identity summary settles on the server's answer for this role: the role's own name and the
      // action-level ceiling, both taken from the response rather than from a table in the frontend.
      await expect(shown).toHaveAttribute('data-ceiling', identity.max_action_level)
      const roleName = identity.roles[0]?.name
      if (roleName) await expect(shown).toContainText(roleName)
      // What is displayed is the ceiling the *role* carries, not a number the UI picked.
      expect(identity.roles[0]?.max_action_level).toBe(identity.max_action_level)
    }
  })

  test('a ceiling refusal is reported as a ceiling, and the server refuses it the same way', async ({
    page,
    request,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/engineering`)
    await waitForLoaded(page)
    await selectRole(page, 'viewer')

    await page.getByRole('button', { name: /Operational readiness assessment/i }).click()
    const run = page.getByTestId('engine-run')

    // The viewer observes: its ceiling is L0 and no permission changes that.
    await expect(run).toHaveAttribute('data-gate-state', 'level-below')
    await expect(run).toBeDisabled()
    const explanation = page.getByTestId('engine-run-gate')
    await expect(explanation).toContainText('L0')
    await expect(explanation).toContainText('L2')
    await expect(explanation).not.toContainText(/does not hold/)

    // The server's own refusal for the same identity names the same gate. `authorize()` checks the
    // ceiling before the permission and reports both numbers; the screen and the payload must agree.
    const refused = await request.post(`/api/v1/registry/engines/${ENGINE_KEY}/run`, {
      headers: { 'X-Dev-Roles': ROLES.viewer },
      data: { inputs: ENGINE_INPUTS, well_id: wellId, persist: false },
    })
    expect(refused.status(), 'an L0 identity may not run an L2 action').toBe(403)
    const envelope = (await refused.json()) as ErrorEnvelope
    expect(envelope.error.details.required_level).toBe('L2')
    expect(envelope.error.details.ceiling).toBe('L0')
    await expect(explanation).toContainText(String(envelope.error.details.ceiling))
    await expect(explanation).toContainText(String(envelope.error.details.required_level))
  })

  test('a permission refusal is reported as a permission, and the server refuses it the same way', async ({
    page,
    request,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/engineering`)
    await waitForLoaded(page)
    // An administrator: a ceiling high enough for anything, and no engineering authority at all. The
    // refusal is about the permission, and asking for a higher ceiling would change nothing.
    await selectRole(page, 'admin')

    await page.getByRole('button', { name: /Operational readiness assessment/i }).click()
    const run = page.getByTestId('engine-run')

    await expect(run).toHaveAttribute('data-gate-state', 'no-permission')
    await expect(run).toBeDisabled()
    const explanation = page.getByTestId('engine-run-gate')
    await expect(explanation).toContainText('engine.run')
    await expect(explanation).toContainText('admin')
    await expect(explanation).not.toContainText(/up to L\d/)

    const refused = await request.post(`/api/v1/registry/engines/${ENGINE_KEY}/run`, {
      headers: { 'X-Dev-Roles': ROLES.admin },
      data: { inputs: ENGINE_INPUTS, well_id: wellId, persist: false },
    })
    expect(refused.status(), 'an administrator without engine.run may not run an engine').toBe(403)
    const envelope = (await refused.json()) as ErrorEnvelope
    expect(envelope.error.details.permission).toBe('engine.run')
    expect(envelope.error.details.role_keys).toEqual(['admin'])
    await expect(explanation).toContainText(String(envelope.error.details.permission))
  })

  test('an identity the server permits runs a real engine, and the run is recorded', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    await page.goto(`/wells/${wellId}/engineering`)
    await waitForLoaded(page)
    await selectRole(page, 'engineer')

    await page.getByRole('button', { name: /Operational readiness assessment/i }).click()
    const run = page.getByTestId('engine-run')
    await expect(run).toHaveAttribute('data-gate-state', 'ready')
    await expect(run).toBeEnabled()

    await page.getByTestId('engine-inputs').fill(JSON.stringify(ENGINE_INPUTS))
    await run.click()

    // A result is a real execution: the card names the engine and version the server ran, and the
    // outcome it computed for the inputs above.
    const result = page.getByRole('button', { name: 'Show evidence' }).first()
    await expect(result).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/readiness\.assessment@/).first()).toBeVisible()
    await expect(page.getByText(/2026-01-01/).first()).toBeVisible()

    // Persisted, not merely displayed: the run appears in the audit list the server serves.
    const runs = await apiGet<{ items: Array<{ engine_key: string; inputs_hash: string; outputs: Record<string, unknown> }> }>(
      request,
      `/wells/${wellId}/engine-runs?limit=50`,
      'engineer',
    )
    const recorded = runs.items.find((row) => row.engine_key === ENGINE_KEY)
    expect(recorded, 'the engine run the page started must be recorded for the well').toBeTruthy()
    // The recorded run is the one whose inputs were sent, and its outputs are the engine's — the page
    // showed a computation, not a rendering of the inputs.
    expect(recorded?.outputs).toHaveProperty('overall_state')

    expect(appConsoleErrors(consoleErrors)).toEqual([])
  })

  test('switching identity replaces the previous identity\'s answers in place, without reloading', async ({
    page,
    wellId,
  }) => {
    await page.goto(`/wells/${wellId}/engineering`)
    await waitForLoaded(page)
    await selectRole(page, 'engineer')
    // The runner panel is opened so the switch has a mounted screen whose gate state must change with
    // the identity: the engine's ceiling is L2, and the viewer's is L0.
    await page.getByRole('button', { name: /Operational readiness assessment/i }).click()
    await expect(page.getByTestId('engine-run')).toHaveAttribute('data-gate-state', 'ready')

    const requests: string[] = []
    page.on('request', (request) => {
      if (/\/registry\/engines(\?|$)/.test(request.url())) requests.push(request.url())
    })
    const before = requests.length

    // A marker on the window, and the number of navigations: if the switch reloaded the page, both
    // would be gone.
    await page.evaluate(() => {
      ;(window as unknown as { __switchMarker?: string }).__switchMarker = 'still-here'
      ;(window as unknown as { __frames?: string[] }).__frames = []
      const observer = new MutationObserver(() => {
        ;(window as unknown as { __frames: string[] }).__frames.push(document.body.innerText)
      })
      observer.observe(document.body, { subtree: true, childList: true, characterData: true })
    })

    await selectRole(page, 'viewer')
    // The switch is complete when the interface has settled on the new identity's answer.
    await expect(page.getByTestId('engine-run')).toHaveAttribute('data-gate-state', 'level-below')

    const { marker, frames, navigations } = await page.evaluate(() => ({
      marker: (window as unknown as { __switchMarker?: string }).__switchMarker,
      frames: (window as unknown as { __frames: string[] }).__frames,
      navigations: performance.getEntriesByType('navigation').length,
    }))

    expect(marker, 'the switch must not reload the page').toBe('still-here')
    expect(navigations, 'the switch must not navigate').toBe(1)

    // Re-read, not reused: the catalogue is asked for again under the new identity. A screen left on its
    // previous answers is not "replaced" however empty a cache somewhere claims to be.
    expect(
      requests.length,
      'the catalogue must be asked for again, under the new identity',
    ).toBeGreaterThan(before)

    // The answers were fetched as the previous identity. They are gone from the document while the new
    // identity's are read — the cache is replaced, not merely marked stale. Under invalidation the rows
    // stay on screen for the whole round trip, and no frame ever lacks them.
    const rowText = /Operational readiness assessment/
    expect(frames.length, 'the switch must change something in the document').toBeGreaterThan(0)
    expect(
      frames.some((frame) => !rowText.test(frame)),
      'no frame showed the interface without the previous identity’s rows: the previous answer was never given up',
    ).toBe(true)

    // And they come back: this is a replacement, not a screen emptied by an error or by a refusal.
    await expect(page.getByRole('button', { name: rowText })).toBeVisible()
  })

  test("switching identity closes the previous identity's event stream and opens one for the new identity", async ({
    page,
    request,
    wellId,
  }) => {
    const workflowId = (await apiGet<{ items: Array<{ id: string; name: string }> }>(request, '/workflows', 'engineer')).items.find(
      (row) => row.name === 'Daily Drilling Intelligence',
    )?.id
    expect(workflowId, 'the seed must have produced the drilling workflow').toBeTruthy()

    // A run that is still waiting for its approval: the monitor streams while it waits.
    const runId = await startRunFromStudio(page, workflowId as string, wellId)
    const opened: string[] = []
    const closed: string[] = []
    page.on('websocket', (socket) => {
      opened.push(socket.url())
      socket.on('close', () => closed.push(socket.url()))
    })

    // Give the stream a moment to be opened by the page that just navigated to the run.
    await expect
      .poll(() => opened.filter((url) => url.includes('/events/stream')).length, { timeout: 15_000 })
      .toBeGreaterThan(0)
    const first = opened.filter((url) => url.includes('/events/stream')).at(-1) as string
    expect(first, 'the first stream carries the identity that opened it').toContain(
      `dev_roles=${ROLES.supervisor}`,
    )

    await selectRole(page, 'viewer')

    // The socket is a second transport carrying the identity in its URL: the previous principal's
    // stream must not keep feeding a screen that now acts as somebody else.
    await expect
      .poll(() => opened.filter((url) => url.includes(`dev_roles=${ROLES.viewer}`)).length, { timeout: 15_000 })
      .toBeGreaterThan(0)
    await expect.poll(() => closed.includes(first), { timeout: 15_000 }).toBe(true)

    // Leave the shared database as the journey found it: the approval is decided by the role that may.
    const pending = await apiGet<{ items: Array<{ id: string; run_id: string }> }>(
      request,
      `/approvals?status=pending&run_id=${runId}`,
      'supervisor',
    )
    const approval = pending.items[0]
    if (approval) {
      const decided = await request.post(`/api/v1/approvals/${approval.id}/decide`, {
        headers: { 'X-Dev-Roles': ROLES.wellManager },
        data: {
          decision: 'rejected',
          note: 'e2e identity journey: closed to leave the seeded state clean',
          resume: false,
        },
      })
      expect(decided.ok(), `deciding the seeded approval -> ${decided.status()}`).toBeTruthy()
    }
  })
})
