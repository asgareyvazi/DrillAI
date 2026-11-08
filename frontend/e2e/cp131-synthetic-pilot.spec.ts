/**
 * CP131 acceptance pilot: one isolated, synthetic well exercised end to end in a real browser.
 *
 * The master-data hierarchy is seeded through the real API by `seed_cp131_pilot.py`; the survey,
 * document upload/DDR promotion, daily-report import/reconciliation, engineering calculation, scoped
 * RAG workflow run, Advisor response and report export are created through their real product surfaces.
 * Every consequential write is read back from the API/database. No application APIs are mocked and no
 * existing `.e2e` directory is used by the dedicated CP131 Playwright configuration.
 */

import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  apiGet,
  apiPost,
  expect,
  expectNoFailures,
  fixtures,
  ROLES,
  startRunFromStudio,
  test,
} from './fixtures'

const here = path.dirname(fileURLToPath(import.meta.url))
const DDR_FIXTURE = path.join(here, 'assets', 'cp131_synthetic_ddr.csv')
const DAY_MARKER = 'CP131-PILOT-DAY-MARKER'
const NPT_MARKER = 'CP131-PILOT-NPT-MARKER'

interface PilotHierarchy {
  classification: 'synthetic'
  fixture_id: 'cp131-synthetic-pilot-v1'
  project_id: string
  well_id: string
  well_name: string
  wellbore_id: string
  wellbore_name: string
  section_id: string
  section_name: string
}

interface SurveySummary {
  id: string
  wellbore_id: string
  name: string
  kind: string
  version: number
  is_current: boolean
  datum: string
  station_count: number
  calculation_state: string
  engineering_acceptance: string
  source_document_id: string | null
  revision_reason: string | null
}

interface SurveyDetail {
  trajectory: SurveySummary & {
    engine_key: string | null
    engine_version: string | null
    inputs_hash: string | null
    outputs_hash: string | null
  }
  stations: Array<{
    id: string
    station_index: number
    md_si: number
    inclination_deg: number
    azimuth_deg: number
    input_azimuth_reference: string
    input_azimuth_reference_id: string
  }>
  history: SurveySummary[]
}

interface PilotExtractedRecord {
  id: string
  record_type: string
  validation_state: string
  region_id: string | null
  page_number: number | null
  payload: Record<string, unknown>
}

interface PilotDocumentDetail {
  document: {
    id: string
    title: string
    status: string
    well_id: string | null
    wellbore_id: string | null
    section_id: string | null
    doc_type: string | null
  }
  records: PilotExtractedRecord[]
}

interface DdrProcessing {
  document_id: string
  well_id: string
  doc_type: string
  dry_run: boolean
  operations_created: string[]
  events_created: string[]
  records_promoted: string[]
  records_needing_review: string[]
  npt_hours_classified: number
  counts: { operations: number; events: number; promoted: number; needs_review: number }
}

interface OperationRow {
  id: string
  name: string
  well_id: string
  wellbore_id: string | null
  section_id: string | null
  source_document_id: string | null
  source_record_id: string | null
  actual_duration_hours: number | null
}

interface EventRow {
  id: string
  title: string
  well_id: string
  wellbore_id: string | null
  section_id: string | null
  source_document_id: string | null
  source_record_id: string | null
  is_npt: boolean
  npt_category: string | null
  npt_hours: number | null
  occurred_at: string | null
  occurred_at_basis: string
  ended_at: string | null
  ended_at_basis: string
  root_cause: string | null
  cause_basis: string
}

interface NptSummaryEnvelope {
  npt: {
    total_hours: number
    basis: string
    event_count: number
    cases: Array<{
      id: string
      source: string
      source_record_id: string | null
      document_id: string | null
      hours: number
      started_at: string | null
      started_at_basis: string
      ended_at: string | null
      ended_at_basis: string
    }>
  }
}

interface DailyReportList {
  items: Array<{
    id: string
    report_date: string
    report_number: string | null
    latest_revision: { edit_version: number; status: string } | null
  }>
  total: number
}

interface DailyReportRead {
  report: {
    id: string
    well_id: string
    wellbore_id: string
    report_date: string
    report_timezone: string
    shift_code: string
    report_number: string | null
  }
  latest_revision: {
    edit_version: number
    source_kind: string
    source_document_id: string | null
    source_record_id: string | null
    summary: string | null
    activities: Array<{
      title: string
      activity_kind: string
      operation_kind: string | null
      npt_category: string | null
      duration_source_value: number | null
      duration_source_unit: string | null
      source_document_id: string | null
      source_record_id: string | null
    }>
    measurements: Array<{
      parameter_key: string
      source_value: number
      source_unit: string
      value_si: number
      canonical_unit: string
      source_document_id: string | null
      source_record_id: string | null
    }>
    reconciliation: Array<{
      target_kind: string
      field_key: string
      source_document_id: string
      source_record_id: string
      source_region_id: string | null
      source_page_number: number | null
      source_path: string
      resolution_state: string
      proposal_matches_current: boolean
    }>
  }
}

interface EngineRunList {
  items: Array<{
    id: string
    engine_key: string
    engine_version: string
    status: string
    well_id: string | null
    wellbore_id: string | null
    section_id: string | null
    inputs: Record<string, unknown>
    outputs: Record<string, unknown>
    inputs_hash: string
    outputs_hash: string
    input_source_kind: string
  }>
}

interface WorkflowDefinition {
  id: string
  key: string
  name: string
  project_id: string | null
  current_version: number
  status: string
}

interface WorkflowRunDetail {
  run: {
    id: string
    workflow_id: string
    status: string
    project_id: string | null
    well_id: string | null
    version: number
    is_dry_run: boolean
  }
  node_runs: Array<{
    node_id: string
    node_type: string
    status: string
    inputs: Record<string, unknown>
    outputs: Record<string, unknown>
  }>
}

interface AdvisorEnvelope {
  answer: {
    well_id: string
    question: string
    facts: Array<{
      key: string
      statement: string
      value: unknown
      unit: string | null
      source: string
      source_id: string | null
      provenance: Record<string, unknown>
    }>
    narrative: { status: string; text?: string } | null
  }
}

interface ReportSection {
  key: string
  title: string
  kind: string
  items: Array<Record<string, unknown>>
}

interface Report {
  kind: string
  title: string
  well_id: string
  metadata: Record<string, unknown>
  sections: ReportSection[]
  is_renderable: boolean
}

interface ReportEnvelope {
  report: Report
}

function reportPath(wellId: string, reportId: string): string {
  return `/wells/${wellId}/daily-reports/${reportId}`
}

test('CP131 synthetic-well pilot persists Survey → DDR → Operations/NPT → Engineering → RAG/Advisor → Reports/Export', async ({
  page,
  request,
  consoleErrors,
  failedResponses,
}) => {
  const pilot = fixtures().cp131_pilot as PilotHierarchy
  expect(pilot.classification).toBe('synthetic')
  expect(pilot.fixture_id).toBe('cp131-synthetic-pilot-v1')
  expect(pilot.well_name).toBe('CP131-SYNTHETIC-PILOT-01')

  // Survey: explicit measured-depth/angle units, RKB datum, grid frame identity and synthetic reason.
  const surveyName = 'CP131 synthetic actual trajectory 01'
  await page.goto(`/wells/${pilot.well_id}/surveys`)
  await page.getByLabel('Select a wellbore').selectOption(pilot.wellbore_id)
  await page.getByRole('button', { name: 'New manual trajectory' }).click()
  await page.getByLabel('Series name').fill(surveyName)
  await page.getByLabel('Trajectory kind').selectOption('actual')
  await page.getByLabel('Vertical datum').selectOption('rkb')
  await page.getByLabel('Reason for this revision').fill(
    'Synthetic CP131 browser pilot only; not field data or engineering acceptance.',
  )
  await page.getByLabel('Measured depth 1').fill('0')
  await page.getByLabel('Inclination 1').fill('0')
  await page.getByLabel('Input azimuth 1').fill('0')
  await page.getByLabel('Azimuth reference').nth(0).selectOption('grid')
  await page.getByLabel('Exact frame identity').nth(0).fill('CP131-SYNTHETIC-GRID-01')

  await page.getByLabel('Measured depth 2').fill('500')
  await page.getByLabel('Inclination 2').fill('5')
  await page.getByLabel('Input azimuth 2').fill('2')
  await page.getByLabel('Azimuth reference').nth(1).selectOption('grid')
  await page.getByLabel('Exact frame identity').nth(1).fill('CP131-SYNTHETIC-GRID-01')
  await page.getByRole('button', { name: 'Add station' }).click()
  await page.getByLabel('Measured depth 3').fill('1000')
  await page.getByLabel('Inclination 3').fill('10')
  await page.getByLabel('Input azimuth 3').fill('5')
  await page.getByLabel('Azimuth reference').nth(2).selectOption('grid')
  await page.getByLabel('Exact frame identity').nth(2).fill('CP131-SYNTHETIC-GRID-01')
  await page.getByRole('button', { name: 'Calculate and save revision' }).click()
  await expect(page.getByText(/Trajectory revision saved/)).toBeVisible()

  const surveyList = await apiGet<{ items: SurveySummary[] }>(request, `/wells/${pilot.well_id}/surveys`)
  const survey = surveyList.items.find((row) => row.name === surveyName && row.version === 1)
  expect(survey, 'the browser-created trajectory revision must be persisted').toBeTruthy()
  if (!survey) throw new Error('persisted CP131 survey revision is missing')
  expect(survey.wellbore_id).toBe(pilot.wellbore_id)
  expect(survey.kind).toBe('actual')
  expect(survey.datum).toBe('rkb')
  expect(survey.station_count).toBe(3)
  expect(survey.calculation_state).toBe('formula_derived')
  expect(survey.engineering_acceptance).toBe('not_established')
  expect(survey.source_document_id).toBeNull()
  const surveyDetail = await apiGet<SurveyDetail>(
    request,
    `/wells/${pilot.well_id}/surveys/${survey.id}`,
  )
  expect(surveyDetail.trajectory.engine_key).toBe('trajectory.minimum_curvature')
  expect(surveyDetail.trajectory.inputs_hash).toMatch(/^[a-f0-9]{64}$/)
  expect(surveyDetail.trajectory.outputs_hash).toMatch(/^[a-f0-9]{64}$/)
  expect(surveyDetail.stations.map((row) => row.md_si)).toEqual([0, 500, 1000])
  expect(surveyDetail.stations.every((row) =>
    row.input_azimuth_reference === 'grid' &&
    row.input_azimuth_reference_id === 'CP131-SYNTHETIC-GRID-01',
  )).toBe(true)

  // Documents/DDR: the browser uploads a static, labelled synthetic CSV against explicit wellbore
  // and section scope, then invokes the real promotion endpoint from the document workspace.
  await page.goto(`/wells/${pilot.well_id}/documents`)
  await page.getByTestId('document-upload-type').selectOption('ddr')
  await page.getByTestId('document-upload-wellbore').selectOption(pilot.wellbore_id)
  await page.getByTestId('document-upload-section').selectOption(pilot.section_id)
  const uploadResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/api/v1/documents'),
  )
  await page.getByTestId('document-upload-file').setInputFiles(DDR_FIXTURE)
  const uploadResponse = await uploadResponsePromise
  expect(uploadResponse.status(), 'the real document upload must succeed').toBe(201)
  const uploaded = (await uploadResponse.json()) as { document: { id: string; title: string } }
  const documentId = uploaded.document.id
  expect(uploaded.document.title).toBe('cp131_synthetic_ddr.csv')

  await expect.poll(async () => {
    const document = await apiGet<PilotDocumentDetail>(request, `/documents/${documentId}`)
    const operationRows = document.records.filter((row) => row.record_type === 'operation_row')
    const hasDayRow = operationRows.some((row) => row.payload.summary === DAY_MARKER)
    const hasNptRow = operationRows.some((row) => row.payload.summary === NPT_MARKER)
    const header = document.records.find((row) => row.record_type === 'report_header')
    return Boolean(
      document.document.status === 'extracted' &&
      hasDayRow &&
      hasNptRow &&
      header?.payload.report_date === '2026-06-15' &&
      header?.payload.report_number === '131',
    )
  }, { timeout: 60_000, message: 'the real CSV extractor must persist the two source rows and report header' }).toBe(true)
  const documentBeforePromotion = await apiGet<PilotDocumentDetail>(request, `/documents/${documentId}`)
  expect(documentBeforePromotion.document.well_id).toBe(pilot.well_id)
  expect(documentBeforePromotion.document.wellbore_id).toBe(pilot.wellbore_id)
  expect(documentBeforePromotion.document.section_id).toBe(pilot.section_id)
  expect(documentBeforePromotion.document.doc_type).toBe('ddr')
  const dayRecord = documentBeforePromotion.records.find(
    (row) => row.record_type === 'operation_row' && row.payload.summary === DAY_MARKER,
  )
  const nptRecord = documentBeforePromotion.records.find(
    (row) => row.record_type === 'operation_row' && row.payload.summary === NPT_MARKER,
  )
  const headerRecord = documentBeforePromotion.records.find(
    (row) => row.record_type === 'report_header' && row.payload.report_number === '131',
  )
  expect(dayRecord, 'the daylight operation row must be source-backed').toBeTruthy()
  expect(nptRecord, 'the NPT row must be source-backed').toBeTruthy()
  expect(headerRecord, 'the DDR identity header must be source-backed').toBeTruthy()
  if (!dayRecord || !nptRecord || !headerRecord) throw new Error('CP131 extracted DDR source rows are missing')
  expect(dayRecord.payload.operation).toBe('Drilling ahead')
  expect(dayRecord.payload.duration_hours).toBe(4)
  expect(dayRecord.payload.depth_md_si).toBe(1000)
  expect(String(dayRecord.payload['mud weight (ppg)'])).toBe('10.8')
  expect(nptRecord.payload.operation).toBe('Stuck pipe')
  expect(nptRecord.payload.duration_hours).toBe(2)

  const processResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname.endsWith(`/api/v1/documents/${documentId}/process`),
  )
  await page.getByRole('button', { name: 'Extract engineering data' }).click()
  const processResponse = await processResponsePromise
  expect(processResponse.ok(), `DDR processing returned HTTP ${processResponse.status()}`).toBeTruthy()
  const processing = ((await processResponse.json()) as { processing: DdrProcessing }).processing
  expect(processing.document_id).toBe(documentId)
  expect(processing.well_id).toBe(pilot.well_id)
  expect(processing.doc_type).toBe('ddr')
  expect(processing.dry_run).toBe(false)
  expect(processing.npt_hours_classified).toBe(2)
  expect(processing.operations_created.length).toBeGreaterThanOrEqual(2)
  expect(processing.events_created.length).toBeGreaterThanOrEqual(1)
  await expect(page.getByText('written', { exact: true })).toBeVisible()
  await expect(page.getByText('NPT hours', { exact: true })).toBeVisible()

  // Operations/NPT: read the exact promoted rows back from the operational APIs and UI. In particular,
  // the report date is not treated as event occurrence time, no end time or cause is invented, and
  // every row retains the document/record provenance.
  const operationsBeforeDailyImport = await apiGet<{ items: OperationRow[] }>(
    request,
    `/operations?well_id=${encodeURIComponent(pilot.well_id)}&limit=200`,
  )
  const promotedOperations = operationsBeforeDailyImport.items.filter(
    (row) => row.source_document_id === documentId,
  )
  expect(promotedOperations.map((row) => row.source_record_id)).toEqual(
    expect.arrayContaining([dayRecord.id, nptRecord.id]),
  )
  expect(promotedOperations.every((row) =>
    row.wellbore_id === pilot.wellbore_id && row.section_id === pilot.section_id,
  )).toBe(true)
  const events = await apiGet<{ items: EventRow[] }>(
    request,
    `/events?well_id=${encodeURIComponent(pilot.well_id)}&limit=200`,
  )
  const stuckPipeEvent = events.items.find(
    (row) => row.source_document_id === documentId && row.source_record_id === nptRecord.id,
  )
  expect(stuckPipeEvent, 'the classified stuck-pipe event must be persisted').toBeTruthy()
  if (!stuckPipeEvent) throw new Error('CP131 stuck-pipe event is missing')
  expect(stuckPipeEvent.is_npt).toBe(true)
  expect(stuckPipeEvent.npt_category).toBe('stuck_pipe')
  expect(stuckPipeEvent.npt_hours).toBe(2)
  expect(stuckPipeEvent.wellbore_id).toBe(pilot.wellbore_id)
  expect(stuckPipeEvent.section_id).toBe(pilot.section_id)
  expect(stuckPipeEvent.occurred_at).toBeNull()
  expect(stuckPipeEvent.occurred_at_basis).toBe('unknown')
  expect(stuckPipeEvent.ended_at).toBeNull()
  expect(stuckPipeEvent.ended_at_basis).toBe('unknown')
  expect(stuckPipeEvent.root_cause).toBeNull()
  expect(stuckPipeEvent.cause_basis).toBe('unknown')
  const nptSummary = await apiGet<NptSummaryEnvelope>(request, `/wells/${pilot.well_id}/npt`)
  expect(nptSummary.npt.total_hours).toBe(2)
  expect(nptSummary.npt.cases.some((row) =>
    row.id === stuckPipeEvent.id &&
    row.source_record_id === nptRecord.id &&
    row.document_id === documentId &&
    row.started_at === null &&
    row.started_at_basis === 'unknown' &&
    row.ended_at === null &&
    row.ended_at_basis === 'unknown',
  )).toBe(true)

  await page.goto(`/wells/${pilot.well_id}/operations`)
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
  await expect(page.getByText('Drilling ahead', { exact: false })).toBeVisible()
  await page.getByRole('tab', { name: 'Events' }).click()
  await page.getByTestId('event-npt-filter').check()
  await expect(page.getByText(/Stuck pipe reported/i)).toBeVisible()
  await expect(page.getByText(/stuck pipe\s*·\s*2\s*h/i)).toBeVisible()

  // The same extracted DDR is explicitly mapped into the daily-report revision flow. Identity and
  // row proposals remain proposals until the browser records an operator disposition for each one.
  await page.goto(`/wells/${pilot.well_id}/daily-reports`)
  await page.getByTestId('daily-report-import-open').click()
  await page.getByTestId('daily-report-import-document').selectOption(documentId)
  await page.getByTestId('daily-report-import-identity-record').selectOption(headerRecord.id)
  await expect(page.getByTestId('daily-report-import-header-sources')).toContainText(pilot.well_name)
  await page.getByTestId('daily-report-import-identity-source-report-date').selectOption('payload.report_date')
  await page.getByTestId('daily-report-import-identity-source-report-number').selectOption('payload.report_number')
  await page.getByTestId('daily-report-import-record').selectOption(dayRecord.id)
  await page.getByTestId('daily-report-import-summary-source').selectOption('payload.summary')
  await page.getByTestId('daily-report-import-wellbore').selectOption(pilot.wellbore_id)
  await expect(page.getByTestId('daily-report-import-date')).toHaveValue('2026-06-15')
  await page.getByTestId('daily-report-import-timezone').fill('Europe/Oslo')
  await page.getByTestId('daily-report-import-shift').selectOption('day')
  await expect(page.getByTestId('daily-report-import-number')).toHaveValue('131')

  await page.getByTestId('daily-report-import-add-activity').click()
  await page.locator('#import-activity-0-kind').selectOption('operation')
  await page.locator('#import-activity-0-title').fill('Drilling ahead')
  await page.locator('#import-activity-0-duration').fill('4')
  await page.locator('#import-activity-0-duration-unit').selectOption('h')
  await page.locator('#import-activity-0-duration-basis').selectOption('reported')
  await page.getByTestId('daily-report-import-activity-source-0').selectOption(dayRecord.id)
  await page.getByTestId('daily-report-classification-preview-0').click()
  await expect(page.getByTestId('daily-report-classification-result-0')).toContainText(/drilling/i)
  await page.getByTestId('daily-report-apply-operation-classification-0').click()
  await expect(page.locator('#import-activity-0-operation')).toHaveValue('drilling')

  await page.getByTestId('daily-report-import-add-activity').click()
  await page.locator('#import-activity-1-kind').selectOption('npt')
  await page.locator('#import-activity-1-npt').selectOption('stuck_pipe')
  await page.locator('#import-activity-1-title').fill('Stuck pipe')
  await page.locator('#import-activity-1-duration').fill('2')
  await page.locator('#import-activity-1-duration-unit').selectOption('h')
  await page.locator('#import-activity-1-duration-basis').selectOption('reported')
  await page.getByTestId('daily-report-import-activity-source-1').selectOption(nptRecord.id)

  await page.getByTestId('daily-report-import-add-measurement').click()
  await page.getByTestId('daily-report-import-measurement-0-parameter').selectOption('mud_weight')
  await page.getByTestId('daily-report-import-measurement-0-value').fill('10.8')
  await page.getByTestId('daily-report-import-measurement-0-unit').selectOption('ppg')
  await page.getByTestId('daily-report-import-measurement-0-source').selectOption(dayRecord.id)
  await page.getByTestId('daily-report-import-submit').click()
  await expect(page.getByTestId('daily-report-import-form')).toHaveCount(0)
  await expect(page.getByTestId('daily-report-reconciliation')).toBeVisible()
  await expect(page.getByTestId('daily-report-reported-npt-duration')).toContainText('2 h')

  const dailyReports = await apiGet<DailyReportList>(
    request,
    `/wells/${pilot.well_id}/daily-reports?limit=50`,
  )
  const importedDailyReport = dailyReports.items.find((row) => row.report_number === '131')
  expect(importedDailyReport, 'the source-linked daily report must be persisted').toBeTruthy()
  if (!importedDailyReport) throw new Error('CP131 imported daily report is missing')
  const dailyRead = await apiGet<DailyReportRead>(
    request,
    reportPath(pilot.well_id, importedDailyReport.id),
  )
  expect(dailyRead.report.well_id).toBe(pilot.well_id)
  expect(dailyRead.report.wellbore_id).toBe(pilot.wellbore_id)
  expect(dailyRead.report.report_date).toBe('2026-06-15')
  expect(dailyRead.report.report_timezone).toBe('Europe/Oslo')
  expect(dailyRead.report.report_number).toBe('131')
  expect(dailyRead.latest_revision.source_kind).toBe('import')
  expect(dailyRead.latest_revision.source_document_id).toBe(documentId)
  expect(dailyRead.latest_revision.summary).toBe(DAY_MARKER)
  expect(dailyRead.latest_revision.activities).toEqual(expect.arrayContaining([
    expect.objectContaining({
      title: 'Drilling ahead',
      activity_kind: 'operation',
      operation_kind: 'drilling',
      duration_source_value: 4,
      duration_source_unit: 'h',
      source_document_id: documentId,
      source_record_id: dayRecord.id,
    }),
    expect.objectContaining({
      title: 'Stuck pipe',
      activity_kind: 'npt',
      npt_category: 'stuck_pipe',
      duration_source_value: 2,
      duration_source_unit: 'h',
      source_document_id: documentId,
      source_record_id: nptRecord.id,
    }),
  ]))
  expect(dailyRead.latest_revision.measurements).toEqual(expect.arrayContaining([
    expect.objectContaining({
      parameter_key: 'mud_weight',
      source_value: 10.8,
      source_unit: 'ppg',
      source_document_id: documentId,
      source_record_id: dayRecord.id,
    }),
  ]))
  expect(dailyRead.latest_revision.reconciliation).toHaveLength(6)
  expect(dailyRead.latest_revision.reconciliation.every((row) =>
    row.resolution_state === 'proposed' && row.proposal_matches_current,
  )).toBe(true)
  expect(dailyRead.latest_revision.reconciliation.every((row) => row.source_document_id === documentId)).toBe(true)

  const decisionSelects = page.getByTestId('daily-report-reconciliation').locator('select')
  await expect(decisionSelects).toHaveCount(6)
  for (const decision of await decisionSelects.all()) await decision.selectOption('accepted')
  await page.getByTestId('daily-report-reconciliation-reason').fill(
    'Synthetic pilot operator reviewed each mapped identity, activity and measurement proposal against its extracted source row.',
  )
  await page.getByTestId('daily-report-reconciliation-submit').click()
  await expect(page.getByText('Every source proposal has a recorded disposition.')).toBeVisible()
  const reconciled = await apiGet<DailyReportRead>(request, reportPath(pilot.well_id, importedDailyReport.id))
  expect(reconciled.latest_revision.edit_version).toBe(2)
  expect(reconciled.latest_revision.reconciliation.every((row) => row.resolution_state === 'accepted')).toBe(true)

  // Engineering: run the deterministic MSE engine in the actual engineering workspace, with explicit
  // SI-named inputs. These numbers are wholly synthetic; no rock-strength judgement or field claim is
  // requested or implied. The browser UI and engine-run table must agree on the persisted hashes.
  const mseInputs = {
    wob_si: 40000,
    torque_si: 8000,
    rpm_si: 100,
    rop_si: 18,
    bit_diameter_si: 0.31115,
    depth_si: 1000,
  }
  await page.goto(`/wells/${pilot.well_id}/engineering`)
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
  await page.getByTestId('shell-role-switch').selectOption(ROLES.engineer)
  const mseEngineButton = page.getByRole('button', { name: /drilling\.mse/i })
  await mseEngineButton.click()
  await page.getByTestId('engine-inputs').fill(JSON.stringify(mseInputs, null, 2))
  const engineResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname.endsWith('/api/v1/registry/engines/drilling.mse/run'),
  )
  await page.getByTestId('engine-run').click()
  const engineResponse = await engineResponsePromise
  expect(engineResponse.ok(), `MSE execution returned HTTP ${engineResponse.status()}`).toBeTruthy()
  const engineEnvelope = (await engineResponse.json()) as {
    engine_run_id: string
    engine: { key: string; version: string; validation_status: string }
    outputs: Record<string, unknown>
    inputs_hash: string
    outputs_hash: string
  }
  expect(engineEnvelope.engine.key).toBe('drilling.mse')
  expect(engineEnvelope.outputs_hash).toMatch(/^[a-f0-9]{64}$/)
  await expect(page.getByText(/drilling\.mse@/).first()).toBeVisible()
  const engineRuns = await apiGet<EngineRunList>(request, `/wells/${pilot.well_id}/engine-runs?limit=50`)
  const mseRun = engineRuns.items.find((row) => row.id === engineEnvelope.engine_run_id)
  expect(mseRun, 'the browser-requested MSE result must be persisted').toBeTruthy()
  if (!mseRun) throw new Error('CP131 MSE engine run is missing')
  expect(mseRun.engine_key).toBe('drilling.mse')
  expect(mseRun.status).toBe('succeeded')
  expect(mseRun.well_id).toBe(pilot.well_id)
  expect(mseRun.wellbore_id).toBe(pilot.wellbore_id)
  expect(mseRun.section_id).toBe(pilot.section_id)
  expect(mseRun.inputs).toEqual(mseInputs)
  expect(mseRun.inputs_hash).toBe(engineEnvelope.inputs_hash)
  expect(mseRun.outputs_hash).toBe(engineEnvelope.outputs_hash)
  expect(mseRun.outputs.mse_si).toBeGreaterThan(0)
  expect(mseRun.outputs.drilling_efficiency).toBeNull()

  // RAG retrieval: create and publish a real scoped workflow definition through the authenticated
  // API, then start it only through Workflow Studio in the browser. `rag.search` executes the actual
  // retriever against the uploaded DDR chunks in lexical mode; Advisor fact assembly is not used as a
  // substitute for retrieval and no LLM/provider is called.
  const workflow = await apiPost<WorkflowDefinition>(
    request,
    '/workflows',
    {
      key: 'cp131.synthetic.ddr-retrieval',
      name: 'CP131 synthetic DDR evidence retrieval',
      description: 'Synthetic pilot only: retrieve a passage scoped to its DDR document and well.',
      category: 'rag',
      domain_pack: 'drilling',
      project_id: pilot.project_id,
      tags: ['cp131', 'synthetic', 'browser-pilot'],
      graph: {
        version: 1,
        description: 'Real lexical retrieval against the CP131 synthetic DDR; no action nodes.',
        variables: {},
        settings: {},
        nodes: [
          {
            id: 'retrieve_cp131_ddr',
            type: 'rag.search',
            name: 'Retrieve the synthetic DDR marker',
            config: { query: DAY_MARKER, mode: 'lexical', limit: 5, doc_type: 'ddr' },
          },
        ],
        edges: [],
      },
    },
    'supervisor',
  )
  expect(workflow.project_id).toBe(pilot.project_id)
  expect(workflow.current_version).toBe(1)
  const publishResponse = await request.post(
    `/api/v1/workflows/${workflow.id}/publish?version=${workflow.current_version}`,
    { headers: { 'X-Dev-Roles': ROLES.supervisor } },
  )
  expect(publishResponse.ok(), `workflow publish returned HTTP ${publishResponse.status()}`).toBeTruthy()
  const published = (await publishResponse.json()) as { version: number; published_at: string | null }
  expect(published.version).toBe(workflow.current_version)
  expect(published.published_at).toBeTruthy()

  const workflowRunId = await startRunFromStudio(page, workflow.id, pilot.well_id)
  await expect(page.getByTestId('run-status')).toHaveText(/succeeded/i, { timeout: 60_000 })
  await expect(page.getByRole('tab', { name: /Node executions/ })).toHaveAttribute('aria-selected', 'true')
  const ragNodeRow = page.getByRole('row').filter({ hasText: 'retrieve_cp131_ddr' })
  await expect(ragNodeRow).toContainText(/succeeded/i)
  await ragNodeRow.getByText('View', { exact: true }).click()
  const ragRun = await apiGet<WorkflowRunDetail>(request, `/runs/${workflowRunId}`, 'supervisor')
  expect(ragRun.run.workflow_id).toBe(workflow.id)
  expect(ragRun.run.project_id).toBe(pilot.project_id)
  expect(ragRun.run.well_id).toBe(pilot.well_id)
  expect(ragRun.run.status).toBe('succeeded')
  expect(ragRun.run.is_dry_run).toBe(false)
  const ragNode = ragRun.node_runs.find((row) => row.node_id === 'retrieve_cp131_ddr')
  expect(ragNode?.node_type).toBe('rag.search')
  expect(ragNode?.status).toBe('succeeded')
  expect(ragNode?.outputs.mode_used).toBe('lexical')
  expect(ragNode?.outputs.hit_count).toBeGreaterThan(0)
  const retrievalHits = ragNode?.outputs.hits as Array<{
    document_id?: string
    doc_type?: string
    well_id?: string | null
    chunk_id?: string
  }>
  expect(retrievalHits.some((hit) =>
    hit.document_id === documentId && hit.doc_type === 'ddr' && hit.well_id === pilot.well_id && Boolean(hit.chunk_id),
  )).toBe(true)
  await expect(ragNodeRow).toContainText(documentId)

  // Advisor is separately exercised as the real recorded-facts surface, with no LLM request. Its
  // response is checked against persisted NPT, not described as a RAG result or semantic answer.
  await page.goto(`/wells/${pilot.well_id}/advisor`)
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
  await page.getByTestId('shell-role-switch').selectOption(ROLES.engineer)
  await page.getByLabel('Question').selectOption('npt')
  const advisorResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname.endsWith(`/api/v1/wells/${pilot.well_id}/advisor`),
  )
  await page.getByRole('button', { name: 'Ask', exact: true }).click()
  const advisorResponse = await advisorResponsePromise
  expect(advisorResponse.ok(), `Advisor request returned HTTP ${advisorResponse.status()}`).toBeTruthy()
  const advisor = (await advisorResponse.json()) as AdvisorEnvelope
  expect(advisor.answer.well_id).toBe(pilot.well_id)
  expect(advisor.answer.question).toBe('npt')
  const nptFact = advisor.answer.facts.find((fact) => fact.key === 'npt.total')
  expect(nptFact?.value).toBe(2)
  expect(nptFact?.unit).toBe('h')
  expect(nptFact?.provenance.selected_basis).toBeTruthy()
  expect(advisor.answer.narrative).toBeNull()
  await expect(page.getByText(/NPT 2\.0 h on the recorded events basis/i)).toBeVisible()
  await expect(page.getByText('No copy-only response was requested.')).toBeVisible()

  // Reports/export: the management summary is assembled from persisted operations, NPT, the MSE
  // engine run and document timeline, and the JSON attachment is downloaded from the real UI.
  await page.goto(`/wells/${pilot.well_id}/reports`)
  await page.getByLabel('Report kind').selectOption('management_summary')
  const reportResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'GET' &&
    new URL(response.url()).pathname.endsWith(`/api/v1/wells/${pilot.well_id}/reports/management_summary`),
  )
  await page.getByRole('button', { name: 'Build', exact: true }).click()
  const reportResponse = await reportResponsePromise
  expect(reportResponse.ok(), `management report returned HTTP ${reportResponse.status()}`).toBeTruthy()
  const builtReport = (await reportResponse.json()) as ReportEnvelope
  expect(builtReport.report.kind).toBe('management_summary')
  expect(builtReport.report.well_id).toBe(pilot.well_id)
  const timelineSection = builtReport.report.sections.find((section) => section.key === 'timeline')
  const operationTimelineItem = timelineSection?.items.find(
    (item) => item.kind === 'operation' && item.document_id === documentId,
  )
  expect(operationTimelineItem, 'the management report timeline must retain the source-linked operation').toBeTruthy()
  expect(operationTimelineItem?.section_id).toBe(pilot.section_id)
  expect(operationTimelineItem?.time_basis).toBe('unknown')
  expect(timelineSection?.items.some((item) => item.kind === 'event' && item.document_id === documentId)).toBe(true)
  const nptSection = builtReport.report.sections.find((section) => section.key === 'npt')
  expect(nptSection?.items[0]?.total_hours).toBe(2)
  const calculationsSection = builtReport.report.sections.find((section) => section.key === 'calculations')
  expect(calculationsSection?.items.some((item) =>
    item.engine_key === 'drilling.mse' && item.inputs_hash === mseRun.inputs_hash,
  )).toBe(true)
  const evidenceSection = builtReport.report.sections.find((section) => section.key === 'evidence')
  expect(evidenceSection?.items.some((item) => item.kind === 'document' && item.id === documentId)).toBe(true)
  expect(builtReport.report.sections.some((section) => section.key === 'timeline')).toBe(true)
  await expect(page.getByText('Engineering calculations', { exact: true })).toBeVisible()
  await expect(page.getByText('Evidence trail', { exact: true })).toBeVisible()

  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('report-export-json').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe(`drillai-management_summary-${pilot.well_id}.json`)
  const exportedPath = await download.path()
  expect(exportedPath).toBeTruthy()
  if (!exportedPath) throw new Error('the browser report export did not produce a downloaded file')
  const exported = JSON.parse(await readFile(exportedPath, 'utf8')) as ReportEnvelope
  expect(exported.report.kind).toBe('management_summary')
  expect(exported.report.well_id).toBe(pilot.well_id)
  expect(exported.report.sections.find((section) => section.key === 'timeline')?.items.some(
    (item) => item.kind === 'operation' && item.document_id === documentId,
  )).toBe(true)
  expect(exported.report.sections.find((section) => section.key === 'calculations')?.items.some(
    (item) => item.engine_key === 'drilling.mse' && item.inputs_hash === mseRun.inputs_hash,
  )).toBe(true)

  expectNoFailures(consoleErrors, failedResponses)
})
