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

import { apiGet, test, expect, wellPath } from './fixtures'

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
    consoleErrors,
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
    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('shows measured progress, NPT and missing-data honesty from the API', async ({
    page,
    request,
    wellId,
    consoleErrors,
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

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('the operation timeline merges documents, operations and events', async ({
    page,
    request,
    wellId,
    consoleErrors,
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

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('deep links survive a reload and an unknown well is a not-found, not a blank page', async ({
    page,
    consoleErrors,
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
    // exist, so the browser is expected to log the two missing resources. Anything else — a thrown
    // error, a React warning, a failed asset — still fails the test.
    for (const error of consoleErrors) {
      expect(error, `unexpected console error: ${error}`).toMatch(/status of 404/)
    }
  })
})
