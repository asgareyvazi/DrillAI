/**
 * CP150: isolated synthetic two-well browser/API/database pilot.
 *
 * The API seed creates only the two labelled well hierarchies. The browser creates one manual draft
 * per well through the product UI; API and read-only SQLite checks then verify identity, unit/value
 * provenance, revision fingerprints, and cross-well isolation. No `.e2e` demo database is used.
 */

import { createHash } from 'node:crypto'
import { apiGet, expect, expectNoFailures, selectRole, test } from './fixtures'
import { cp150Fixtures, type CP150WellFixture } from './cp150-fixtures'
import { readDailyReportDatabaseSnapshotFromDatabase } from './database-readback'

interface DailyReportRow {
  id: string
  well_id: string
  wellbore_id: string
  report_date: string
  report_timezone: string
  shift_code: string
  report_number: string | null
  latest_revision_number: number
  latest_revision: { status: string; source_kind: string } | null
}

interface DailyReportList {
  items: DailyReportRow[]
  total: number
  limit: number
  offset: number
}

interface ActivityRead {
  id: string
  activity_kind: string
  operation_kind: string | null
  title: string
  duration_seconds: number | null
  duration_source_value: number | null
  duration_source_unit: string | null
  duration_basis: string
  resolution_state: string
}

interface MeasurementRead {
  id: string
  parameter_key: string
  source_value: number
  source_unit: string
  accepted_input_value: number
  accepted_input_unit: string
  value_si: number
  canonical_unit: string
  value_basis: string
  resolution_state: string
}

interface RevisionRead {
  id: string
  revision_number: number
  edit_version: number
  status: string
  source_kind: string
  summary: string | null
  content_hash: string | null
  content_hash_verified: boolean
  activities: ActivityRead[]
  measurements: MeasurementRead[]
}

interface DailyReportDetail {
  report: DailyReportRow
  latest_revision: RevisionRead
}

interface CreateDailyReportResponse {
  report: DailyReportRow
  revision: RevisionRead
}

interface PilotDraft {
  fixture: CP150WellFixture
  report_id: string
  report_number: string
  summary: string
  activity_title: string
  activity_duration: number
  measurement_value: number
  created: CreateDailyReportResponse
}

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex')
}

async function createDraftThroughBrowser(
  page: import('@playwright/test').Page,
  fixture: CP150WellFixture,
  label: 'A' | 'B',
  runId: string,
): Promise<PilotDraft> {
  const reportNumber = `CP150-${label}-${runId}`
  const summary = `Synthetic CP150 pilot only, well ${label}, run ${runId}; not field data.`
  const activityTitle = `Synthetic CP150 UI activity ${label}`
  const activityDuration = label === 'A' ? 2 : 3
  const measurementValue = label === 'A' ? 9.2 : 10.1

  await page.goto(`/wells/${encodeURIComponent(fixture.well_id)}/daily-reports`)
  await selectRole(page, 'engineer')
  const newReport = page.getByTestId('daily-report-new')
  await expect(newReport).toBeEnabled()
  await newReport.click()
  await page.getByTestId('daily-report-wellbore').selectOption(fixture.wellbore_id)
  await page.getByTestId('daily-report-date').fill('2026-10-20')
  await page.getByTestId('daily-report-timezone').fill('UTC')
  await page.getByTestId('daily-report-shift').selectOption('day')
  await page.locator('#daily-report-number').fill(reportNumber)
  await page.locator('#daily-report-summary').fill(summary)

  await page.getByTestId('daily-report-add-activity').click()
  await page.locator('#activity-0-kind').selectOption('milestone')
  await page.locator('#activity-0-title').fill(activityTitle)
  await page.locator('#activity-0-duration').fill(String(activityDuration))
  await page.locator('#activity-0-duration-unit').selectOption('h')
  await page.locator('#activity-0-duration-basis').selectOption('reported')

  await page.getByTestId('daily-report-add-measurement').click()
  await page.getByTestId('daily-report-measurement-parameter').selectOption('mud_weight')
  await page.getByTestId('daily-report-measurement-value').fill(String(measurementValue))
  await page.getByTestId('daily-report-measurement-unit').selectOption('ppg')
  await page.locator('#measurement-0-basis').selectOption('reported')

  const createResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' &&
      url.pathname === `/api/v1/wells/${fixture.well_id}/daily-reports`
  })
  await page.getByTestId('daily-report-create-submit').click()
  const createResponse = await createResponsePromise
  expect(createResponse.status(), `browser create API response for synthetic well ${label}`).toBe(201)
  const created = (await createResponse.json()) as CreateDailyReportResponse
  expect(created.report).toMatchObject({
    well_id: fixture.well_id,
    wellbore_id: fixture.wellbore_id,
    report_date: '2026-10-20',
    report_timezone: 'UTC',
    shift_code: 'day',
    report_number: reportNumber,
    latest_revision_number: 1,
  })
  expect(created.revision).toMatchObject({
    report_id: created.report.id,
    revision_number: 1,
    status: 'draft',
    source_kind: 'manual',
    summary,
    content_hash_verified: true,
  })

  await expect(page).toHaveURL(new RegExp(`[?&]report=${created.report.id}(?:&|$)`))
  await expect(page.getByTestId('daily-report-detail')).toBeVisible()
  await expect(page.getByText(summary, { exact: true })).toBeVisible()
  await expect(page.getByText(activityTitle, { exact: true })).toBeVisible()

  return {
    fixture,
    report_id: created.report.id,
    report_number: reportNumber,
    summary,
    activity_title: activityTitle,
    activity_duration: activityDuration,
    measurement_value: measurementValue,
    created,
  }
}

test('creates and independently reads back one scoped synthetic daily-report draft per well', async ({
  page,
  request,
  consoleErrors,
  failedResponses,
}) => {
  const pilot = cp150Fixtures()
  const runId = process.env.DRILLAI_CP150_RUN_ID
  if (!runId) throw new Error('the isolated CP150 invocation must have an explicit run ID')
  const sortedWells = [...pilot.wells].sort((left, right) => left.well_name.localeCompare(right.well_name))
  const wellA = sortedWells[0]
  const wellB = sortedWells[1]
  if (!wellA || !wellB) throw new Error('the CP150 fixture must contain both synthetic well scopes')
  expect(wellA.well_name).toBe('CP150-SYNTHETIC-WELL-A')
  expect(wellB.well_name).toBe('CP150-SYNTHETIC-WELL-B')

  const wellsBefore = await apiGet<{ items: Array<{ id: string; name: string }> }>(request, '/wells?limit=500')
  expect(wellsBefore.items.map((well) => well.name).sort()).toEqual([
    'CP150-SYNTHETIC-WELL-A',
    'CP150-SYNTHETIC-WELL-B',
  ])

  await page.goto('/wells')
  await selectRole(page, 'engineer')
  await expect(page.getByText(wellA.well_name, { exact: true })).toBeVisible()
  await expect(page.getByText(wellB.well_name, { exact: true })).toBeVisible()

  const emptyA = await apiGet<DailyReportList>(
    request,
    `/wells/${encodeURIComponent(wellA.well_id)}/daily-reports?limit=50`,
  )
  const emptyB = await apiGet<DailyReportList>(
    request,
    `/wells/${encodeURIComponent(wellB.well_id)}/daily-reports?limit=50`,
  )
  expect(emptyA).toMatchObject({ items: [], total: 0 })
  expect(emptyB).toMatchObject({ items: [], total: 0 })

  const draftA = await createDraftThroughBrowser(page, wellA, 'A', runId)
  const afterA = await apiGet<DailyReportList>(
    request,
    `/wells/${encodeURIComponent(wellA.well_id)}/daily-reports?limit=50`,
  )
  const stillEmptyB = await apiGet<DailyReportList>(
    request,
    `/wells/${encodeURIComponent(wellB.well_id)}/daily-reports?limit=50`,
  )
  expect(afterA.items.map((row) => row.id)).toEqual([draftA.report_id])
  expect(afterA.total).toBe(1)
  expect(stillEmptyB).toMatchObject({ items: [], total: 0 })

  const draftB = await createDraftThroughBrowser(page, wellB, 'B', runId)
  const [listA, listB] = await Promise.all([
    apiGet<DailyReportList>(request, `/wells/${encodeURIComponent(wellA.well_id)}/daily-reports?limit=50`),
    apiGet<DailyReportList>(request, `/wells/${encodeURIComponent(wellB.well_id)}/daily-reports?limit=50`),
  ])
  expect(listA.items.map((row) => row.id)).toEqual([draftA.report_id])
  expect(listB.items.map((row) => row.id)).toEqual([draftB.report_id])
  expect(listA.items[0]).toMatchObject({ well_id: wellA.well_id, wellbore_id: wellA.wellbore_id })
  expect(listB.items[0]).toMatchObject({ well_id: wellB.well_id, wellbore_id: wellB.wellbore_id })

  const crossWellRead = await request.get(
    `/api/v1/wells/${encodeURIComponent(wellA.well_id)}/daily-reports/${encodeURIComponent(draftB.report_id)}`,
    { headers: { 'X-Dev-Roles': 'engineer' } },
  )
  expect(crossWellRead.status(), 'a report from well B must not be readable through well A scope').toBe(404)
  const crossWellBody = await crossWellRead.json() as {
    error: { code: string; details: Record<string, unknown> }
  }
  expect(crossWellBody.error).toMatchObject({ code: 'platform.not_found', details: {} })
  const crossWellText = JSON.stringify(crossWellBody)
  expect(crossWellText).not.toContain(draftB.created.revision.id)
  expect(crossWellText).not.toContain(draftB.summary)
  expect(crossWellText).not.toContain(draftB.activity_title)
  expect(crossWellText).not.toContain(draftB.report_number)

  for (const draft of [draftA, draftB]) {
    const detail = await apiGet<DailyReportDetail>(
      request,
      `/wells/${encodeURIComponent(draft.fixture.well_id)}/daily-reports/${encodeURIComponent(draft.report_id)}`,
    )
    expect(detail.report).toMatchObject({
      id: draft.report_id,
      well_id: draft.fixture.well_id,
      wellbore_id: draft.fixture.wellbore_id,
      report_date: '2026-10-20',
      report_timezone: 'UTC',
      report_number: draft.report_number,
      latest_revision_number: 1,
    })
    expect(detail.latest_revision).toMatchObject({
      id: draft.created.revision.id,
      revision_number: 1,
      status: 'draft',
      source_kind: 'manual',
      summary: draft.summary,
      content_hash: draft.created.revision.content_hash,
      content_hash_verified: true,
    })
    expect(detail.latest_revision.activities).toHaveLength(1)
    expect(detail.latest_revision.measurements).toHaveLength(1)
    const activity = detail.latest_revision.activities[0]
    const measurement = detail.latest_revision.measurements[0]
    if (!activity || !measurement) throw new Error('the API detail did not return its typed synthetic content rows')
    expect(activity).toMatchObject({
      activity_kind: 'milestone',
      title: draft.activity_title,
      duration_source_value: draft.activity_duration,
      duration_source_unit: 'h',
      duration_basis: 'reported',
      resolution_state: 'manual',
    })
    expect(measurement).toMatchObject({
      parameter_key: 'mud_weight',
      source_value: draft.measurement_value,
      source_unit: 'ppg',
      accepted_input_value: draft.measurement_value,
      accepted_input_unit: 'ppg',
      resolution_state: 'manual',
    })

    const database = readDailyReportDatabaseSnapshotFromDatabase(
      pilot.database_url,
      draft.report_id,
      draft.fixture.well_id,
      draft.fixture.wellbore_id,
      1,
    )
    expect(database.report).toMatchObject({
      id: draft.report_id,
      well_id: draft.fixture.well_id,
      wellbore_id: draft.fixture.wellbore_id,
      report_date: '2026-10-20',
      report_number: draft.report_number,
      latest_revision_number: 1,
      latest_approved_revision_number: null,
    })
    expect(database.revision).toMatchObject({
      id: draft.created.revision.id,
      revision_number: 1,
      status: 'draft',
      source_kind: 'manual',
      summary_sha256: sha256(draft.summary),
      content_hash: detail.latest_revision.content_hash,
    })
    expect(database.activity).toMatchObject({
      title_sha256: sha256(draft.activity_title),
      operation_kind: null,
      duration_seconds: draft.activity_duration * 60 * 60,
      duration_source_value: draft.activity_duration,
      duration_source_unit: 'h',
      resolution_state: 'manual',
    })
    expect(database.measurement).toMatchObject({
      parameter_key: 'mud_weight',
      source_value: draft.measurement_value,
      source_unit: 'ppg',
      accepted_input_value: draft.measurement_value,
      accepted_input_unit: 'ppg',
      value_si: measurement.value_si,
      canonical_unit: measurement.canonical_unit,
      resolution_state: 'manual',
    })
  }

  expectNoFailures(consoleErrors, failedResponses)
})
