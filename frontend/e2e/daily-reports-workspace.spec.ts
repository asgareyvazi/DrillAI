/**
 * Journey: a real operator creates, revises, submits, reviews, corrects and withdraws daily-report
 * revisions in the browser. Reads and independent authorization refusals are checked against the API.
 */

import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import {
  apiAttempt,
  apiGet,
  apiPost,
  expect,
  expectNoFailures,
  test,
} from './fixtures'
import { readDailyReportDatabaseSnapshot } from './database-readback'

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex')
}

interface DailyReportRevisionSummary {
  id: string
  revision_number: number
  edit_version: number
  status: 'draft' | 'submitted' | 'approved' | 'rejected' | 'withdrawn'
  created_by: string
  submitted_by: string | null
  decided_by: string | null
  decision_reason: string | null
  supersedes_revision_id: string | null
  content_hash_verified?: boolean
}

interface DailyReportRecord {
  id: string
  well_id: string
  wellbore_id: string
  report_date: string
  report_timezone: string
  shift_code: string
  report_number: string | null
  latest_revision_number: number
  latest_revision: DailyReportRevisionSummary | null
}

interface DailyReportRead {
  report: DailyReportRecord
  latest_revision: DailyReportRevisionSummary & {
    source_kind: 'manual' | 'import' | 'connector'
    source_document_id: string | null
    source_record_id: string | null
    summary: string | null
    reconciliation: Array<{
      target_kind: 'revision_field' | 'report_identity' | 'activity' | 'measurement'
      field_key: string
      source_document_id: string
      source_record_id: string
      source_region_id: string | null
      source_page_number: number | null
      source_path: string
      resolution_state: string
      proposal_matches_current: boolean
    }>
    activities: Array<{
      title: string
      operation_kind: string | null
      source_document_id: string | null
      source_record_id: string | null
      source_region_id: string | null
      source_page_number: number | null
      start_at: string | null
      start_time_basis: string
      duration_seconds: number | null
      duration_source_value: number | null
      duration_source_unit: string | null
      depth_from_source_value: number | null
      depth_to_source_value: number | null
      depth_reference: string
      depth_basis: string
    }>
    measurements: Array<{
      parameter_key: string
      source_document_id: string | null
      source_record_id: string | null
      source_region_id: string | null
      source_page_number: number | null
      source_value: number
      source_unit: string
      accepted_input_value: number
      accepted_input_unit: string
      value_si: number
      canonical_unit: string
      value_basis: string
    }>
  }
}

interface ImportedDocumentDetail {
  document: { id: string; title: string; status: string; well_id: string | null; wellbore_id: string | null }
  records: Array<{
    id: string
    record_type: string
    validation_state: string
    page_number: number | null
    region_id: string | null
    payload: Record<string, unknown>
  }>
  ingestion_jobs: Array<{ status: string }>
}

interface DailyReportHistory {
  items: DailyReportRevisionSummary[]
  total: number
}

function reportPath(wellId: string, reportId: string): string {
  return `/wells/${wellId}/daily-reports/${reportId}`
}

async function enterReason(page: import('./fixtures').Page, reason: string) {
  await page.getByTestId('daily-report-action-reason').fill(reason)
  await page.getByTestId('daily-report-action-confirm').click()
}

test.describe('daily-report lifecycle in a real browser', () => {
  test('imports and reconciles rows against an actual extracted CSV source', async ({
    page,
    request,
    consoleErrors,
    failedResponses,
  }) => {
    const tag = Date.now().toString(36)
    const summary = `Synthetic source-backed summary ${tag}.`
    const project = await apiPost<{ id: string }>(
      request,
      '/projects',
      { name: `E2E DDR import project ${tag}` },
      'wellManager',
    )
    const well = await apiPost<{ id: string }>(
      request,
      '/wells',
      { project_id: project.id, name: `E2E-DDR-IMPORT-${tag}`, well_type: 'development_producer' },
      'engineer',
    )
    const wellbore = await apiPost<{ id: string }>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'Main bore', purpose: 'original' },
      'engineer',
    )
    const reportDate = '2026-11-02'
    const reportNumber = '42'
    const sourceWellName = `E2E-DDR-IMPORT-${tag}`
    const csv = [
      `Report No: ${reportNumber}`,
      `Date: ${reportDate}`,
      `Well: ${sourceWellName}`,
      'summary,operation,duration (h),depth md (m),mud weight (ppg)',
      `${summary},Drilling ahead,8,2450,12.5`,
      '',
    ].join('\n')
    await page.goto(`/wells/${well.id}/documents`)
    await page.getByTestId('shell-role-switch').selectOption('engineer')
    await expect(page.getByTestId('shell-role-switch')).toHaveValue('engineer')
    await page.getByTestId('document-upload-type').selectOption('ddr')
    await expect(page.getByTestId('document-upload-wellbore')).toContainText('Main bore')
    await page.getByTestId('document-upload-wellbore').selectOption(wellbore.id)
    const uploadResponsePromise = page.waitForResponse((response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/documents',
    )
    await page.getByTestId('document-upload-file').setInputFiles({
      name: `synthetic-ddr-import-${tag}.csv`,
      mimeType: 'text/csv',
      buffer: Buffer.from(csv, 'utf8'),
    })
    const uploadResponse = await uploadResponsePromise
    expect(uploadResponse.status(), 'the browser document upload must succeed').toBe(201)
    const uploaded = (await uploadResponse.json()) as { document: { id: string } }
    await expect(page.getByText(/Uploaded /)).toBeVisible({ timeout: 30_000 })
    await expect.poll(async () => {
      const detail = await apiGet<ImportedDocumentDetail>(request, `/documents/${uploaded.document.id}`, 'engineer')
      return detail.records.some((record) =>
        record.record_type === 'operation_row' &&
        record.validation_state === 'extracted' &&
        record.payload.summary === summary &&
        record.payload.operation === 'Drilling ahead' &&
        record.payload.duration_hours === 8 &&
        record.payload.depth_md_si === 2450,
      ) && detail.records.some((record) =>
        record.record_type === 'report_header' &&
        record.validation_state === 'extracted' &&
        record.payload.well_name_text === sourceWellName &&
        record.payload.report_date === reportDate &&
        record.payload.report_number === reportNumber,
      )
    }, { timeout: 60_000, message: 'the CSV extractor must preserve row values and source-backed report identity' }).toBe(true)
    const document = await apiGet<ImportedDocumentDetail>(request, `/documents/${uploaded.document.id}`, 'engineer')
    expect(document.document).toMatchObject({
      id: uploaded.document.id,
      well_id: well.id,
      wellbore_id: wellbore.id,
      status: 'extracted',
    })
    const sourceRecord = document.records.find((record) =>
      record.record_type === 'operation_row' && record.payload.summary === summary,
    )
    const headerRecord = document.records.find((record) =>
      record.record_type === 'report_header' &&
      record.validation_state === 'extracted' &&
      record.payload.well_name_text === sourceWellName &&
      record.payload.report_date === reportDate &&
      record.payload.report_number === reportNumber,
    )
    expect(sourceRecord, 'the uploaded CSV must produce the row used by the import').toBeTruthy()
    expect(headerRecord, 'the same document must produce exact report-header identity evidence').toBeTruthy()
    if (!sourceRecord) throw new Error('extracted operation source record is missing')
    if (!headerRecord) throw new Error('extracted report-header identity source record is missing')

    await page.goto(`/wells/${well.id}/daily-reports`)
    await expect(page.getByTestId('daily-report-import-open')).toBeEnabled()
    await page.getByTestId('daily-report-import-open').click()
    await page.getByTestId('daily-report-import-document').selectOption(uploaded.document.id)
    await page.getByTestId('daily-report-import-identity-record').selectOption(headerRecord.id)
    await expect(page.getByTestId('daily-report-import-header-sources')).toContainText(sourceWellName)
    await expect(page.getByTestId('daily-report-import-wellbore')).toHaveValue('')
    await page.getByTestId('daily-report-import-identity-source-report-date').selectOption('payload.report_date')
    await page.getByTestId('daily-report-import-identity-source-report-number').selectOption('payload.report_number')
    await page.getByTestId('daily-report-import-record').selectOption(sourceRecord.id)
    await page.getByTestId('daily-report-import-summary-source').selectOption('payload.summary')
    await page.getByTestId('daily-report-import-wellbore').selectOption(wellbore.id)
    await expect(page.getByTestId('daily-report-import-date')).toHaveValue(reportDate)
    await page.getByTestId('daily-report-import-timezone').fill('Europe/Amsterdam')
    await page.getByTestId('daily-report-import-shift').selectOption('day')
    await expect(page.getByTestId('daily-report-import-number')).toHaveValue(reportNumber)

    await page.getByTestId('daily-report-import-add-activity').click()
    await page.locator('#import-activity-0-kind').selectOption('operation')
    await page.locator('#import-activity-0-title').fill(String(sourceRecord.payload.operation))
    await page.locator('#import-activity-0-duration').fill(String(sourceRecord.payload.duration_hours))
    await page.locator('#import-activity-0-duration-unit').selectOption('h')
    await page.locator('#import-activity-0-duration-basis').selectOption('reported')
    await page.locator('#import-activity-0-source').selectOption(sourceRecord.id)
    await page.getByTestId('daily-report-classification-preview-0').click()
    await expect(page.getByTestId('daily-report-classification-result-0')).toContainText(/drilling/i)
    await page.getByTestId('daily-report-apply-operation-classification-0').click()
    await expect(page.locator('#import-activity-0-operation')).toHaveValue('drilling')

    await page.getByTestId('daily-report-import-add-activity').click()
    await page.locator('#import-activity-1-kind').selectOption('npt')
    await page.locator('#import-activity-1-npt').selectOption('stuck_pipe')
    await page.locator('#import-activity-1-title').fill('Reported stuck pipe response')
    await page.locator('#import-activity-1-duration').fill('2')
    await page.locator('#import-activity-1-duration-unit').selectOption('h')
    await page.locator('#import-activity-1-duration-basis').selectOption('reported')

    await page.getByTestId('daily-report-import-add-measurement').click()
    await page.getByTestId('daily-report-import-measurement-0-parameter').selectOption('mud_weight')
    await page.getByTestId('daily-report-import-measurement-0-value').fill('12.5')
    await page.getByTestId('daily-report-import-measurement-0-unit').selectOption('ppg')
    await page.getByTestId('daily-report-import-measurement-0-source').selectOption(sourceRecord.id)

    await page.getByTestId('daily-report-import-submit').click()
    await expect(page.getByTestId('daily-report-import-form')).toHaveCount(0)
    await expect(page.getByTestId('daily-report-reconciliation')).toBeVisible()
    await expect(page.getByTestId('daily-report-reported-npt-duration')).toContainText('2 h')
    await page.getByTestId('daily-report-wellbore-filter').selectOption(wellbore.id)
    await page.getByTestId('daily-report-report-number-filter').fill(reportNumber)
    await expect(page.getByTestId('daily-report-list').getByText(reportNumber, { exact: true })).toBeVisible()

    const listed = await apiGet<{ items: DailyReportRecord[]; total: number }>(
      request,
      `/wells/${well.id}/daily-reports?limit=20`,
      'engineer',
    )
    const importedRow = listed.items.find((item) => item.report_number === reportNumber)
    expect(importedRow, 'the imported report must be persisted by the backend').toBeTruthy()
    if (!importedRow) throw new Error('imported daily report is missing from the API')
    const importedRead = await apiGet<DailyReportRead>(request, reportPath(well.id, importedRow.id), 'engineer')
    expect(importedRead.latest_revision.source_kind).toBe('import')
    expect(importedRead.latest_revision.source_document_id).toBe(uploaded.document.id)
    expect(importedRead.report.report_date).toBe(reportDate)
    expect(importedRead.report.report_number).toBe(reportNumber)
    expect(importedRead.latest_revision.summary).toBe(summary)
    expect(importedRead.latest_revision.activities[0]).toEqual(expect.objectContaining({
      title: 'Drilling ahead',
      operation_kind: 'drilling',
      duration_source_value: 8,
      duration_source_unit: 'h',
      source_document_id: uploaded.document.id,
      source_record_id: sourceRecord.id,
      source_region_id: sourceRecord.region_id,
      source_page_number: sourceRecord.page_number,
    }))
    expect(importedRead.latest_revision.measurements[0]).toEqual(expect.objectContaining({
      parameter_key: 'mud_weight',
      source_value: 12.5,
      source_unit: 'ppg',
      source_document_id: uploaded.document.id,
      source_record_id: sourceRecord.id,
      source_region_id: sourceRecord.region_id,
      source_page_number: sourceRecord.page_number,
    }))
    const proposals = importedRead.latest_revision.reconciliation
    expect(proposals).toHaveLength(5)
    expect(proposals.every((item) => item.resolution_state === 'proposed')).toBe(true)
    expect(proposals.every((item) => item.proposal_matches_current)).toBe(true)
    const identityProposals = proposals.filter((item) => item.target_kind === 'report_identity')
    expect(identityProposals.map((item) => item.field_key).sort()).toEqual(['report_date', 'report_number'])
    expect(identityProposals.every((item) =>
      item.source_document_id === uploaded.document.id && item.source_record_id === headerRecord.id &&
      item.source_region_id === headerRecord.region_id && item.source_page_number === headerRecord.page_number &&
      ['payload.report_date', 'payload.report_number'].includes(item.source_path),
    )).toBe(true)
    const contentProposals = proposals.filter((item) => item.target_kind !== 'report_identity')
    expect(contentProposals.every((item) =>
      item.source_document_id === uploaded.document.id && item.source_record_id === sourceRecord.id &&
      item.source_region_id === sourceRecord.region_id && item.source_page_number === sourceRecord.page_number,
    )).toBe(true)

    const decisionSelects = page.getByTestId('daily-report-reconciliation').locator('select')
    await expect(decisionSelects).toHaveCount(5)
    for (const decision of await decisionSelects.all()) await decision.selectOption('accepted')
    await page.getByTestId('daily-report-reconciliation-reason').fill('Compared every proposed value with the extracted CSV row.')
    await page.getByTestId('daily-report-reconciliation-submit').click()
    await expect(page.getByText('Every source proposal has a recorded disposition.')).toBeVisible()
    await expect(page.getByTestId('daily-report-submit')).toBeEnabled()

    const reconciled = await apiGet<DailyReportRead>(request, reportPath(well.id, importedRow.id), 'engineer')
    expect(reconciled.latest_revision.edit_version).toBe(2)
    expect(reconciled.latest_revision.reconciliation.every((item) => item.resolution_state === 'accepted')).toBe(true)

    const submittedReadbackPromise = page.waitForResponse((response) => {
      const url = new URL(response.url())
      return response.request().method() === 'GET' &&
        url.pathname === `/api/v1${reportPath(well.id, importedRow.id)}` &&
        response.ok()
    })
    await page.getByTestId('daily-report-submit').click()
    await enterReason(page, 'Submit the reconciled imported report for independent review.')
    const submittedResponse = await submittedReadbackPromise
    const submitted = (await submittedResponse.json()) as DailyReportRead
    expect(submitted.report.id).toBe(importedRow.id)
    expect(submitted.latest_revision.status).toBe('submitted')
    expect(submitted.latest_revision.submitted_by).toBeTruthy()
    await expect(page.getByTestId('daily-report-detail').getByText('Submitted', { exact: true })).toBeVisible()

    await page.getByTestId('shell-role-switch').selectOption('drilling_supervisor')
    await expect(page.getByTestId('shell-role-switch')).toHaveValue('drilling_supervisor')
    await expect(page.getByTestId('daily-report-approve')).toBeEnabled()
    const approvedReadbackPromise = page.waitForResponse((response) => {
      const url = new URL(response.url())
      return response.request().method() === 'GET' &&
        url.pathname === `/api/v1${reportPath(well.id, importedRow.id)}` &&
        response.ok()
    })
    await page.getByTestId('daily-report-approve').click()
    await enterReason(page, 'Independent reviewer matched the accepted proposal to the uploaded source.')
    const approvedResponse = await approvedReadbackPromise
    const approved = (await approvedResponse.json()) as DailyReportRead
    expect(approved.report.id).toBe(importedRow.id)
    expect(approved.latest_revision.status).toBe('approved')
    expect(approved.latest_revision.decided_by).not.toBe(approved.latest_revision.submitted_by)
    await expect(page.getByTestId('daily-report-detail').getByText('Approved', { exact: true })).toBeVisible()

    // Verify the same imported revision, source links, accepted decisions, and typed values in the
    // actual isolated SQLite database, using a separate read-only connection (no application API mock).
    const database = readDailyReportDatabaseSnapshot(importedRow.id, well.id, wellbore.id)
    expect(database.report).toMatchObject({
      id: importedRow.id,
      well_id: well.id,
      wellbore_id: wellbore.id,
      report_date: reportDate,
      report_number: reportNumber,
      latest_revision_number: 1,
      latest_approved_revision_number: 1,
    })
    expect(database.revision).toMatchObject({
      revision_number: 1,
      status: 'approved',
      source_kind: 'import',
      source_document_id: uploaded.document.id,
      source_record_id: sourceRecord.id,
      summary_sha256: sha256(summary),
    })
    expect(database.revision.content_hash).toMatch(/^[a-f0-9]{64}$/)
    expect(database.activity).toMatchObject({
      title_sha256: sha256('Drilling ahead'),
      operation_kind: 'drilling',
      duration_source_value: 8,
      duration_source_unit: 'h',
      source_document_id: uploaded.document.id,
      source_record_id: sourceRecord.id,
      resolution_state: 'accepted',
    })
    expect(database.measurement).toMatchObject({
      parameter_key: 'mud_weight',
      source_value: 12.5,
      source_unit: 'ppg',
      accepted_input_value: 12.5,
      accepted_input_unit: 'ppg',
      source_document_id: uploaded.document.id,
      source_record_id: sourceRecord.id,
      resolution_state: 'accepted',
    })
    expect(database.reconciliation).toEqual({ total: 5, accepted: 5, source_documents: 1, source_records: 2 })
    expectNoFailures(consoleErrors, failedResponses)
  })

  test('orders same-date reports by explicit periods and never bridges incomplete reports', async ({
    page,
    request,
    consoleErrors,
    failedResponses,
  }) => {
    const tag = Date.now().toString(36)
    const project = await apiPost<{ id: string }>(
      request,
      '/projects',
      { name: `E2E DDR period project ${tag}` },
      'wellManager',
    )
    const well = await apiPost<{ id: string }>(
      request,
      '/wells',
      { project_id: project.id, name: `E2E-DDR-PERIOD-${tag}`, well_type: 'development_producer' },
      'engineer',
    )
    const wellbore = await apiPost<{ id: string }>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'Main bore', purpose: 'original' },
      'engineer',
    )

    const periods: Array<{
      date: string
      number: string
      start: string
      end: string | null
      approve: boolean
      shift?: 'day' | 'night'
    }> = [
      { date: '2026-03-01', number: 'DDR-PERIOD-01', start: '2026-03-01T06:00:00Z', end: '2026-03-02T06:00:00Z', approve: true },
      { date: '2026-03-02', number: 'DDR-PERIOD-02', start: '2026-03-02T06:00:00Z', end: null, approve: true },
      { date: '2026-03-03', number: 'DDR-PERIOD-03-A', start: '2026-03-03T06:00:00Z', end: '2026-03-03T18:00:00Z', approve: true, shift: 'day' },
      { date: '2026-03-03', number: 'DDR-PERIOD-03-B', start: '2026-03-03T18:00:00Z', end: '2026-03-04T06:00:00Z', approve: true, shift: 'night' },
      { date: '2026-03-04', number: 'DDR-PERIOD-04', start: '2026-03-04T07:00:00Z', end: '2026-03-05T06:00:00Z', approve: true },
      { date: '2026-03-05', number: 'DDR-PERIOD-05', start: '2026-03-05T06:00:00Z', end: '2026-03-06T06:00:00Z', approve: false },
    ]
    for (const period of periods) {
      const content: Record<string, string> = { summary: 'Synthetic E2E period-boundary test record.' }
      if (period.start) content.period_start_at = period.start
      if (period.end) content.period_end_at = period.end
      const created = await apiPost<{ report: { id: string }; revision: { edit_version: number } }>(
        request,
        `/wells/${well.id}/daily-reports`,
        {
          wellbore_id: wellbore.id,
          report_date: period.date,
          report_timezone: 'UTC',
          shift_code: period.shift ?? 'day',
          report_number: `${period.number}-${tag}`,
          content,
        },
        'engineer',
      )
      if (!period.approve) continue
      const reportPathForPeriod = reportPath(well.id, created.report.id)
      const submitted = await apiPost<{ revision: { edit_version: number } }>(
        request,
        `${reportPathForPeriod}/revisions/1/submit`,
        { expected_edit_version: created.revision.edit_version, reason: 'Synthetic E2E submission for period-review test.' },
        'engineer',
      )
      await apiPost(
        request,
        `${reportPathForPeriod}/revisions/1/approve`,
        { expected_edit_version: submitted.revision.edit_version, reason: 'Synthetic E2E independent period-test approval.' },
        'supervisor',
      )
    }

    await page.goto(`/wells/${well.id}/daily-reports`)
    const periodPanel = page.getByTestId('daily-report-period-integrity')
    await expect(periodPanel).toContainText('Reported-period boundary review')
    await expect(periodPanel).toContainText('Reported period gap')
    await expect(periodPanel).toContainText('1 h')
    await expect(periodPanel).toContainText('Reported boundaries meet')
    const comparisons = periodPanel.getByTestId('daily-report-period-comparisons').locator('li')
    await expect(comparisons).toHaveCount(2)
    await expect(comparisons.nth(0)).toContainText(`DDR-PERIOD-03-A-${tag}`)
    await expect(comparisons.nth(0)).toContainText(`DDR-PERIOD-03-B-${tag}`)
    await expect(comparisons.nth(1)).toContainText(`DDR-PERIOD-03-B-${tag}`)
    await expect(comparisons.nth(1)).toContainText(`DDR-PERIOD-04-${tag}`)
    await expect(comparisons.nth(0)).toContainText('Distinct explicit period-start instants within one local report date')
    await expect(comparisons.nth(1)).toContainText('Consecutive local report dates with increasing explicit period-start instants')
    await expect(comparisons.nth(1)).toContainText('2026-03-04T06:00:00Z → 2026-03-04T07:00:00Z')
    await expect(periodPanel).toContainText('Adjacent identity pairs not compared')
    await periodPanel.getByText('Report revisions and period-assessment states').click()
    await expect(periodPanel).toContainText('Period end missing')
    await expect(periodPanel).toContainText('No approved revision')
    await expect(periodPanel).toContainText('none are skipped to bridge intervals')
    expectNoFailures(consoleErrors, failedResponses)
  })

  test('preserves explicit provenance, reviewer separation, history, corrections and withdrawal', async ({
    page,
    request,
    consoleErrors,
    failedResponses,
  }) => {
    const tag = Date.now().toString(36)
    const project = await apiPost<{ id: string }>(
      request,
      '/projects',
      { name: `E2E daily-report project ${tag}` },
      'wellManager',
    )
    const well = await apiPost<{ id: string }>(
      request,
      '/wells',
      { project_id: project.id, name: `E2E-DDR-${tag}`, well_type: 'development_producer' },
      'engineer',
    )
    const wellbore = await apiPost<{ id: string }>(
      request,
      `/wells/${well.id}/wellbores`,
      { name: 'Main bore', purpose: 'original' },
      'engineer',
    )

    await page.goto(`/wells/${well.id}/daily-reports`)
    await expect(page.getByRole('heading', { level: 1, name: 'Daily Drilling Reports' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Daily Drilling Reports', exact: true })).toHaveAttribute('href', `/wells/${well.id}/daily-reports`)
    await expect(page.getByTestId('daily-report-new')).toBeEnabled()
    await page.getByTestId('daily-report-new').click()

    await page.getByTestId('daily-report-wellbore').selectOption(wellbore.id)
    await page.getByTestId('daily-report-date').fill('2026-03-15')
    await page.getByTestId('daily-report-timezone').fill('Europe/Amsterdam')
    await page.getByTestId('daily-report-shift').selectOption('day')
    await page.getByLabel('Operator report number').fill(`DDR-E2E-${tag}`)
    await page.getByTestId('daily-report-period-start').fill('2026-03-15T06:00:00+01:00')
    await page.getByTestId('daily-report-period-end').fill('2026-03-16T06:00:00+01:00')
    await page.getByLabel('Summary', { exact: true }).fill('Daily drilling report entered from the operator shift record.')
    await page.getByLabel('Operations summary').fill('Drilled ahead; no event time was inferred from the reporting period.')

    await page.getByTestId('daily-report-add-activity').click()
    await page.locator('#activity-0-kind').selectOption('operation')
    await page.locator('#activity-0-operation').selectOption('drilling')
    await page.locator('#activity-0-title').fill('Drilling ahead')
    await page.locator('#activity-0-start').fill('2026-03-15T07:00:00+01:00')
    await page.locator('#activity-0-start-basis').selectOption('reported')
    await page.locator('#activity-0-duration').fill('8.5')
    await page.locator('#activity-0-duration-unit').selectOption('h')
    await page.locator('#activity-0-duration-basis').selectOption('reported')
    await page.locator('#activity-0-depth-from').fill('2300')
    await page.locator('#activity-0-depth-from-unit').selectOption('m')
    await page.locator('#activity-0-depth-to').fill('2450')
    await page.locator('#activity-0-depth-to-unit').selectOption('m')
    await page.locator('#activity-0-depth-reference').selectOption('md')
    await page.locator('#activity-0-depth-basis').selectOption('reported')
    await page.locator('#activity-0-cause-basis').selectOption('recorded')

    await page.getByTestId('daily-report-add-measurement').click()
    await page.getByTestId('daily-report-measurement-parameter').selectOption('mud_weight')
    await page.getByTestId('daily-report-measurement-value').fill('9.2')
    await page.getByTestId('daily-report-measurement-unit').selectOption('ppg')

    await page.getByTestId('daily-report-create-submit').click()
    await expect(page.getByTestId('daily-report-editor')).toHaveCount(0)
    await expect(page.getByTestId('daily-report-list').getByText(`DDR-E2E-${tag}`, { exact: true })).toBeVisible()

    const listed = await apiGet<{ items: DailyReportRecord[]; total: number }>(
      request,
      `/wells/${well.id}/daily-reports?limit=20`,
      'engineer',
    )
    expect(listed.total).toBe(1)
    const row = listed.items.find((item) => item.report_number === `DDR-E2E-${tag}`)
    expect(row, 'the manual report must be present in the API list').toBeTruthy()
    if (!row) throw new Error('created daily report missing from list')
    expect(row.latest_revision?.status).toBe('draft')

    const reportId = row.id
    const initialRead = await apiGet<DailyReportRead>(request, reportPath(well.id, reportId), 'engineer')
    expect(initialRead.latest_revision.summary).toBe('Daily drilling report entered from the operator shift record.')
    expect(initialRead.latest_revision.content_hash_verified).toBe(true)
    expect(initialRead.latest_revision.activities[0]).toEqual(expect.objectContaining({
      title: 'Drilling ahead',
      start_time_basis: 'reported',
      duration_seconds: 8.5 * 3600,
      duration_source_value: 8.5,
      duration_source_unit: 'h',
      depth_from_source_value: 2300,
      depth_to_source_value: 2450,
      depth_reference: 'md',
      depth_basis: 'reported',
    }))
    expect(initialRead.latest_revision.measurements[0]).toEqual(expect.objectContaining({
      parameter_key: 'mud_weight',
      source_value: 9.2,
      source_unit: 'ppg',
      accepted_input_value: 9.2,
      accepted_input_unit: 'ppg',
      canonical_unit: 'kg/m3',
      value_basis: 'reported',
    }))

    // Editing is a new edit version of the draft; the entered reason is part of the governed change.
    await page.getByTestId('daily-report-edit').click()
    const revisedSummary = 'Shift summary revised after checking the operator entry.'
    await page.locator('#daily-report-summary').fill(revisedSummary)
    await page.getByTestId('daily-report-edit-reason').fill('Corrected the summary against the signed shift sheet.')
    await page.getByTestId('daily-report-save-submit').click()
    await expect(page.getByTestId('daily-report-editor')).toHaveCount(0)
    await expect(page.getByText(revisedSummary, { exact: true })).toBeVisible()

    const historyAfterEdit = await apiGet<DailyReportHistory>(
      request,
      `${reportPath(well.id, reportId)}/history`,
      'viewer',
    )
    expect(historyAfterEdit.total).toBe(1)
    expect(historyAfterEdit.items[0]?.edit_version).toBe(2)

    // The submitting operator cannot review their own revision; the API refuses it independently.
    await page.getByTestId('daily-report-submit').click()
    await enterReason(page, 'The report is ready for independent review.')
    await expect(page.getByTestId('daily-report-approve')).toBeDisabled()
    await expect(page.getByText('The submitter cannot approve or reject this revision. Choose a different reviewer.')).toBeVisible()

    const submitted = await apiGet<DailyReportRead>(request, reportPath(well.id, reportId), 'viewer')
    expect(submitted.latest_revision.status).toBe('submitted')
    expect(submitted.latest_revision.submitted_by).toBeTruthy()
    const selfApproval = await apiAttempt(request, 'POST', `${reportPath(well.id, reportId)}/revisions/1/approve`, {
      body: { expected_edit_version: submitted.latest_revision.edit_version, reason: 'This should be denied.' },
      role: 'engineer',
    })
    expect(selfApproval.status).toBe(403)
    expect(selfApproval.code).toBe('security.permission_denied')

    await page.getByTestId('shell-role-switch').selectOption('drilling_supervisor')
    await expect(page.getByTestId('shell-role-switch')).toHaveValue('drilling_supervisor')
    await expect(page.getByTestId('daily-report-approve')).toBeEnabled()
    await page.getByTestId('daily-report-approve').click()
    await enterReason(page, 'Independent review verified the content and provenance.')
    await expect(page.getByTestId('daily-report-detail')).toContainText('Approved')

    const approved = await apiGet<DailyReportRead>(request, reportPath(well.id, reportId), 'supervisor')
    expect(approved.latest_revision.status).toBe('approved')
    expect(approved.latest_revision.decided_by).not.toBe(approved.latest_revision.submitted_by)

    // Corrections are new numbered drafts linked to the terminal revision, never in-place edits.
    await page.getByTestId('daily-report-correction').click()
    await enterReason(page, 'Open a correction after independent review.')
    await expect(page.getByTestId('daily-report-detail')).toContainText('Draft')
    const correctedDraft = await apiGet<DailyReportRead>(request, reportPath(well.id, reportId), 'supervisor')
    expect(correctedDraft.report.latest_revision_number).toBe(2)
    expect(correctedDraft.latest_revision.revision_number).toBe(2)
    expect(correctedDraft.latest_revision.status).toBe('draft')
    expect(correctedDraft.latest_revision.supersedes_revision_id).toBe(approved.latest_revision.id)

    // A separate identity submits the correction; the supervisor rejects it with an audited reason.
    await page.getByTestId('shell-role-switch').selectOption('engineer')
    await expect(page.getByTestId('shell-role-switch')).toHaveValue('engineer')
    await page.getByTestId('daily-report-submit').click()
    await enterReason(page, 'Submit correction for independent review.')
    await expect(page.getByTestId('daily-report-detail')).toContainText('Submitted')

    await page.getByTestId('shell-role-switch').selectOption('drilling_supervisor')
    await expect(page.getByTestId('daily-report-reject')).toBeEnabled()
    await page.getByTestId('daily-report-reject').click()
    await enterReason(page, 'Reject until the shift record is reconciled.')
    await expect(page.getByTestId('daily-report-detail')).toContainText('Rejected')

    // A second correction is created from the rejected revision and may only be withdrawn by its creator.
    await page.getByTestId('daily-report-correction').click()
    await enterReason(page, 'The report owner will withdraw this superseded correction.')
    await expect(page.getByTestId('daily-report-withdraw')).toBeEnabled()
    await page.getByTestId('daily-report-withdraw').click()
    await enterReason(page, 'Withdraw this correction; it remains part of the audit history.')
    await expect(page.getByTestId('daily-report-detail')).toContainText('Withdrawn')

    const finalRead = await apiGet<DailyReportRead>(request, reportPath(well.id, reportId), 'viewer')
    expect(finalRead.report.latest_revision_number).toBe(3)
    expect(finalRead.latest_revision.status).toBe('withdrawn')
    expect(finalRead.latest_revision.decision_reason).toBe('Withdraw this correction; it remains part of the audit history.')
    const finalHistory = await apiGet<DailyReportHistory>(
      request,
      `${reportPath(well.id, reportId)}/history`,
      'viewer',
    )
    expect(finalHistory.total).toBe(3)
    expect(finalHistory.items.map((item) => item.status).sort()).toEqual(['approved', 'rejected', 'withdrawn'].sort())

    // Historical revisions remain readable and have no current-revision mutation controls.
    await page.getByTestId('daily-report-revision-1').click()
    await expect(page.getByTestId('daily-report-detail')).toContainText('Approved')
    await expect(page.getByTestId('daily-report-submit')).toHaveCount(0)
    await expect(page.getByTestId('daily-report-approve')).toHaveCount(0)
    await expect(page.getByTestId('daily-report-correction')).toHaveCount(0)

    // The same report remains usable in Persian, with RTL chrome and technical identifiers kept LTR.
    await page.getByTestId('locale-switch').selectOption('fa')
    await expect(page.locator('html')).toHaveAttribute('lang', 'fa')
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await expect(page.getByRole('heading', { level: 1, name: 'گزارش‌های روزانه حفاری' })).toBeVisible()
    await expect(page.getByTestId('daily-report-detail')).toContainText('تأییدشده')
    await expect(page.locator('bdi[dir="ltr"]').first()).toContainText(well.id)

    // Export the selected historical revision (not the current draft) from the Persian UI. The
    // download is a real binary response, and the API independently refuses a viewer without the
    // dedicated export entitlement.
    const revisionExportPath = `${reportPath(well.id, reportId)}/revisions/1/exports/pdf`
    const pdfResponsePromise = page.waitForResponse((response) =>
      response.url().includes(revisionExportPath) && response.url().includes('locale=fa'),
    )
    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('daily-report-export-pdf').click()
    const [pdfResponse, download] = await Promise.all([pdfResponsePromise, downloadPromise])
    expect(pdfResponse.status()).toBe(200)
    expect(pdfResponse.headers()['content-type']).toContain('application/pdf')
    const downloadedPath = await download.path()
    expect(downloadedPath).toBeTruthy()
    if (!downloadedPath) throw new Error('PDF export download has no saved artifact path')
    expect((await readFile(downloadedPath)).subarray(0, 5).toString('ascii')).toBe('%PDF-')
    expect(download.suggestedFilename()).toBe('daily-report-revision-1.pdf')
    await expect(page.getByText('خروجی نسخه دریافت شد.')).toBeVisible()

    const deniedExport = await request.get(`/api/v1${revisionExportPath}?locale=fa`, {
      headers: { 'X-Dev-Roles': 'viewer' },
    })
    expect(deniedExport.status()).toBe(403)
    expect((await deniedExport.json()).error.code).toBe('security.permission_denied')

    expectNoFailures(consoleErrors, failedResponses)
  })
})
