/**
 * CP150: isolated synthetic two-well browser/API/database pilot.
 *
 * The API seed creates only the two labelled well hierarchies. The browser creates one manual draft
 * per well through the product UI; API and read-only SQLite checks then verify identity, unit/value
 * provenance, revision fingerprints, and cross-well isolation. No `.e2e` demo database is used.
 */

import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { type Page } from '@playwright/test'
import { apiGet, expect, expectNoFailures, selectRole, test } from './fixtures'
import { cp150Fixtures, type CP150WellFixture } from './cp150-fixtures'
import {
  readDailyReportDatabaseSnapshotFromDatabase,
  readEngineeringProductDatabaseSnapshotFromDatabase,
} from './database-readback'

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


interface EngineeringProductVersionRead {
  id: string
  artifact_id: string
  version: number
  state: string
  schema_key: 'drilling_program' | 'controlled_procedure'
  schema_version: number
  payload: Record<string, unknown>
  content_hash: string
  authored_by: string | null
  reviewed_by: string | null
  approved_by: string | null
  approval_request_id: string | null
  supersedes_version_id: string | null
  validation_summary: Record<string, unknown>
}

interface EngineeringProductArtifactRead {
  id: string
  org_id: string
  kind: 'program' | 'procedure'
  title: string
  code: string | null
  status: string
  current_version: number
  project_id: string
  well_id: string
  wellbore_id: string
}

interface EngineeringProductApprovalRead {
  id: string
  status: string
  requested_by: string | null
  decided_by: string | null
  request_payload: Record<string, unknown>
}

interface EngineeringProductMutationRead {
  artifact: EngineeringProductArtifactRead
  version: EngineeringProductVersionRead
  validation?: {
    state: 'blocked' | 'warnings' | 'passed'
    field_acceptance: 'not_established'
    linked_program_state: string | null
    findings: Array<{ code: string; severity: string }>
  }
  approval_request?: EngineeringProductApprovalRead
}

interface EngineeringProductListRead {
  items: Array<{ artifact: EngineeringProductArtifactRead; current_version: EngineeringProductVersionRead | null }>
  total: number
}

function waitForApiResponse(page: Page, method: string, pathname: string) {
  return page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === method && url.pathname === pathname
  })
}

async function createProgramThroughBrowser(
  page: Page,
  fixture: CP150WellFixture,
  label: 'A' | 'B',
  runId: string,
): Promise<EngineeringProductMutationRead> {
  const title = `Synthetic CP150 drilling program ${label} ${runId}`
  const code = `CP150-PROG-${label}-${runId}`
  await page.goto(`/wells/${encodeURIComponent(fixture.well_id)}/engineering-products`)
  await selectRole(page, 'engineer')
  await expect(page.getByTestId('engineering-products-workspace')).toBeVisible()
  await page.getByRole('button', { name: 'Create product', exact: true }).click()
  await page.getByTestId('product-title').fill(title)
  await page.getByTestId('product-code').fill(code)
  await page.getByTestId('product-wellbore').selectOption(fixture.wellbore_id)
  await page.getByTestId('product-purpose').fill('Synthetic CP150-only program; not a field design or operational instruction.')
  await page.getByRole('button', { name: 'Add objective', exact: true }).click()
  await page.getByLabel('Objective statement').fill(`Synthetic CP150 objective for well ${label}; not field data.`)
  await page.getByRole('button', { name: 'Add section', exact: true }).click()
  const section = page.getByTestId('program-section')
  await section.getByLabel('Program section name').fill(`Synthetic CP150 planned section ${label}`)
  const quantities = section.getByTestId('quantity-editor')
  const values = [
    { value: '0.2159', unit: 'm' },
    { value: '0', unit: 'm' },
    { value: '100', unit: 'm' },
  ]
  for (const [index, item] of values.entries()) {
    const editor = quantities.nth(index)
    await editor.getByLabel('Value state').selectOption('known')
    await editor.getByLabel('Entered value').fill(item.value)
    await editor.getByLabel('Registered unit').selectOption(item.unit)
    await expect(editor.getByLabel('Value provenance')).toHaveValue('operator_entered')
  }

  const createResponsePromise = waitForApiResponse(
    page,
    'POST',
    `/api/v1/wells/${fixture.well_id}/engineering-products`,
  )
  await page.getByTestId('product-save').click()
  const response = await createResponsePromise
  expect(response.status(), `synthetic program create response for well ${label}`).toBe(201)
  const created = (await response.json()) as EngineeringProductMutationRead
  expect(created.artifact).toMatchObject({
    kind: 'program',
    title,
    code,
    project_id: cp150Fixtures().project_id,
    well_id: fixture.well_id,
    wellbore_id: fixture.wellbore_id,
    status: 'draft',
  })
  expect(created.version).toMatchObject({
    version: 1,
    state: 'draft',
    schema_key: 'drilling_program',
    payload: {
      purpose: 'Synthetic CP150-only program; not a field design or operational instruction.',
      objectives: [{ sequence: 1 }],
      sections: [{
        sequence: 1,
        name: `Synthetic CP150 planned section ${label}`,
        hole_diameter: { state: 'known', value: 0.2159, unit: 'm', source_kind: 'operator_entered' },
        planned_md_from: { state: 'known', value: 0, unit: 'm', source_kind: 'operator_entered' },
        planned_md_to: { state: 'known', value: 100, unit: 'm', source_kind: 'operator_entered' },
      }],
    },
  })
  expect(created.version.content_hash).toMatch(/^[a-f0-9]{64}$/)
  expect(created.version.validation_summary.linked_program).toBeNull()
  return created
}

async function selectProductFromRegister(page: Page, kind: 'program' | 'procedure', title: string): Promise<void> {
  await page.getByRole('button', {
    name: kind === 'program' ? 'Drilling program' : 'Controlled procedure',
    exact: true,
  }).click()
  await page.getByTestId('engineering-product-list').getByRole('button').filter({ hasText: title }).click()
}

async function setCompositeDevelopmentIdentity(page: Page, roles: string): Promise<void> {
  await page.evaluate((roleValue) => {
    const key = 'drillai.session'
    const stored = localStorage.getItem(key)
    if (!stored) throw new Error('the persisted development identity is missing')
    const envelope = JSON.parse(stored) as { state?: Record<string, unknown>; version?: number }
    if (!envelope.state) throw new Error('the persisted development identity has no state')
    localStorage.setItem(key, JSON.stringify({
      ...envelope,
      state: { ...envelope.state, devRoles: roleValue },
    }))
  }, roles)
  await page.reload()
  await expect(page.getByTestId('shell-role-switch')).toHaveValue(roles)
}

async function approveSelectedRevision(
  page: Page,
  kind: 'program' | 'procedure',
  title: string,
  artifactId: string,
  versionId: string,
  withdrawFirst = false,
): Promise<EngineeringProductMutationRead> {
  const validationPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/validate`
  const validationResponsePromise = waitForApiResponse(page, 'POST', validationPath)
  await page.getByRole('button', { name: 'Validate this exact draft', exact: true }).click()
  const validationResponse = await validationResponsePromise
  expect(validationResponse.status()).toBe(200)
  const validated = (await validationResponse.json()) as EngineeringProductMutationRead
  expect(validated.version.id).toBe(versionId)
  expect(validated.validation?.state).toMatch(/^(passed|warnings)$/)
  expect(validated.validation?.field_acceptance).toBe('not_established')

  await selectRole(page, 'supervisor')
  const submitPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/submit`
  const submitResponsePromise = waitForApiResponse(page, 'POST', submitPath)
  await page.getByRole('button', { name: 'Submit for independent review', exact: true }).click()
  const submittedResponse = await submitResponsePromise
  expect(submittedResponse.status()).toBe(202)
  let submitted = (await submittedResponse.json()) as EngineeringProductMutationRead
  expect(submitted.version.state).toBe('in_review')
  if (withdrawFirst) {
    const oldApprovalId = submitted.approval_request?.id
    expect(oldApprovalId).toBeTruthy()
    await page.getByLabel('Withdrawal reason').fill('Synthetic review restart; no operational authorization.')
    const withdrawalPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/withdraw`
    const withdrawalPromise = waitForApiResponse(page, 'POST', withdrawalPath)
    await page.getByRole('button', { name: 'Withdraw review and return to draft', exact: true }).click()
    const withdrawal = await withdrawalPromise
    expect(withdrawal.status()).toBe(200)
    const withdrawn = await withdrawal.json() as EngineeringProductMutationRead
    expect(withdrawn.version.state).toBe('draft')
    expect(withdrawn.approval_request).toMatchObject({ id: oldApprovalId, status: 'cancelled' })
    await expect(page.getByRole('button', { name: 'Submit for independent review', exact: true })).toBeDisabled()
    await expect(page.getByText('Synthetic review restart; no operational authorization.')).toBeVisible()
    await selectRole(page, 'engineer')
    const revalidationPromise = waitForApiResponse(page, 'POST', validationPath)
    await page.getByRole('button', { name: 'Validate this exact draft', exact: true }).click()
    expect((await revalidationPromise).status()).toBe(200)
    await selectRole(page, 'supervisor')
    const resubmitPromise = waitForApiResponse(page, 'POST', submitPath)
    await page.getByRole('button', { name: 'Submit for independent review', exact: true }).click()
    const resubmitted = await resubmitPromise
    expect(resubmitted.status()).toBe(202)
    submitted = await resubmitted.json() as EngineeringProductMutationRead
    expect(submitted.approval_request?.id).not.toBe(oldApprovalId)
  }
  const approvalId = submitted.approval_request?.id
  if (!approvalId) throw new Error('the synthetic submission did not persist an approval request')

  await selectRole(page, 'wellManager')
  await page.getByLabel('Independent review notes').fill(`Synthetic ${kind} ${title} review; no field acceptance.`)
  const reviewPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/review`
  const reviewResponsePromise = waitForApiResponse(page, 'POST', reviewPath)
  await page.getByRole('button', { name: 'Record independent review', exact: true }).click()
  const reviewedResponse = await reviewResponsePromise
  expect(reviewedResponse.status()).toBe(200)
  const reviewed = (await reviewedResponse.json()) as EngineeringProductMutationRead
  expect(reviewed.version.reviewed_by).toBeTruthy()
  expect(reviewed.version.reviewed_by).not.toBe(submitted.approval_request?.requested_by)

  await setCompositeDevelopmentIdentity(page, 'well_manager,drilling_supervisor')
  const detailPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}`
  const detailResponsePromise = waitForApiResponse(page, 'GET', detailPath)
  await selectProductFromRegister(page, kind, title)
  const detailResponse = await detailResponsePromise
  expect(detailResponse.status()).toBe(200)
  const exactDetail = await detailResponse.json() as EngineeringProductMutationRead
  expect(exactDetail.version).toMatchObject({ id: versionId, state: 'in_review', reviewed_by: reviewed.version.reviewed_by })
  expect(exactDetail.approval_request).toMatchObject({ id: approvalId, status: 'pending' })
  await page.getByLabel('Decision note (required)').fill(`Synthetic ${kind} ${title} approved only for CP150 software testing.`)
  const decisionPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/decision`
  const decisionResponsePromise = waitForApiResponse(page, 'POST', decisionPath)
  await page.getByRole('button', { name: 'Approve this exact revision', exact: true }).click()
  const decisionResponse = await decisionResponsePromise
  expect(decisionResponse.status()).toBe(200)
  const decided = (await decisionResponse.json()) as EngineeringProductMutationRead
  expect(decided.version).toMatchObject({ id: versionId, state: 'approved' })
  expect(decided.artifact.status).toBe('approved')
  expect(decided.version.reviewed_by).not.toBe(decided.version.approved_by)
  expect(decided.version.authored_by).not.toBe(decided.version.approved_by)
  expect(decided.approval_request).toMatchObject({ id: approvalId, status: 'approved' })
  return decided
}

async function createProcedureThroughBrowser(
  page: Page,
  fixture: CP150WellFixture,
  label: 'A' | 'B',
  runId: string,
  programVersionId: string,
): Promise<EngineeringProductMutationRead> {
  const title = `Synthetic CP150 controlled procedure ${label} ${runId}`
  const code = `CP150-PROC-${label}-${runId}`
  await page.goto(`/wells/${encodeURIComponent(fixture.well_id)}/engineering-products`)
  await selectRole(page, 'engineer')
  await page.getByRole('button', { name: 'Controlled procedure', exact: true }).click()
  await page.getByRole('button', { name: 'Create product', exact: true }).click()
  await page.getByTestId('product-title').fill(title)
  await page.getByTestId('product-code').fill(code)
  await page.getByTestId('product-wellbore').selectOption(fixture.wellbore_id)
  await page.getByTestId('product-purpose').fill('Synthetic CP150-only controlled-document test; not a field procedure.')
  await page.getByLabel('Exact approved drilling-program revision (optional)').selectOption(programVersionId)
  await page.getByLabel('Scope statement').fill(`Synthetic browser/API/DB pilot scope for well ${label}; no operational scope.`)
  await page.getByRole('button', { name: 'Add prerequisite', exact: true }).click()
  await page.getByLabel('Requirement').fill('Synthetic test prerequisite: confirm the fixture is isolated.')
  await page.getByLabel('How it is verified').fill('Read the invocation-owned CP150 fixture metadata; this is not field verification.')
  await page.getByRole('button', { name: 'Add step', exact: true }).click()
  const step = page.getByTestId('procedure-step').first()
  await step.getByLabel('Step title').fill(`Synthetic review step ${label}`)
  await step.getByLabel('Instruction').fill('Review the synthetic record only; do not treat it as an operating instruction.')
  await step.getByLabel('Responsible role').fill('CP150 synthetic test reviewer')
  await step.getByLabel('Preconditions').fill('The isolated fixture is marked synthetic.')
  await step.getByLabel('Hazards').fill('Misinterpretation as field authorization.')
  await step.getByLabel('Control measures').fill('Display the synthetic-only notice and require independent human review.')
  await step.getByLabel('Verification criteria').fill('The recorded state remains explicitly synthetic and scoped to this well.')
  await step.getByLabel('Response if criteria are not met').fill('Stop the software test and retain its failure evidence.')
  await page.getByRole('button', { name: 'Add stop-work condition', exact: true }).click()
  await page.getByLabel('Stop-work trigger').fill('Any test record is mistaken for field evidence or authorization.')
  await page.getByLabel('Immediate response').fill('Stop the test; preserve the observed state; do not execute field actions.')
  await page.getByLabel('Human escalation role').fill('Independent CP150 test reviewer')

  const createPath = `/api/v1/wells/${fixture.well_id}/engineering-products`
  const createResponsePromise = waitForApiResponse(page, 'POST', createPath)
  await page.getByTestId('product-save').click()
  const response = await createResponsePromise
  expect(response.status(), `synthetic procedure create response for well ${label}`).toBe(201)
  const created = (await response.json()) as EngineeringProductMutationRead
  expect(created.artifact).toMatchObject({
    kind: 'procedure',
    title,
    code,
    project_id: cp150Fixtures().project_id,
    well_id: fixture.well_id,
    wellbore_id: fixture.wellbore_id,
    status: 'draft',
  })
  const linkedProgram = created.version.validation_summary.linked_program as Record<string, unknown> | null
  expect(linkedProgram).toMatchObject({ version_id: programVersionId, state_at_link: 'approved' })
  expect(created.version.schema_key).toBe('controlled_procedure')
  expect(created.version.content_hash).toMatch(/^[a-f0-9]{64}$/)

  const validatePath = `/api/v1/engineering-products/${encodeURIComponent(created.artifact.id)}/versions/${encodeURIComponent(created.version.id)}/validate`
  const validationResponsePromise = waitForApiResponse(page, 'POST', validatePath)
  await page.getByRole('button', { name: 'Validate this exact draft', exact: true }).click()
  const validationResponse = await validationResponsePromise
  expect(validationResponse.status()).toBe(200)
  const validated = (await validationResponse.json()) as EngineeringProductMutationRead
  expect(validated.validation?.state).toMatch(/^(passed|warnings)$/)
  expect(validated.validation?.linked_program_state).toBe('approved')
  expect(validated.validation?.field_acceptance).toBe('not_established')
  return created
}

async function exportProcedureRevision(page: Page, product: EngineeringProductMutationRead): Promise<void> {
  const artifactId = product.artifact.id
  const versionId = product.version.id
  const jsonPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/exports/json`
  const jsonResponsePromise = waitForApiResponse(page, 'GET', jsonPath)
  const jsonDownloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export JSON', exact: true }).click()
  const [jsonResponse, jsonDownload] = await Promise.all([jsonResponsePromise, jsonDownloadPromise])
  expect(jsonResponse.status()).toBe(200)
  expect(jsonDownload.suggestedFilename()).toBe(`procedure-revision-${product.version.version}.json`)
  const jsonPathOnDisk = await jsonDownload.path()
  if (!jsonPathOnDisk) throw new Error('the JSON export download has no retained file path')
  const exported = JSON.parse(await readFile(jsonPathOnDisk, 'utf8')) as {
    revision: Record<string, unknown>
    export_limits: Record<string, unknown>
  }
  expect(exported.revision).toMatchObject({
    id: versionId,
    content_hash: product.version.content_hash,
    field_acceptance: 'not_established',
    linked_program_snapshot: { version_id: (product.version.validation_summary.linked_program as Record<string, unknown>).version_id },
  })
  expect(exported.export_limits).toMatchObject({
    engineering_calculation: 'not_performed_by_export',
    approval: 'not_granted_by_export',
    operational_control: 'not_available',
  })

  const markdownPath = `/api/v1/engineering-products/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(versionId)}/exports/markdown`
  const markdownResponsePromise = waitForApiResponse(page, 'GET', markdownPath)
  const markdownDownloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export Markdown', exact: true }).click()
  const [markdownResponse, markdownDownload] = await Promise.all([markdownResponsePromise, markdownDownloadPromise])
  expect(markdownResponse.status()).toBe(200)
  expect(markdownDownload.suggestedFilename()).toBe(`procedure-revision-${product.version.version}.md`)
  const markdownPathOnDisk = await markdownDownload.path()
  if (!markdownPathOnDisk) throw new Error('the Markdown export download has no retained file path')
  const markdown = await readFile(markdownPathOnDisk, 'utf8')
  expect(markdown).toContain(`Revision: \`${product.version.version}\``)
  expect(markdown).toContain('Field acceptance: `not_established`')
  expect(markdown).toContain('does not perform calculations')
}

test('creates scoped synthetic daily reports, versioned programs and controlled procedures across two wells', async ({
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

  // Exercise the real advisor route from the engineering workspace with narration disabled. The
  // well-identity claim must resolve to a stored same-tenant citation; unknowns remain explicit.
  await page.goto(`/wells/${encodeURIComponent(wellA.well_id)}/engineering-products`)
  await selectRole(page, 'engineer')
  const advisorPath = `/api/v1/wells/${encodeURIComponent(wellA.well_id)}/advisor`
  await expect(page.getByLabel('Request the configured copy-only narrative (optional)')).not.toBeChecked()
  const advisorResponsePromise = waitForApiResponse(page, 'POST', advisorPath)
  await page.getByRole('button', { name: 'Read grounded well context', exact: true }).click()
  const advisorResponse = await advisorResponsePromise
  expect(advisorResponse.status()).toBe(200)
  const advisorResult = await advisorResponse.json() as {
    answer: {
      question: string
      well_id: string
      facts: Array<{
        key: string
        statement: string
        citations: Array<{ kind: string; source_id: string; scope: { well_id: string } }>
        grounding: { status: string }
      }>
      unknown: unknown[]
      narrative: unknown
      grounding: { scope: { project_id: string; well_id: string }; uncited_claim_count: number }
    }
  }
  expect(advisorResult.answer.question).toBe('engineering_checks')
  expect(advisorResult.answer.well_id).toBe(wellA.well_id)
  const identityFact = advisorResult.answer.facts.find((fact) => fact.key === 'well.identity')
  expect(identityFact?.statement).toContain(wellA.well_name)
  expect(identityFact?.grounding.status).toBe('cited')
  expect(identityFact?.citations).toContainEqual(expect.objectContaining({
    kind: 'database_record',
    source_id: wellA.well_id,
    scope: expect.objectContaining({ well_id: wellA.well_id }),
  }))
  expect(advisorResult.answer.grounding).toMatchObject({
    scope: { project_id: pilot.project_id, well_id: wellA.well_id },
    uncited_claim_count: 0,
  })
  expect(advisorResult.answer.narrative).toBeNull()
  expect(advisorResult.answer.unknown.length).toBeGreaterThan(0)
  const groundedContext = page.getByTestId('grounded-engineering-context')
  await expect(groundedContext).toContainText(wellA.well_name)
  await expect(groundedContext).toContainText('Recorded facts')
  await expect(groundedContext).toContainText('Unknowns')
  const identityRow = groundedContext.locator('li').filter({ hasText: wellA.well_name })
  await identityRow.getByText(/Citations/).click()
  await expect(identityRow).toContainText('database_record')

  await page.getByTestId('locale-switch').selectOption('fa')
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
  await expect(page.getByRole('button', { name: 'خواندن بافت مستند چاه', exact: true })).toBeVisible()
  await page.getByTestId('locale-switch').selectOption('en')
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr')

  const programA = await createProgramThroughBrowser(page, wellA, 'A', runId)
  const productsAfterProgramA = await apiGet<EngineeringProductListRead>(
    request,
    `/wells/${encodeURIComponent(wellA.well_id)}/engineering-products`,
  )
  const productsStillEmptyB = await apiGet<EngineeringProductListRead>(
    request,
    `/wells/${encodeURIComponent(wellB.well_id)}/engineering-products`,
  )
  expect(productsAfterProgramA.total).toBe(1)
  expect(productsStillEmptyB).toMatchObject({ items: [], total: 0 })
  const approvedProgramA = await approveSelectedRevision(
    page,
    'program',
    programA.artifact.title,
    programA.artifact.id,
    programA.version.id,
    true,
  )
  const procedureA = await createProcedureThroughBrowser(
    page,
    wellA,
    'A',
    runId,
    approvedProgramA.version.id,
  )
  const approvedProcedureA = await approveSelectedRevision(
    page,
    'procedure',
    procedureA.artifact.title,
    procedureA.artifact.id,
    procedureA.version.id,
  )
  await selectRole(page, 'engineer')
  await selectProductFromRegister(page, 'procedure', procedureA.artifact.title)
  await exportProcedureRevision(page, approvedProcedureA)

  const programB = await createProgramThroughBrowser(page, wellB, 'B', runId)
  const approvedProgramB = await approveSelectedRevision(
    page,
    'program',
    programB.artifact.title,
    programB.artifact.id,
    programB.version.id,
  )
  const procedureB = await createProcedureThroughBrowser(
    page,
    wellB,
    'B',
    runId,
    approvedProgramB.version.id,
  )
  const approvedProcedureB = await approveSelectedRevision(
    page,
    'procedure',
    procedureB.artifact.title,
    procedureB.artifact.id,
    procedureB.version.id,
  )

  const rejectedCrossWellLink = await request.post(
    `/api/v1/wells/${encodeURIComponent(wellA.well_id)}/engineering-products`,
    {
      headers: { 'X-Dev-Roles': 'engineer' },
      data: {
        kind: 'procedure',
        title: `Rejected synthetic cross-well link ${runId}`,
        project_id: pilot.project_id,
        wellbore_id: wellA.wellbore_id,
        payload: procedureA.version.payload,
        linked_program_version_id: approvedProgramB.version.id,
      },
    },
  )
  expect(rejectedCrossWellLink.status(), 'a procedure in well A must not link a well B program revision').toBe(422)
  const rejectedLinkBody = await rejectedCrossWellLink.json() as { error: { code: string; message: string } }
  expect(rejectedLinkBody.error.code).toBe('platform.validation_failed')
  expect(rejectedLinkBody.error.message).toContain('different well')
  expect(rejectedLinkBody.error.message).not.toContain(wellB.well_name)

  await selectRole(page, 'engineer')
  await page.goto(`/wells/${encodeURIComponent(wellA.well_id)}/engineering-products`)
  await selectRole(page, 'engineer')
  await selectProductFromRegister(page, 'program', programA.artifact.title)
  await page.getByRole('button', { name: 'Create revision', exact: true }).click()
  await page.getByTestId('product-change-reason').fill('Synthetic CP150 revision to test immutable procedure linkage; not a field change.')
  await page.getByTestId('product-purpose').fill('Synthetic CP150 program revision 2; explicitly test data only.')
  const programRevisionPath = `/api/v1/engineering-products/${encodeURIComponent(programA.artifact.id)}/versions`
  const programRevisionResponsePromise = waitForApiResponse(page, 'POST', programRevisionPath)
  await page.getByTestId('product-save').click()
  const programRevisionResponse = await programRevisionResponsePromise
  expect(programRevisionResponse.status()).toBe(201)
  const programARevision2 = (await programRevisionResponse.json()) as EngineeringProductMutationRead
  expect(programARevision2.version).toMatchObject({
    version: 2,
    state: 'draft',
    supersedes_version_id: programA.version.id,
  })
  expect(programARevision2.version.content_hash).not.toBe(programA.version.content_hash)
  const approvedProgramARevision2 = await approveSelectedRevision(
    page,
    'program',
    programA.artifact.title,
    programA.artifact.id,
    programARevision2.version.id,
  )
  const historicalProgramARevision1 = await apiGet<EngineeringProductMutationRead>(
    request,
    `/engineering-products/${encodeURIComponent(programA.artifact.id)}/versions/${encodeURIComponent(programA.version.id)}`,
  )
  expect(historicalProgramARevision1.version).toMatchObject({
    id: programA.version.id,
    state: 'superseded',
    approval_request_id: approvedProgramA.version.approval_request_id,
    approved_by: approvedProgramA.version.approved_by,
    reviewed_by: approvedProgramA.version.reviewed_by,
  })

  const pinnedProcedureARevision1BeforeReplacement = await apiGet<EngineeringProductMutationRead & {
    linked_program: Record<string, unknown>
    linked_program_current_state: string
  }>(
    request,
    `/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions/${encodeURIComponent(procedureA.version.id)}`,
  )
  expect(pinnedProcedureARevision1BeforeReplacement.version).toMatchObject({
    id: procedureA.version.id,
    state: 'approved',
    content_hash: procedureA.version.content_hash,
  })
  expect(pinnedProcedureARevision1BeforeReplacement.linked_program).toMatchObject({
    version_id: programA.version.id,
    content_hash: programA.version.content_hash,
  })
  expect(pinnedProcedureARevision1BeforeReplacement.linked_program_current_state).toBe('superseded')
  expect(pinnedProcedureARevision1BeforeReplacement.linked_program).toEqual(
    procedureA.version.validation_summary.linked_program,
  )

  const staleLinkedRevisionResponse = await request.post(
    `/api/v1/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions`,
    {
      headers: { 'X-Dev-Roles': 'engineer' },
      data: {
        expected_current_version: 1,
        change_reason: `Synthetic CP150: reject a new revision that still points at superseded program ${runId}.`,
        payload: procedureA.version.payload,
        linked_program_version_id: programA.version.id,
      },
    },
  )
  expect(staleLinkedRevisionResponse.status(), 'a new procedure revision must explicitly select a currently approved program revision').toBe(409)
  const staleLinkedRevisionBody = await staleLinkedRevisionResponse.json() as { error: { code: string; message: string } }
  expect(staleLinkedRevisionBody.error.code).toBe('platform.conflict')
  expect(staleLinkedRevisionBody.error.message).toContain('only to an approved drilling-program revision')
  const procedureHistoryAfterRejectedRevision = await apiGet<{ items: EngineeringProductVersionRead[]; total: number }>(
    request,
    `/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions`,
  )
  expect(procedureHistoryAfterRejectedRevision).toMatchObject({ total: 1, items: [{ id: procedureA.version.id, state: 'approved' }] })

  await selectRole(page, 'engineer')
  await page.goto(`/wells/${encodeURIComponent(wellA.well_id)}/engineering-products`)
  await selectRole(page, 'engineer')
  await page.getByRole('button', { name: 'Controlled procedure', exact: true }).click()
  await selectProductFromRegister(page, 'procedure', procedureA.artifact.title)
  await page.getByRole('button', { name: 'Create revision', exact: true }).click()
  const linkedProgramSelect = page.getByLabel('Exact approved drilling-program revision (optional)')
  await expect(linkedProgramSelect).toHaveValue(programA.version.id)
  await linkedProgramSelect.selectOption(approvedProgramARevision2.version.id)
  await page.getByTestId('product-change-reason').fill('Synthetic CP150 procedure revision explicitly adopts approved program revision 2; the prior revision remains pinned to revision 1.')
  await page.getByTestId('product-purpose').fill('Synthetic CP150 procedure revision 2; explicitly linked to the current approved synthetic program.')
  const procedureRevisionPath = `/api/v1/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions`
  const procedureRevisionResponsePromise = waitForApiResponse(page, 'POST', procedureRevisionPath)
  await page.getByTestId('product-save').click()
  const procedureRevisionResponse = await procedureRevisionResponsePromise
  expect(procedureRevisionResponse.status()).toBe(201)
  const procedureARevision2 = (await procedureRevisionResponse.json()) as EngineeringProductMutationRead
  expect(procedureARevision2.version).toMatchObject({
    version: 2,
    state: 'draft',
    supersedes_version_id: procedureA.version.id,
  })
  expect(procedureARevision2.version.validation_summary.linked_program).toMatchObject({
    version_id: approvedProgramARevision2.version.id,
    content_hash: approvedProgramARevision2.version.content_hash,
    state_at_link: 'approved',
  })
  const approvedProcedureARevision2 = await approveSelectedRevision(
    page,
    'procedure',
    procedureA.artifact.title,
    procedureA.artifact.id,
    procedureARevision2.version.id,
  )
  expect(approvedProcedureARevision2.version.validation_summary.linked_program).toMatchObject({
    version_id: approvedProgramARevision2.version.id,
    content_hash: approvedProgramARevision2.version.content_hash,
  })
  const historicalProcedureARevision1 = await apiGet<EngineeringProductMutationRead & {
    linked_program: Record<string, unknown>
    linked_program_current_state: string
  }>(
    request,
    `/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions/${encodeURIComponent(procedureA.version.id)}`,
  )
  expect(historicalProcedureARevision1.version).toMatchObject({
    id: procedureA.version.id,
    state: 'superseded',
    content_hash: procedureA.version.content_hash,
  })
  expect(historicalProcedureARevision1.linked_program).toEqual(
    procedureA.version.validation_summary.linked_program,
  )
  expect(historicalProcedureARevision1.linked_program_current_state).toBe('superseded')
  const procedureARevision2Detail = await apiGet<EngineeringProductMutationRead & {
    linked_program: Record<string, unknown>
    linked_program_current_state: string
  }>(
    request,
    `/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions/${encodeURIComponent(procedureARevision2.version.id)}`,
  )
  expect(procedureARevision2Detail.version).toMatchObject({
    id: procedureARevision2.version.id,
    state: 'approved',
    content_hash: procedureARevision2.version.content_hash,
  })
  expect(procedureARevision2Detail.linked_program).toMatchObject({
    version_id: approvedProgramARevision2.version.id,
    content_hash: approvedProgramARevision2.version.content_hash,
  })
  expect(procedureARevision2Detail.linked_program_current_state).toBe('approved')
  const supersededProgramExport = await request.get(
    `/api/v1/engineering-products/${encodeURIComponent(procedureA.artifact.id)}/versions/${encodeURIComponent(procedureA.version.id)}/exports/json`,
    { headers: { 'X-Dev-Roles': 'engineer' } },
  )
  expect(supersededProgramExport.status()).toBe(200)
  const supersededProgramExportBody = await supersededProgramExport.json() as {
    revision: { linked_program_snapshot: Record<string, unknown>; linked_program_current_state: string; field_acceptance: string }
  }
  expect(supersededProgramExportBody.revision).toMatchObject({
    linked_program_snapshot: {
      version_id: programA.version.id,
      content_hash: programA.version.content_hash,
    },
    linked_program_current_state: 'superseded',
    field_acceptance: 'not_established',
  })

  const [productsA, productsB] = await Promise.all([
    apiGet<EngineeringProductListRead>(request, `/wells/${encodeURIComponent(wellA.well_id)}/engineering-products`),
    apiGet<EngineeringProductListRead>(request, `/wells/${encodeURIComponent(wellB.well_id)}/engineering-products`),
  ])
  expect(productsA.total).toBe(2)
  expect(productsB.total).toBe(2)
  expect(productsA.items.map((item) => item.artifact.well_id)).toEqual([wellA.well_id, wellA.well_id])
  expect(productsB.items.map((item) => item.artifact.well_id)).toEqual([wellB.well_id, wellB.well_id])
  expect(productsA.items.find((item) => item.artifact.id === programA.artifact.id)?.current_version).toMatchObject({
    id: approvedProgramARevision2.version.id,
    state: 'approved',
  })
  expect(productsA.items.find((item) => item.artifact.id === procedureA.artifact.id)?.current_version).toMatchObject({
    id: approvedProcedureARevision2.version.id,
    state: 'approved',
  })
  expect(productsB.items.find((item) => item.artifact.id === programB.artifact.id)?.current_version).toMatchObject({
    id: approvedProgramB.version.id,
    state: 'approved',
  })
  expect(productsB.items.find((item) => item.artifact.id === procedureB.artifact.id)?.current_version).toMatchObject({
    id: approvedProcedureB.version.id,
    state: 'approved',
  })

  const dbSnapshots = [
    { product: historicalProgramARevision1, version: historicalProgramARevision1.version, state: 'superseded', kind: 'program' as const },
    { product: approvedProgramARevision2, version: approvedProgramARevision2.version, state: 'approved', kind: 'program' as const },
    { product: historicalProcedureARevision1, version: historicalProcedureARevision1.version, state: 'superseded', kind: 'procedure' as const },
    { product: approvedProcedureARevision2, version: approvedProcedureARevision2.version, state: 'approved', kind: 'procedure' as const },
    { product: approvedProgramB, version: approvedProgramB.version, state: 'approved', kind: 'program' as const },
    { product: approvedProcedureB, version: approvedProcedureB.version, state: 'approved', kind: 'procedure' as const },
  ]
  for (const entry of dbSnapshots) {
    const artifact = entry.product.artifact
    const database = readEngineeringProductDatabaseSnapshotFromDatabase(
      pilot.database_url,
      artifact.id,
      artifact.well_id,
      artifact.wellbore_id,
      entry.version.id,
    )
    expect(database.artifact).toMatchObject({
      id: artifact.id,
      org_id: artifact.org_id,
      project_id: pilot.project_id,
      well_id: artifact.well_id,
      wellbore_id: artifact.wellbore_id,
      kind: entry.kind,
      current_version: entry.kind === 'program' && artifact.id === programA.artifact.id ? 2 :
        entry.kind === 'procedure' && artifact.id === procedureA.artifact.id ? 2 : 1,
      is_demo_fixture: 0,
    })
    expect(database.version).toMatchObject({
      id: entry.version.id,
      artifact_id: artifact.id,
      version: entry.version.version,
      state: entry.state,
      content_hash: entry.version.content_hash,
    })
    if (entry.version.id === historicalProgramARevision1.version.id) {
      expect(database.approval_history).toHaveLength(2)
      expect(database.approval_history).toEqual(expect.arrayContaining([
        expect.objectContaining({ status: 'cancelled', withdrawal: expect.objectContaining({ reason: 'Synthetic review restart; no operational authorization.' }), withdrawn_validation: expect.objectContaining({ ruleset: 'drillai.program-structure.v2' }) }),
        expect.objectContaining({ id: entry.version.approval_request_id, status: 'approved' }),
      ]))
    }
    const historyRow = database.versions.find((item) => item.id === entry.version.id)
    expect(historyRow).toMatchObject({ id: entry.version.id, state: entry.state, content_hash: entry.version.content_hash })
    expect(database.version.validation_summary).toMatchObject(entry.version.validation_summary)
    if (entry.version.id === procedureA.version.id) {
      expect(database.version.validation_summary.linked_program).toMatchObject({
        version_id: programA.version.id,
        content_hash: programA.version.content_hash,
      })
    }
    if (entry.version.id === procedureARevision2.version.id) {
      expect(database.version.validation_summary.linked_program).toMatchObject({
        version_id: approvedProgramARevision2.version.id,
        content_hash: approvedProgramARevision2.version.content_hash,
      })
    }
    if (entry.state === 'approved' || entry.state === 'superseded') {
      const expectedApproval = entry.product.approval_request
      expect(expectedApproval).toBeTruthy()
      expect(entry.version.approval_request_id).toBe(expectedApproval?.id)
      expect(database.version.approval_request_id).toBe(expectedApproval?.id)
      expect(database.approval).toMatchObject({
        id: expectedApproval?.id,
        status: 'approved',
        subject_kind: 'engineering_artifact_version',
        subject_id: entry.version.id,
        requested_by: expectedApproval?.requested_by,
        decided_by: expectedApproval?.decided_by,
        reviewed_by: entry.version.reviewed_by,
      })
      expect(entry.version.authored_by).toBeTruthy()
      expect(entry.version.reviewed_by).toBeTruthy()
      expect(entry.version.approved_by).toBeTruthy()
      expect(expectedApproval?.requested_by).not.toBe(entry.version.authored_by)
      expect(entry.version.reviewed_by).not.toBe(entry.version.authored_by)
      expect(entry.version.approved_by).not.toBe(entry.version.authored_by)
      expect(entry.version.approved_by).not.toBe(expectedApproval?.requested_by)
      expect(entry.version.approved_by).not.toBe(entry.version.reviewed_by)
    } else {
      expect(database.version.approval_request_id).toBeNull()
      expect(database.approval).toBeNull()
    }
  }

  const programAHistory = readEngineeringProductDatabaseSnapshotFromDatabase(
    pilot.database_url,
    programA.artifact.id,
    wellA.well_id,
    wellA.wellbore_id,
    approvedProgramARevision2.version.id,
  )
  expect(programAHistory.versions).toHaveLength(2)
  expect(programAHistory.versions.map((item) => item.state)).toEqual(['superseded', 'approved'])
  const procedureAHistory = readEngineeringProductDatabaseSnapshotFromDatabase(
    pilot.database_url,
    procedureA.artifact.id,
    wellA.well_id,
    wellA.wellbore_id,
    approvedProcedureARevision2.version.id,
  )
  expect(procedureAHistory.versions).toHaveLength(2)
  expect(procedureAHistory.versions.map((item) => item.state)).toEqual(['superseded', 'approved'])
  expect(procedureAHistory.version).toMatchObject({
    id: approvedProcedureARevision2.version.id,
    state: 'approved',
    content_hash: approvedProcedureARevision2.version.content_hash,
  })
  expect(procedureAHistory.version.validation_summary.linked_program).toMatchObject({
    version_id: approvedProgramARevision2.version.id,
    content_hash: approvedProgramARevision2.version.content_hash,
  })
  expect(procedureAHistory.version.validation_summary.validation).toMatchObject({
    state: 'warnings',
    linked_program_state: 'approved',
    field_acceptance: 'not_established',
  })

  expectNoFailures(consoleErrors, failedResponses)
})
