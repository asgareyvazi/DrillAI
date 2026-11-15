/**
 * Real-browser survey journeys: create and correct immutable revisions, compare/export results, and
 * import only source records that a human has reviewed (including current unit-inference review).
 */

import {
  apiGet,
  expect,
  expectNoFailures,
  test,
  wellPath,
} from './fixtures'

interface WellboreList {
  items: Array<{ id: string; name: string; is_active: boolean }>
  total: number
}

interface SurveySummary {
  id: string
  wellbore_id: string
  name: string
  kind: string
  version: number
  is_current: boolean
  datum: string
  datum_provenance: string
  azimuth_reference: string | null
  azimuth_reference_id: string | null
  source_document_id: string | null
  station_count: number
  calculation_state: string
  engineering_acceptance: string
}

interface SurveyList {
  items: SurveySummary[]
  total: number
}

interface SurveyStation {
  id: string
  station_index: number
  md_si: number
  inclination_deg: number
  azimuth_deg: number
  input_azimuth_deg: number | null
  input_azimuth_reference: string | null
  input_azimuth_reference_id: string | null
  tvd_si: number
  tvdss_si: number | null
  grid_correction_deg: number | null
  evidence_ref: string | null
  raw_source: Record<string, unknown>
}

interface SurveyDetail {
  trajectory: SurveySummary & {
    engine_key: string | null
    inputs_hash: string | null
    outputs_hash: string | null
    validation: { calculation_parameters: Record<string, unknown> }
  }
  stations: SurveyStation[]
  history: SurveySummary[]
}

interface SourceRecord {
  id: string
  record_type: string
  validation_state: string
  review_version: number
  unit_inference_review_required: boolean
  unit_inference_reviewed: boolean
  quality_flags: string[]
  payload: Record<string, unknown>
}

interface DocumentDetail {
  document: { id: string; title: string; wellbore_id: string | null }
  records: SourceRecord[]
}

async function wellboreFor(request: import('./fixtures').ApiRequest, wellId: string): Promise<string> {
  const result = await apiGet<WellboreList>(request, `/wells/${wellId}/wellbores`)
  const wellbore = result.items.find((row) => row.is_active) ?? result.items[0]
  expect(wellbore, 'the seeded synthetic well must have a wellbore').toBeTruthy()
  return wellbore?.id as string
}

test.describe('survey revision and source review in a real browser', () => {
  test('creates, corrects, compares, visualizes and exports source-backed calculation revisions', async ({
    page,
    request,
    wellId,
    consoleErrors,
    failedResponses,
  }) => {
    const wellboreId = await wellboreFor(request, wellId)
    const name = `E2E survey ${Date.now().toString(36)}`

    await page.goto(wellPath('/surveys'))
    const boreSelect = page.getByLabel('Select a wellbore')
    await expect(boreSelect).toBeVisible()
    await boreSelect.selectOption(wellboreId)
    await page.getByRole('button', { name: 'New manual trajectory' }).click()
    await page.getByLabel('Series name').fill(name)
    await page.getByLabel('Reason for this revision').fill('Synthetic browser test initial survey')
    await page.getByLabel('Measured depth 1').fill('0')
    await page.getByLabel('Inclination 1').fill('0')
    await page.getByLabel('Input azimuth 1').fill('0')
    await page.getByLabel('Azimuth reference').nth(0).selectOption('grid')
    await page.getByLabel('Measured depth 2').fill('100')
    await page.getByLabel('Inclination 2').fill('10')
    await page.getByLabel('Input azimuth 2').fill('5')
    await page.getByLabel('Azimuth reference').nth(1).selectOption('grid')
    await page.getByLabel('Exact frame identity').nth(0).fill('Synthetic E2E grid frame')
    await page.getByLabel('Exact frame identity').nth(1).fill('Synthetic E2E grid frame')
    await page.getByRole('button', { name: 'Calculate and save revision' }).click()
    await expect(page.getByText(/Trajectory revision saved/)).toBeVisible()
    await expect(page.getByRole('img', { name: 'Plan view (east / north, m)' })).toBeVisible()
    await expect(page.getByRole('img', { name: 'Measured depth / true vertical depth (m)' })).toBeVisible()

    const firstList = await apiGet<SurveyList>(request, `/wells/${wellId}/surveys`)
    const first = firstList.items.find((item) => item.name === name && item.version === 1)
    expect(first, 'the initial revision must be persisted by the API').toBeTruthy()
    expect(first?.is_current).toBe(true)
    expect(first?.calculation_state).toBe('formula_derived')
    const firstDetail = await apiGet<SurveyDetail>(request, `/wells/${wellId}/surveys/${first?.id}`)
    expect(firstDetail.stations).toHaveLength(2)
    expect(firstDetail.trajectory.engine_key).toBe('trajectory.minimum_curvature')
    expect(firstDetail.trajectory.inputs_hash).toMatch(/^[a-f0-9]{64}$/)
    expect(firstDetail.trajectory.outputs_hash).toMatch(/^[a-f0-9]{64}$/)

    await page.getByRole('button', { name: 'Correct current revision' }).click()
    await page.getByLabel('Reason for this revision').fill('Synthetic browser test correction')
    await page.getByLabel('Inclination 2').fill('15')
    await page.getByRole('button', { name: 'Calculate and save revision' }).click()
    await expect(page.getByText(/Trajectory revision saved/)).toBeVisible()

    const revisedList = await apiGet<SurveyList>(request, `/wells/${wellId}/surveys`)
    const revised = revisedList.items.find((item) => item.name === name && item.version === 2)
    expect(revised, 'a correction must create a new immutable revision').toBeTruthy()
    expect(revised?.is_current).toBe(true)
    const historical = revisedList.items.find((item) => item.name === name && item.version === 1)
    expect(historical?.is_current).toBe(false)
    const revisedDetail = await apiGet<SurveyDetail>(request, `/wells/${wellId}/surveys/${revised?.id}`)
    expect(revisedDetail.stations[1]?.inclination_deg).toBe(15)
    expect(revisedDetail.history.map((row) => row.version)).toEqual([2, 1])

    await page.getByLabel('Compare with revision').selectOption(historical?.id ?? '')
    await page.getByRole('button', { name: 'Compare revisions' }).click()
    await expect(page.getByText(/2 matched · 0 added · 0 removed/)).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Export provenance CSV' }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`trajectory-${revised?.id}.csv`)
    expectNoFailures(consoleErrors, failedResponses)
  })

  test('requires current unit review, then imports the explicitly reviewed source rows', async ({
    page,
    request,
    wellId,
    consoleErrors,
    failedResponses,
  }) => {
    const wellboreId = await wellboreFor(request, wellId)
    const tag = Date.now().toString(36)
    const title = `Synthetic survey source ${tag}`
    const csv = [
      'MD,Inclination,Grid Azimuth,Azimuth Reference ID,Vertical Datum',
      '0,0,0,SYNTHETIC-GRID-01,RKB',
      '100,10,5,SYNTHETIC-GRID-01,RKB',
      '',
    ].join('\n')
    const upload = await request.post('/api/v1/documents', {
      headers: { 'X-Dev-Roles': 'engineer' },
      multipart: {
        file: {
          name: `synthetic-survey-${tag}.csv`,
          mimeType: 'text/csv',
          buffer: Buffer.from(csv, 'utf8'),
        },
        well_id: wellId,
        wellbore_id: wellboreId,
        doc_type: 'survey_report',
        title,
      },
    })
    expect(upload.ok(), `survey source upload -> ${upload.status()}`).toBeTruthy()
    const uploaded = (await upload.json()) as { document: { id: string } }
    await expect.poll(async () => {
      const detail = await apiGet<DocumentDetail>(request, `/documents/${uploaded.document.id}`)
      return detail.records.filter((record) => record.record_type === 'survey_station').length
    }, { timeout: 60_000, message: 'the survey CSV extractor must produce source-backed station rows' }).toBe(2)
    const source = await apiGet<DocumentDetail>(request, `/documents/${uploaded.document.id}`)
    const records = source.records.filter((record) => record.record_type === 'survey_station')
    expect(records).toHaveLength(2)
    expect(records.every((record) => record.quality_flags.includes('unit_inferred'))).toBe(true)
    expect(records.every((record) => record.validation_state !== 'human_validated')).toBe(true)

    await page.goto(wellPath('/surveys'))
    await page.getByLabel('Select a wellbore').selectOption(wellboreId)
    await page.getByRole('button', { name: 'Import reviewed records' }).click()
    await page.getByLabel('Series name').fill(`E2E imported survey ${tag}`)
    await page.getByLabel('Reason for this revision').fill('Synthetic browser source review and import')
    await page.getByLabel('Survey source document').selectOption(uploaded.document.id)
    await expect(page.getByText('Unit review required')).toHaveCount(2)
    const ineligibleChecks = page.getByRole('checkbox', { name: /Human-reviewed source records/ })
    await expect(ineligibleChecks).toHaveCount(2)
    await expect(ineligibleChecks.nth(0)).toBeDisabled()
    await expect(ineligibleChecks.nth(1)).toBeDisabled()

    await page.goto(`/wells/${wellId}/documents?document=${uploaded.document.id}`)
    for (const record of records) {
      await page.getByTestId(`extracted-record-review-open-${record.id}`).click()
      await expect(page.getByRole('heading', { name: 'Review extracted record' })).toBeVisible()
      await expect(page.getByText('unit_inferred', { exact: true })).toBeVisible()
      await page.getByLabel('I inspected the source and explicitly reviewed the inferred-unit fields and accepted values, including any units that remain unknown.').check()
      await page.getByTestId('extracted-record-review-reason').fill('Reviewed against the synthetic source record')
      await page.getByTestId('extracted-record-review-submit').click()
      await expect(page.getByTestId('extracted-record-review-form')).toHaveCount(0)
    }

    await expect.poll(async () => {
      const detail = await apiGet<DocumentDetail>(request, `/documents/${uploaded.document.id}`)
      const reviewed = detail.records.filter((record) => record.record_type === 'survey_station')
      return reviewed.length === 2 && reviewed.every((record) =>
        record.validation_state === 'human_validated' &&
        record.unit_inference_reviewed &&
        record.review_version >= 2,
      )
    }).toBe(true)

    await page.goto(wellPath('/surveys'))
    await page.getByLabel('Select a wellbore').selectOption(wellboreId)
    await page.getByRole('button', { name: 'Import reviewed records' }).click()
    const seriesName = `E2E imported survey ${tag}`
    await page.getByLabel('Series name').fill(seriesName)
    await page.getByLabel('Trajectory kind').selectOption('plan')
    await page.getByLabel('Reason for this revision').fill('Synthetic browser source review and import')
    await page.getByLabel('Survey source document').selectOption(uploaded.document.id)
    await page.getByLabel('Vertical datum').selectOption('rkb')
    await expect(page.getByText('Human validated')).toHaveCount(2)
    const eligibleChecks = page.getByRole('checkbox', { name: /Human-reviewed source records/ })
    await expect(eligibleChecks).toHaveCount(2)
    await eligibleChecks.nth(0).check()
    await eligibleChecks.nth(1).check()
    await expect(page.getByRole('button', { name: 'Calculate from selected records' })).toBeEnabled()
    await page.getByRole('button', { name: 'Calculate from selected records' }).click()
    await expect(page.getByText(/Trajectory revision saved/)).toBeVisible()

    const importedList = await apiGet<SurveyList>(request, `/wells/${wellId}/surveys`)
    const imported = importedList.items.find((item) => item.name === seriesName)
    expect(imported).toBeTruthy()
    expect(imported?.source_document_id).toBe(uploaded.document.id)
    expect(imported?.station_count).toBe(2)
    expect(imported?.engineering_acceptance).toBe('not_established')
    const importedDetail = await apiGet<SurveyDetail>(request, `/wells/${wellId}/surveys/${imported?.id}`)
    expect(importedDetail.stations).toHaveLength(2)
    expect(importedDetail.stations.every((station) => station.evidence_ref)).toBe(true)
    expect(importedDetail.stations.every((station) => station.raw_source.source_record_id)).toBe(true)
    expect(importedDetail.stations.every((station) => station.raw_source.human_review)).toBe(true)
    expectNoFailures(consoleErrors, failedResponses)
  })

  test('requires explicit datum and azimuth-frame identities and reads back declared corrections', async ({
    page,
    request,
    wellId,
    consoleErrors,
    failedResponses,
  }) => {
    const wellboreId = await wellboreFor(request, wellId)
    const name = `E2E explicit frame survey ${Date.now().toString(36)}`
    const gridFrameId = 'SYNTHETIC-GRID-FRAME-01'
    const trueFrameId = 'SYNTHETIC-TRUE-FRAME-01'

    await page.goto(wellPath('/surveys'))
    await page.getByLabel('Select a wellbore').selectOption(wellboreId)
    await page.getByRole('button', { name: 'New manual trajectory' }).click()
    await page.getByLabel('Series name').fill(name)
    await page.getByLabel('Trajectory kind').selectOption('plan')
    await page.getByLabel('Reason for this revision').fill('Synthetic browser check of explicit datum and azimuth frames')
    await page.getByLabel('Measured depth 1').fill('0')
    await page.getByLabel('Inclination 1').fill('0')
    await page.getByLabel('Input azimuth 1').fill('0')
    await page.getByLabel('Measured depth 2').fill('100')
    await page.getByLabel('Inclination 2').fill('10')
    await page.getByLabel('Input azimuth 2').fill('5')

    await page.getByLabel('Vertical datum').selectOption('msl')
    await page.getByLabel('KB elevation').fill('25')
    await page.getByRole('button', { name: 'Calculate and save revision' }).click()
    await expect(page.getByRole('alert')).toHaveText('KB elevation is only accepted with an RKB datum; no conversion is applied.')

    await page.getByLabel('Vertical datum').selectOption('rkb')
    await page.getByLabel('KB elevation').fill('0')
    await page.getByRole('button', { name: 'Calculate and save revision' }).click()
    await expect(page.getByRole('alert')).toHaveText(
      'Station 1 has no known azimuth reference; select grid, true, or magnetic before calculating.',
    )

    const stationForms = page.locator('fieldset')
    await stationForms.nth(0).getByLabel('Azimuth reference').selectOption('true')
    await stationForms.nth(1).getByLabel('Azimuth reference').selectOption('true')
    await page.getByRole('button', { name: 'Calculate and save revision' }).click()
    await expect(page.getByRole('alert')).toHaveText('Station 1 needs an exact identity for its declared azimuth frame.')

    await stationForms.nth(0).getByLabel('Exact frame identity').fill(trueFrameId)
    await stationForms.nth(1).getByLabel('Exact frame identity').fill(trueFrameId)
    await page.getByLabel('Target azimuth frame (optional)').selectOption('grid')
    await page.getByLabel('Exact frame identity').nth(0).fill(gridFrameId)
    await stationForms.nth(0).getByLabel('Grid correction (optional)').fill('-2')
    await stationForms.nth(1).getByLabel('Grid correction (optional)').fill('1')
    await page.getByLabel('Vertical-section azimuth (optional)').fill('90')
    await page.getByLabel('Azimuth reference').nth(0).selectOption('grid')
    await page.getByLabel('Exact frame identity').nth(1).fill(gridFrameId)

    await page.getByRole('button', { name: 'Calculate and save revision' }).click()
    await expect(page.getByText(/Trajectory revision saved/)).toBeVisible()
    await expect(page.getByText('Calculated coordinate frame', { exact: true })).toBeVisible()
    await expect(page.getByText(gridFrameId, { exact: true })).toHaveCount(2)
    await expect(page.getByText(trueFrameId, { exact: true })).toHaveCount(2)

    const list = await apiGet<SurveyList>(request, `/wells/${wellId}/surveys`)
    const saved = list.items.find((item) => item.name === name && item.version === 1)
    expect(saved, 'the browser-created frame-aware trajectory must be persisted').toBeTruthy()
    expect(saved?.datum).toBe('rkb')
    expect(saved?.datum_provenance).toBe('engine_input')
    expect(saved?.azimuth_reference).toBe('grid')
    expect(saved?.azimuth_reference_id).toBe(gridFrameId)

    const detail = await apiGet<SurveyDetail>(request, `/wells/${wellId}/surveys/${saved?.id}`)
    const parameters = detail.trajectory.validation.calculation_parameters
    expect(detail.trajectory.datum).toBe('rkb')
    expect(parameters.kb_elevation_si).toBe(0)
    expect(parameters.target_azimuth_reference).toBe('grid')
    expect(parameters.target_azimuth_reference_id).toBe(gridFrameId)
    expect(parameters.vertical_section_azimuth_deg).toBe(90)
    expect(parameters.vertical_section_azimuth_reference).toBe('grid')
    expect(parameters.vertical_section_azimuth_reference_id).toBe(gridFrameId)
    expect(detail.stations).toHaveLength(2)
    expect(detail.stations[0]?.input_azimuth_reference).toBe('true')
    expect(detail.stations[0]?.input_azimuth_reference_id).toBe(trueFrameId)
    expect(detail.stations[0]?.grid_correction_deg).toBe(-2)
    expect(detail.stations[0]?.azimuth_deg).toBe(358)
    expect(detail.stations[0]?.tvdss_si).toBeCloseTo(detail.stations[0]?.tvd_si ?? Number.NaN)
    expect(detail.stations[1]?.input_azimuth_reference_id).toBe(trueFrameId)
    expect(detail.stations[1]?.grid_correction_deg).toBe(1)
    expect(detail.stations[1]?.azimuth_deg).toBeCloseTo(6)
    expectNoFailures(consoleErrors, failedResponses)
  })
})
