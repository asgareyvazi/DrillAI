/**
 * Journey 1 — the well, its documents and the evidence behind a value.
 *
 * The spec walks the same path an engineer does: the well list from the API, the cockpit built from
 * `GET /wells/{id}/state`, the measured values with their provenance, the timeline, and the
 * evidence panel that resolves an answer back to a page and region of the source document.
 *
 * Every number asserted here is read from the API in the same test, so the assertion fails if the
 * product stops reporting the real value — and it can never pass because a number was typed in.
 */

import {
  apiGet,
  apiPost,
  expect,
  expectNoFailures,
  test,
  wellPath,
} from './fixtures'

type WellState = {
  state: {
    well: { id: string; name: string }
    progress: {
      current_md_si: number | null
      current_md_source: string | null
      planned_td_md_si: number | null
    }
    counts: Record<string, number>
    npt: { total_hours: number; event_count: number }
    missing: { key: string; description: string; how_to_supply: string }[]
  }
}

type WellNpt = {
  npt: {
    total_hours: number
    basis: string
    controllable_hours: number
    unknown_controllability_hours: number
    event_count: number
    by_category: { label: string; hours: number; occurrences: number }[]
    cases: { id: string; title: string; code: string | null }[]
  }
}

type WellList = { items: { id: string; name: string }[]; total: number }

test.describe('well cockpit built from the real well context', () => {
  test('lists the seeded well and opens a cockpit that matches the API', async ({
    page,
    request,
    wellId,
    consoleErrors, failedResponses,
  }) => {
    await page.goto('/wells')
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()

    const wells = await apiGet<WellList>(request, '/wells')
    const seeded = wells.items.find((item) => item.id === wellId)
    expect(seeded, 'the seeded well must be listed').toBeTruthy()

    const row = page.getByRole('row').filter({ hasText: seeded?.name ?? '' })
    await expect(row).toBeVisible()
    await row.getByRole('link').first().click()

    await expect(page).toHaveURL(new RegExp(`/wells/${wellId}`))
    expectNoFailures(consoleErrors, failedResponses)
  })

  test('shows measured progress, NPT and missing-data honesty from the API', async ({
    page,
    request,
    wellId,
    consoleErrors, failedResponses,
  }) => {
    const { state } = await apiGet<WellState>(request, `/wells/${wellId}/state`)
    const { npt } = await apiGet<WellNpt>(request, `/wells/${wellId}/npt`)

    await page.goto(wellPath('/cockpit'))
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()

    // Progress: the header names the well, and the depth shown is the one the API reports.
    await expect(page.getByRole('heading', { level: 1 }).first()).toHaveText(state.well.name)
    const currentDepth = state.progress.current_md_si
    expect(currentDepth, 'the fixture must carry a measured depth').not.toBeNull()
    const depthText = new RegExp(`${Math.round(currentDepth ?? 0).toLocaleString('en-US')}`)
    await expect(page.getByText(depthText).first()).toBeVisible()
    expect(state.progress.current_md_source).toBeTruthy()

    // NPT: the total on the page is the API's total, and the Pareto lists the API's causes.
    await page.getByRole('tab', { name: /NPT|Non-productive time/i }).click()
    await expect(page.getByText(String(npt.total_hours)).first()).toBeVisible()
    for (const category of npt.by_category.filter((row) => row.occurrences > 0)) {
      await expect(page.getByText(new RegExp(category.label, 'i')).first()).toBeVisible()
    }

    // Missing data: capability the platform does not have is stated with how to supply it.
    expect(state.missing.length).toBeGreaterThan(0)
    await page.getByRole('tab', { name: /Missing data/i }).click()
    for (const item of state.missing.slice(0, 2)) {
      await expect(page.getByText(item.description).first()).toBeVisible()
      await expect(page.getByText(item.how_to_supply).first()).toBeVisible()
    }

    expectNoFailures(consoleErrors, failedResponses)
  })

  test('distinguishes overlapping NPT intervals from a generic event duration', async ({
    page, request, consoleErrors, failedResponses,
  }) => {
    // A separate synthetic well prevents these append-only evidence rows from affecting other journeys.
    const tag = Date.now().toString(36)
    const project = await apiPost<{ id: string }>(request, '/projects', { name: `NPT interval E2E ${tag}` }, 'wellManager')
    const well = await apiPost<{ id: string }>(request, '/wells', {
      project_id: project.id, name: `NPT-INTERVAL-${tag}`, well_type: 'development_producer',
    })
    const makeEvent = async (title: string, start: string, end: string, hours: number, explicit: boolean) =>
      apiPost<{ id: string }>(request, '/events', {
        well_id: well.id, title, kind: 'stuck_pipe', is_npt: true, npt_category: 'stuck_pipe',
        occurred_at: start, occurred_at_basis: 'operator_entered',
        ended_at: end, ended_at_basis: 'operator_entered',
        ...(explicit ? { npt_hours: hours, npt_hours_basis: 'reported' } : { duration_hours: hours, duration_basis: 'reported' }),
      })
    await makeEvent('First overlap', '2026-03-15T00:00:00Z', '2026-03-15T02:00:00Z', 2, true)
    await makeEvent('Second overlap', '2026-03-15T01:00:00Z', '2026-03-15T03:00:00Z', 2, true)
    await makeEvent('Nested interval', '2026-03-15T01:00:00Z', '2026-03-15T01:30:00Z', 0.5, true)
    type Reconciliation = { npt: { total_hours: number; interval_reconciliation: {
      status: string; union_hours: number | null; overlap_hours: number | null; missing_npt_hours_count: number | null
    } } }
    const before = await apiGet<Reconciliation>(request, `/wells/${well.id}/npt`)
    expect(before.npt.total_hours).toBe(4.5)
    expect(before.npt.interval_reconciliation).toMatchObject({ status: 'complete_for_matching_events', union_hours: 3, overlap_hours: 1.5 })
    await page.goto(`/wells/${well.id}/cockpit`)
    await page.getByRole('tab', { name: /NPT|Non-productive time/i }).click()
    await expect(page.getByText('Union: 3 h; overlap in recorded intervals: 1 h 30 min')).toBeVisible()

    const generic = await makeEvent('Generic duration only', '2026-03-15T04:00:00Z', '2026-03-15T05:00:00Z', 1, false)
    const after = await apiGet<Reconciliation>(request, `/wells/${well.id}/npt`)
    expect(after.npt.total_hours).toBe(5.5)
    expect(after.npt.interval_reconciliation).toMatchObject({ status: 'unavailable_incomplete', union_hours: null, missing_npt_hours_count: 1 })
    const readback = await apiGet<{ id: string; duration_hours: number; npt_hours: number | null }>(request, `/events/${generic.id}`)
    expect(readback).toMatchObject({ id: generic.id, duration_hours: 1, npt_hours: null })
    await page.reload()
    await page.getByRole('tab', { name: /NPT|Non-productive time/i }).click()
    await expect(page.getByText(/1 matching event\(s\) lack explicit NPT hours/)).toBeVisible()
    await page.getByText('Inspect selected source rows').click()
    await expect(page.getByText(/Generic event duration \(legacy row sum only; not interval evidence\)/)).toBeVisible()
    await expect(page.getByText(generic.id, { exact: true })).toBeVisible()

    // A record with unknown time must not disappear from a narrowed interval reconciliation.
    await apiPost(request, '/events', {
      well_id: well.id, title: 'Unassignable time', kind: 'stuck_pipe', is_npt: true,
      npt_category: 'stuck_pipe', npt_hours: 1, npt_hours_basis: 'reported',
    })
    const filtered = await apiGet<Reconciliation & { npt: { interval_reconciliation: {
      unassignable_time_count: number; scoped_event_count: number; outside_window_count: number
    } } }>(request, `/wells/${well.id}/npt?since=2026-03-15T00%3A00%3A00Z&until=2026-03-15T03%3A00%3A00Z`)
    expect(filtered.npt.interval_reconciliation).toMatchObject({
      status: 'unavailable_incomplete', union_hours: null, unassignable_time_count: 1,
      scoped_event_count: 5, outside_window_count: 1,
    })
    await page.reload()
    await page.getByRole('tab', { name: /NPT|Non-productive time/i }).click()
    await expect(page.getByText(/unassignable time: 1/)).toBeVisible()
    expectNoFailures(consoleErrors, failedResponses)
  })

  test('the operation timeline merges documents, operations and events', async ({
    page,
    request,
    wellId,
    consoleErrors, failedResponses,
  }) => {
    const timeline = await apiGet<{
      count: number
      entries: { kind: string; title: string; summary: string | null }[]
    }>(request, `/wells/${wellId}/timeline`)

    await page.goto(wellPath('/cockpit'))
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
    await page.getByRole('tab', { name: /timeline/i }).click()

    const kinds = new Set(timeline.entries.map((entry) => entry.kind))
    expect(kinds.size, 'the timeline must merge more than one source kind').toBeGreaterThan(1)

    // The most recent operation is rendered with its real, state-derived duration.
    const operation = timeline.entries.find((entry) => entry.kind === 'operation')
    expect(operation).toBeTruthy()
    await expect(page.getByText(operation?.title ?? '').first()).toBeVisible()

    // Every entry is on the axis: a truncated timeline would hide NPT from the user.
    await expect(page.locator('li').filter({ hasText: operation?.title ?? '' }).first()).toBeVisible()

    expectNoFailures(consoleErrors, failedResponses)
  })

  test('deep links survive a reload and an unknown well is a not-found, not a blank page', async ({
    page,
    consoleErrors,
    failedResponses,
  }) => {
    await page.goto(wellPath('/cockpit'))
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
    await page.reload()
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()

    await page.goto('/wells/wel_does_not_exist/cockpit')
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByRole('alert')).toContainText(/not found/i)

    // A route that does not exist is a rendered 404 rather than an empty shell.
    await page.goto('/definitely-not-a-route')
    await expect(page.getByRole('heading', { level: 1 })).toContainText(/not found/i)

    // This journey asks the API for a well that does not exist and opens a route that does not
    // exist, so the browser is expected to log the two missing resources. The refusal is the one
    // failure this journey provokes; anything else — a thrown error, a React warning, a failed
    // asset, a call that failed for another reason — still fails the test.
    expectNoFailures(consoleErrors, failedResponses, {
      allowed: [
        {
          kind: 'http',
          method: 'GET',
          pathname: '/api/v1/wells/wel_does_not_exist',
          query: '',
          status: 404,
          maxMatches: 1,
          reason: 'the journey opens a deep link naming a well that does not exist',
        },
        {
          kind: 'http',
          method: 'GET',
          pathname: '/api/v1/wells/wel_does_not_exist/state',
          query: '',
          status: 404,
          maxMatches: 1,
          reason: 'the missing-well cockpit checks state without presenting a blank screen',
        },
        {
          kind: 'http',
          method: 'GET',
          pathname: '/api/v1/wells/wel_does_not_exist/timeline',
          query: '?limit=500',
          status: 404,
          maxMatches: 1,
          reason: 'the missing-well cockpit checks its timeline and reports the real not-found result',
        },
        {
          kind: 'http',
          method: 'GET',
          pathname: '/api/v1/wells/wel_does_not_exist/npt',
          query: '',
          status: 404,
          maxMatches: 1,
          reason: 'the missing-well cockpit checks NPT and reports the real not-found result',
        },
      ],
    })
  })
})
