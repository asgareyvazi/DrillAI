import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  DocumentDetail,
  DocumentRow,
  ExtractionRecord,
  SurveyComparison,
  SurveyTrajectoryKind,
  SurveyTrajectoryResponse,
  SurveyTrajectoryStation,
  SurveyTrajectorySummary,
} from '../../api/types'
import { I18nProvider } from '../../i18n'
import SurveysWorkspace from './SurveysWorkspace'

const apiMock = vi.hoisted(() => ({
  getWell: vi.fn(),
  listSurveys: vi.fn(),
  getSurvey: vi.fn(),
  listDocuments: vi.fn(),
  getDocument: vi.fn(),
  createSurvey: vi.fn(),
  importSurvey: vi.fn(),
  compareSurveys: vi.fn(),
  exportSurveyCsv: vi.fn(),
}))

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    getWell: apiMock.getWell,
    listSurveys: apiMock.listSurveys,
    getSurvey: apiMock.getSurvey,
    listDocuments: apiMock.listDocuments,
    getDocument: apiMock.getDocument,
    createSurvey: apiMock.createSurvey,
    importSurvey: apiMock.importSurvey,
    compareSurveys: apiMock.compareSurveys,
    exportSurveyCsv: apiMock.exportSurveyCsv,
  },
}))

const well = {
  id: 'wel_surveys',
  project_id: 'prj_surveys',
  field_id: null,
  name: 'Synthetic survey well',
  uwi: null,
  api_number: null,
  well_type: 'vertical' as const,
  status: 'active' as const,
  spud_date: null,
  release_date: null,
  operator: null,
  rig_id: null,
  is_offshore: false,
  surface_lat: null,
  surface_lon: null,
  kb_elevation_si: null,
  ground_elevation_si: null,
  water_depth_si: null,
  elevation_datum: 'unknown' as const,
  slot: null,
  pad_name: null,
  total_depth_planned_si: null,
  twin_state: 'unknown',
  objectives: null,
  target_formations: [],
  tags: [],
}

const wellbore = {
  id: 'wbr_surveys',
  well_id: well.id,
  name: 'Synthetic main wellbore',
  purpose: 'original' as const,
  sequence: 1,
  parent_wellbore_id: null,
  status: 'drilling' as const,
  is_active: true,
  datum: 'unknown' as const,
  kickoff_md_si: null,
  planned_td_md_si: null,
  planned_td_tvd_si: null,
  actual_td_md_si: null,
  actual_td_tvd_si: null,
}

const sourceDocument: DocumentRow = {
  id: 'doc_survey_ddr',
  project_id: well.project_id,
  well_id: well.id,
  wellbore_id: wellbore.id,
  section_id: null,
  operation_id: null,
  doc_type: 'ddr',
  title: 'Synthetic Daily Drilling Report',
  document_number: null,
  revision: null,
  issue_date: null,
  period_start: null,
  period_end: null,
  language: 'en',
  page_count: 1,
  status: 'extracted',
  extraction_summary: {
    pages: 1,
    regions: 1,
    chunks: 1,
    records: 3,
    evidence_links: 0,
    extractors: { survey_table: '1.4.0' },
    doc_type: 'ddr',
  },
  has_tables: true,
  has_figures: false,
  ocr_required: false,
  raw_artifact_id: 'artifact_survey_ddr',
  logical_key: 'synthetic-survey-ddr',
  is_demo_fixture: true,
  created_at: '2026-11-01T10:00:00Z',
}

function record(id: string, validationState: string, inferredUnits = false): ExtractionRecord {
  const reviewed = validationState === 'human_validated'
  return {
    id,
    document_id: sourceDocument.id,
    record_type: 'survey_station',
    payload: {
      md_si: id === 'record_1' ? 0 : id === 'record_2' ? 30 : 60,
      inclination_deg: 1,
      azimuth_deg: 90,
    },
    payload_truncated: false,
    payload_schema_key: 'extracted.survey_station',
    payload_schema_version: 1,
    page_number: 1,
    region_id: `region_${id}`,
    depth_md_si: null,
    depth_md_basis: 'unknown',
    depth_tvd_si: null,
    depth_tvd_basis: 'unknown',
    observed_at: null,
    observed_at_basis: 'unknown',
    method: 'survey_table',
    method_version: '1.4.0',
    confidence: 0.8,
    validation_state: validationState,
    review_version: reviewed ? 2 : 1,
    unit_inference_review_required: inferredUnits,
    unit_inference_reviewed: !inferredUnits,
    validated_by: reviewed ? 'usr_reviewer' : null,
    validated_at: reviewed ? '2026-11-01T10:30:00Z' : null,
    validation_notes: '',
    validation_notes_truncated: false,
    region_unknown: false,
    validation_rule: null,
    quality_flags: inferredUnits ? ['unit_inferred'] : [],
    unit_context: { md_si: 'm', inclination_deg: 'deg', azimuth_deg: 'deg' },
    promoted_to_kind: null,
    promoted_to_id: null,
    is_demo_fixture: true,
  }
}

const sourceRecords = [
  record('record_1', 'human_validated'),
  record('record_2', 'human_validated'),
  record('record_3', 'extracted'),
  record('record_4', 'human_validated', true),
]

const documentDetail: DocumentDetail = {
  document: sourceDocument,
  ingestion_jobs: [],
  region_count: 1,
  chunks: [],
  records: sourceRecords,
  evidence_links: [],
}

const createdSurvey: SurveyTrajectoryResponse = {
  trajectory: {
    id: 'trj_survey_created',
    wellbore_id: wellbore.id,
    name: 'Reviewed DDR survey',
    kind: 'actual',
    version: 1,
    is_current: true,
    datum: 'unknown',
    datum_provenance: 'engine_input',
    azimuth_reference: 'true',
    azimuth_reference_id: 'true-frame-synthetic',
    source_document_id: sourceDocument.id,
    engine_run_id: 'run_survey_created',
    station_count: 2,
    total_md_si: 30,
    total_tvd_si: 29.9,
    max_inclination_deg: 1,
    revision_reason: 'Synthetic review acceptance test',
    calculation_state: 'formula_derived',
    engineering_acceptance: 'not_established',
    created_by: 'usr_engineer',
    created_at: '2026-11-01T11:00:00Z',
    updated_at: '2026-11-01T11:00:00Z',
    notes: null,
    target_formation_id: null,
    validation: { calculation_parameters: {} },
    validation_truncated: false,
    unit_contract: {},
    engine_key: 'trajectory.minimum_curvature',
    engine_version: '1.2.0',
    inputs_hash: 'input-hash',
    outputs_hash: 'output-hash',
    warnings: [],
    violations: [],
    assumptions: [],
    limitations: [],
  },
  stations: [],
  history: [],
  station_count_returned: 0,
}

function localizationFixture() {
  const baseline: SurveyTrajectorySummary = {
    ...createdSurvey.trajectory,
    id: 'trj_survey_baseline',
    name: 'Synthetic baseline revision',
    version: 1,
    is_current: false,
    kind: 'actual',
  }
  const current = {
    ...createdSurvey.trajectory,
    id: 'trj_survey_current',
    name: 'Synthetic current revision',
    version: 2,
    is_current: true,
    kind: 'unexpected_kind' as unknown as SurveyTrajectoryKind,
    datum: 'rkb',
    datum_provenance: 'provider_specific',
    calculation_state: 'legacy_unclassified',
    warnings: [
      'TVDSS was not calculated because the vertical datum is unknown',
      'TVDSS was not calculated because an RKB-referenced TVD requires KB elevation above MSL',
      'TVDSS was not calculated because no MSL conversion is defined for the gl datum',
      'trajectory coordinates are referenced to magnetic north; they have not been converted to grid or true north',
      'station gap of 30.0 m before MD 100.0 m reduces accuracy',
      'dogleg severity 15.00 deg/30m at MD 100.0 m exceeds 12.00 deg/30m',
    ],
  }
  const station = (index: number, md: number, inclination: number, azimuth: number, reference = 'grid'): SurveyTrajectoryStation => ({
    id: `stn_survey_${index + 1}`,
    trajectory_id: current.id,
    wellbore_id: wellbore.id,
    station_index: index,
    md_si: md,
    inclination_deg: inclination,
    azimuth_deg: azimuth,
    input_azimuth_deg: azimuth,
    input_azimuth_reference: reference,
    input_azimuth_reference_id: `${reference}-frame-synthetic`,
    tvd_si: md * 0.98,
    ns_si: index * 10,
    ew_si: index * 5,
    tvdss_si: null,
    dls_deg_per_30m: index === 0 ? null : 1,
    build_rate_deg_per_30m: null,
    turn_rate_deg_per_30m: null,
    closure_distance_si: null,
    closure_azimuth_deg: null,
    vertical_section_si: null,
    toolface_deg: null,
    source: index === 0 ? 'manual' : 'gyro',
    measured_at: null,
    measured_at_provenance: 'unknown',
    grid_correction_deg: null,
    quality_flags: [],
    method: 'manual',
    engine_version: '1.2.0',
    evidence_ref: null,
    raw_source: {
      calculation: {
        accepted_engine_input: {
          md_si: md,
          inclination_deg: inclination,
          azimuth_deg: azimuth,
          azimuth_reference: reference,
          azimuth_reference_id: `${reference}-frame-synthetic`,
          source: index === 0 ? 'manual' : 'gyro',
        },
      },
    },
    raw_source_truncated: false,
  })
  const stations = [station(0, 0, 0, 0), station(1, 100, 10, 90, 'vendor_frame')]
  const detail: SurveyTrajectoryResponse = {
    ...createdSurvey,
    trajectory: current,
    stations,
    history: [current, baseline],
    station_count_returned: stations.length,
  }
  const comparison: SurveyComparison = {
    comparison_basis: 'exact_md',
    unit_contract: {},
    baseline,
    current,
    matched_count: 1,
    added_count: 1,
    removed_count: 0,
    items: [
      {
        md_si: 0,
        state: 'matched_exact_md',
        baseline_station_id: 'stn_baseline_1',
        current_station_id: 'stn_survey_1',
        baseline: { tvd_si: 0, ns_si: 0, ew_si: 0, inclination_deg: 0, azimuth_deg: 0 },
        current: { tvd_si: 0, ns_si: 0, ew_si: 0, inclination_deg: 0, azimuth_deg: 0 },
        delta: { tvd_si: 0, ns_si: 0, ew_si: 0, inclination_deg: 0, azimuth_deg: 0 },
      },
      {
        md_si: 100,
        state: 'added',
        baseline_station_id: null,
        current_station_id: 'stn_survey_2',
        baseline: { tvd_si: null, ns_si: null, ew_si: null, inclination_deg: null, azimuth_deg: null },
        current: { tvd_si: 98, ns_si: 10, ew_si: 5, inclination_deg: 10, azimuth_deg: 90 },
        delta: { tvd_si: null, ns_si: null, ew_si: null, inclination_deg: null, azimuth_deg: null },
      },
    ],
  }
  return { baseline, current, detail, comparison }
}

function renderWorkspace(locale: 'en' | 'fa' = 'en') {
  window.localStorage.setItem('drillai.locale', locale)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[`/wells/${well.id}/surveys`]}>
            <Routes>
              <Route path="/wells/:wellId/surveys" element={children} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<SurveysWorkspace />, { wrapper: Wrapper })
}

beforeEach(() => {
  window.localStorage.clear()
  window.localStorage.setItem('drillai.locale', 'en')
  apiMock.getWell.mockReset().mockResolvedValue({ ...well, wellbores: [wellbore] })
  apiMock.listSurveys.mockReset().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 })
  apiMock.getSurvey.mockReset().mockResolvedValue(createdSurvey)
  apiMock.listDocuments.mockReset().mockResolvedValue({ items: [sourceDocument], total: 1, limit: 500, offset: 0 })
  apiMock.getDocument.mockReset().mockResolvedValue(documentDetail)
  apiMock.createSurvey.mockReset()
  apiMock.importSurvey.mockReset().mockResolvedValue(createdSurvey)
  apiMock.compareSurveys.mockReset()
  apiMock.exportSurveyCsv.mockReset()
})

const localizationCases = [
  {
    locale: 'en' as const,
    labels: {
      direction: 'ltr',
      original: 'Original',
      unknownKind: 'Unrecognized value: unexpected_kind',
      unknownDatumProvenance: 'Unrecognized value: provider_specific',
      unknownFrame: 'Unrecognized value: vendor_frame',
      antiCollision: 'Anti-collision',
      planView: 'Plan view (east / north, m)',
      depthView: 'Measured depth / true vertical depth (m)',
      east: 'East / west',
      tvd: 'True vertical depth',
      positiveDown: 'positive down',
      warningUnknownDatum: 'TVDSS was not calculated because the vertical datum is unknown.',
      warningMissingKb: 'TVDSS was not calculated because an RKB-referenced TVD requires KB elevation above MSL.',
      warningUnsupportedDatum: 'TVDSS was not calculated because no MSL conversion is defined for the Ground level (GL) datum.',
      warningMagneticNorth: 'Trajectory coordinates are referenced to magnetic north; they have not been converted to grid or true north.',
      warningStationGap: 'A station gap of 30 m before measured depth 100 m reduces accuracy.',
      warningHighDls: 'Dogleg severity 15 deg/30 m at measured depth 100 m exceeds 12 deg/30 m.',
      grid: 'Grid',
      manual: 'Manual',
      matched: 'Matched at exact measured depth',
      added: 'Added',
      compareWith: 'Compare with revision',
      compare: 'Compare revisions',
      deltaTvd: 'Change in True vertical depth (m)',
      newManual: 'New manual trajectory',
      kind: 'Trajectory kind',
      unitMetre: 'Metre (m)',
      unitFoot: 'Foot (ft)',
      unitDegree: 'Degree (deg)',
      unitRadian: 'Radian (rad)',
      azimuthFrame: 'Azimuth reference',
      sourceManual: 'Manual',
      sourceGyro: 'Gyro survey',
      sourceUnknown: 'Unknown source',
      reviseSelected: 'Correct current revision',
      reason: 'Reason for this revision',
      createManual: 'Calculate and save revision',
      sourceDocument: 'Survey source document',
      sourceRecordSummary: 'Measured depth 0 m · inclination 1 deg · azimuth 90 deg',
      import: 'Import reviewed records',
      reviewRequired: 'Only records explicitly marked “Human validated” and carrying a reviewer and review version are eligible.',
      reviewValidated: 'Human validated',
      inputAzimuthHeader: 'Input azimuth (deg)',
    },
  },
  {
    locale: 'fa' as const,
    labels: {
      direction: 'rtl',
      original: 'چاه‌گمانهٔ اصلی',
      unknownKind: 'مقدار ناشناخته: unexpected_kind',
      unknownDatumProvenance: 'مقدار ناشناخته: provider_specific',
      unknownFrame: 'مقدار ناشناخته: vendor_frame',
      antiCollision: 'ضدبرخورد',
      planView: 'نمای پلان (شرق / شمال، متر)',
      depthView: 'عمق اندازه‌گیری / عمق قائم واقعی (متر)',
      east: 'شرق / غرب',
      tvd: 'عمق قائم واقعی',
      positiveDown: 'به‌سوی پایین مثبت',
      warningUnknownDatum: 'محاسبهٔ TVDSS انجام نشد، چون مبنای قائم نامعلوم است.',
      warningMissingKb: 'محاسبهٔ TVDSS انجام نشد، چون تبدیل عمق از RKB به ارتفاع بالای MSL نیاز دارد.',
      warningUnsupportedDatum: 'محاسبهٔ TVDSS انجام نشد، چون برای مبنای قائم سطح زمین (GL) تبدیل به MSL تعریف نشده است.',
      warningMagneticNorth: 'مختصات مسیر نسبت به شمال مغناطیسی هستند و به شمال شبکه‌ای یا حقیقی تبدیل نشده‌اند.',
      warningStationGap: 'فاصلهٔ ۳۰ متر پیش از عمق اندازه‌گیری ۱۰۰ متر، دقت را کاهش می‌دهد.',
      warningHighDls: 'شدت تغییر زاویهٔ ۱۵ درجه بر ۳۰ متر در عمق اندازه‌گیری ۱۰۰ متر از حد ۱۲ درجه بر ۳۰ متر بیشتر است.',
      grid: 'شبکه‌ای',
      manual: 'ورود دستی',
      matched: 'تطبیق دقیق عمق اندازه‌گیری',
      added: 'افزوده‌شده',
      compareWith: 'مقایسه با بازنگری',
      compare: 'مقایسهٔ بازنگری‌ها',
      deltaTvd: 'تغییر عمق قائم واقعی (m)',
      newManual: 'مسیر دستی جدید',
      kind: 'نوع مسیر',
      unitMetre: 'متر (m)',
      unitFoot: 'فوت (ft)',
      unitDegree: 'درجه (deg)',
      unitRadian: 'رادیان (rad)',
      azimuthFrame: 'مرجع آزیموت',
      sourceManual: 'ورود دستی',
      sourceGyro: 'پیمایش ژیروسکوپی',
      sourceUnknown: 'منبع نامعلوم',
      reviseSelected: 'اصلاح بازنگری جاری',
      reason: 'دلیل این بازنگری',
      createManual: 'محاسبه و ذخیرهٔ بازنگری',
      sourceDocument: 'سند منبع مسیر',
      sourceRecordSummary: 'عمق اندازه‌گیری ۰ متر · انحراف ۱ درجه · آزیموت ۹۰ درجه',
      import: 'ورود رکوردهای بازبینی‌شده',
      reviewRequired: 'فقط رکوردهایی مجازند که صریحاً «بازبینی و تأیید انسانی» باشند و بازبین و نسخهٔ بازبینی داشته باشند.',
      reviewValidated: 'بازبینی و تأیید انسانی',
      inputAzimuthHeader: 'آزیموت ورودی (deg)',
    },
  },
]

describe('SurveysWorkspace localization', () => {
  it.each(localizationCases)('localizes trajectory, frame, plot and comparison labels in $locale', async ({ locale, labels }) => {
    const { baseline, current, detail, comparison } = localizationFixture()
    apiMock.listSurveys.mockResolvedValue({ items: [current, baseline], total: 2, limit: 100, offset: 0 })
    apiMock.getSurvey.mockResolvedValue(detail)
    apiMock.compareSurveys.mockResolvedValue(comparison)
    const user = userEvent.setup()
    renderWorkspace(locale)

    const planPlot = await screen.findByRole('img', { name: labels.planView })
    const depthPlot = screen.getByRole('img', { name: labels.depthView })
    expect(document.documentElement.lang).toBe(locale)
    expect(document.documentElement.dir).toBe(labels.direction)
    expect(planPlot).toHaveTextContent(`${labels.east} (m)`)
    expect(depthPlot).toHaveTextContent(`(m, ${labels.positiveDown})`)

    expect(screen.getByRole('option', { name: /Synthetic main wellbore/ })).toHaveTextContent(labels.original)
    expect(document.body.textContent).toContain(labels.unknownKind)
    expect(document.body.textContent).toContain(labels.unknownDatumProvenance)
    expect(document.body.textContent).toContain(labels.unknownFrame)
    expect(screen.queryByText(labels.antiCollision, { exact: true })).not.toBeInTheDocument()
    expect(screen.getByText(labels.warningUnknownDatum)).toBeInTheDocument()
    expect(screen.getByText(labels.warningMissingKb)).toBeInTheDocument()
    expect(screen.getByText(labels.warningUnsupportedDatum)).toBeInTheDocument()
    expect(screen.getByText(labels.warningMagneticNorth)).toBeInTheDocument()
    expect(screen.getByText(labels.warningStationGap)).toBeInTheDocument()
    expect(screen.getByText(labels.warningHighDls)).toBeInTheDocument()
    expect(document.body.textContent).toContain(labels.inputAzimuthHeader)
    expect(document.body.textContent).toContain(labels.grid)
    expect(document.body.textContent).toContain(labels.manual)
    expect(document.body.textContent).toContain(labels.sourceGyro)

    await user.selectOptions(screen.getByRole('combobox', { name: new RegExp(labels.compareWith) }), baseline.id)
    await user.click(screen.getByRole('button', { name: labels.compare }))
    expect(await screen.findByText(labels.matched)).toBeInTheDocument()
    expect(screen.getByText(labels.added, { exact: true })).toBeInTheDocument()
    expect(document.body.textContent).toContain(labels.deltaTvd)

    await user.click(screen.getByRole('button', { name: labels.newManual }))
    expect(screen.getByRole('combobox', { name: labels.kind })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: labels.antiCollision })).toBeInTheDocument()
    expect(screen.getAllByRole('option', { name: labels.unitMetre }).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('option', { name: labels.unitFoot }).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('option', { name: labels.unitDegree }).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('option', { name: labels.unitRadian }).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('option', { name: labels.grid }).length).toBeGreaterThan(0)
    expect(screen.getAllByPlaceholderText(labels.sourceManual)).toHaveLength(2)
  })

  it.each(localizationCases)('localizes and preserves survey-station source values in $locale', async ({ locale, labels }) => {
    const fixture = localizationFixture()
    const stations = fixture.detail.stations.map((station, index) => {
      const source = index === 0 ? 'unknown' : 'gyro'
      const frameId = `synthetic-grid-frame-${index}`
      return {
        ...station,
        source,
        input_azimuth_reference: 'grid',
        input_azimuth_reference_id: frameId,
        raw_source: {
          calculation: {
            accepted_engine_input: {
              md_si: station.md_si,
              inclination_deg: station.inclination_deg,
              azimuth_deg: station.input_azimuth_deg,
              azimuth_reference: 'grid',
              azimuth_reference_id: frameId,
              source,
            },
          },
        },
      }
    })
    const trajectory = {
      ...fixture.current,
      kind: 'actual' as const,
      datum: 'unknown',
      datum_provenance: 'unknown',
      warnings: [],
      validation: { calculation_parameters: {} },
    }
    const detail: SurveyTrajectoryResponse = { ...fixture.detail, trajectory, stations }
    apiMock.listSurveys.mockResolvedValue({ items: [trajectory, fixture.baseline], total: 2, limit: 100, offset: 0 })
    apiMock.getSurvey.mockResolvedValue(detail)
    apiMock.createSurvey.mockResolvedValue(createdSurvey)
    const user = userEvent.setup()
    renderWorkspace(locale)

    await user.click(await screen.findByRole('button', { name: labels.reviseSelected }))
    expect(screen.getByPlaceholderText(labels.sourceUnknown)).toHaveValue('')
    expect(document.body.textContent).toContain(labels.sourceUnknown)
    expect(document.body.textContent).toContain(labels.sourceGyro)
    await user.type(screen.getByLabelText(labels.reason), 'Synthetic source-preservation regression')
    await user.click(screen.getByRole('button', { name: labels.createManual }))

    await waitFor(() => expect(apiMock.createSurvey).toHaveBeenCalledOnce())
    expect(apiMock.createSurvey).toHaveBeenCalledWith(
      well.id,
      expect.objectContaining({
        stations: [
          expect.objectContaining({ source: 'unknown' }),
          expect.objectContaining({ source: 'gyro' }),
        ],
      }),
      expect.any(String),
    )
  })

  it.each(localizationCases)('localizes reviewed source-record values and validation state in $locale', async ({ locale, labels }) => {
    const user = userEvent.setup()
    renderWorkspace(locale)

    await user.click(await screen.findByRole('button', { name: labels.import }))
    const documentSelector = await screen.findByLabelText(labels.sourceDocument)
    await user.selectOptions(documentSelector, sourceDocument.id)

    expect(await screen.findByText(labels.sourceRecordSummary)).toBeInTheDocument()
    expect(await screen.findAllByText(labels.reviewValidated)).not.toHaveLength(0)
    expect(screen.getByText(labels.reviewRequired)).toBeInTheDocument()
    expect(screen.queryByText('human_validated', { exact: true })).not.toBeInTheDocument()
  })

  it('retains unsupported trajectory frames in the editor and refuses implicit reinterpretation', async () => {
    const fixture = localizationFixture()
    const detail: SurveyTrajectoryResponse = {
      ...fixture.detail,
      trajectory: {
        ...fixture.detail.trajectory,
        datum: 'vendor_datum',
        warnings: [],
        validation: {
          calculation_parameters: {
            target_azimuth_reference: 'vendor_target_frame',
            target_azimuth_reference_id: 'synthetic-target-frame-id',
            vertical_section_azimuth_deg: 90,
            vertical_section_azimuth_reference: 'vendor_vertical_frame',
            vertical_section_azimuth_reference_id: 'synthetic-vertical-frame-id',
          },
        },
      },
    }
    apiMock.listSurveys.mockResolvedValue({ items: [fixture.current, fixture.baseline], total: 2, limit: 100, offset: 0 })
    apiMock.getSurvey.mockResolvedValue(detail)
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(await screen.findByRole('button', { name: 'Correct current revision' }))
    const targetReference = screen.getByLabelText('Target azimuth frame (optional)')
    const frameSelectors = screen.getAllByRole('combobox', { name: 'Azimuth reference' })
    expect(screen.getByLabelText('Trajectory kind')).toHaveValue('unexpected_kind')
    expect(screen.getByRole('option', { name: 'Unrecognized value: unexpected_kind' })).toBeInTheDocument()
    expect(screen.getByLabelText('Vertical datum')).toHaveValue('vendor_datum')
    expect(screen.getByRole('option', { name: 'Unrecognized value: vendor_datum' })).toBeInTheDocument()
    expect(targetReference).toHaveValue('vendor_target_frame')
    expect(screen.getByRole('option', { name: 'Unrecognized value: vendor_target_frame' })).toBeInTheDocument()
    expect(frameSelectors[0]).toHaveValue('vendor_vertical_frame')
    expect(frameSelectors[2]).toHaveValue('vendor_frame')
    expect(screen.getByRole('option', { name: 'Unrecognized value: vendor_vertical_frame' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Unrecognized value: vendor_frame' })).toBeInTheDocument()

    await user.type(screen.getByLabelText('Reason for this revision'), 'Synthetic unknown-frame preservation check')
    const saveButton = screen.getByRole('button', { name: 'Calculate and save revision' })
    await user.click(saveButton)
    expect(await screen.findByRole('alert')).toHaveTextContent('Unrecognized target azimuth frame “vendor_target_frame”')
    expect(apiMock.createSurvey).not.toHaveBeenCalled()

    await user.selectOptions(targetReference, '')
    await user.clear(screen.getByLabelText('Vertical-section azimuth (optional)'))
    await user.click(saveButton)
    expect(await screen.findByRole('alert')).toHaveTextContent('Unrecognized azimuth frame “vendor_frame”')
    expect(apiMock.createSurvey).not.toHaveBeenCalled()
  })
})

describe('SurveysWorkspace reviewed import', () => {
  it('selects a DDR, disables machine-only records and sends only explicit human-reviewed rows', async () => {
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(await screen.findByRole('button', { name: 'Import reviewed records' }))
    const documentSelector = await screen.findByLabelText('Survey source document')
    await user.selectOptions(documentSelector, sourceDocument.id)

    const firstReviewed = await screen.findByRole('checkbox', { name: `Human-reviewed source records record_1` })
    const secondReviewed = screen.getByRole('checkbox', { name: `Human-reviewed source records record_2` })
    const machineOnly = screen.getByRole('checkbox', { name: `Human-reviewed source records record_3` })
    const inferredUnits = screen.getByRole('checkbox', { name: `Human-reviewed source records record_4` })
    expect(firstReviewed).toBeEnabled()
    expect(machineOnly).toBeDisabled()
    expect(inferredUnits).toBeDisabled()
    expect(screen.getByText('Unit review required')).toBeInTheDocument()

    await user.click(firstReviewed)
    await user.click(secondReviewed)
    await user.type(screen.getByLabelText('Series name'), 'Reviewed DDR survey')
    await user.type(screen.getByLabelText('Reason for this revision'), 'Synthetic review acceptance test')
    apiMock.listSurveys.mockResolvedValue({ items: [createdSurvey.trajectory], total: 1, limit: 100, offset: 0 })
    await user.click(screen.getByRole('button', { name: 'Calculate from selected records' }))

    await waitFor(() => expect(apiMock.importSurvey).toHaveBeenCalledOnce())
    expect(apiMock.listDocuments).toHaveBeenCalledWith(
      {
        well_id: well.id,
        doc_types: ['ddr', 'drilling_document', 'program', 'survey_report', 'other'],
        limit: 500,
      },
      expect.anything(),
    )
    expect(apiMock.importSurvey).toHaveBeenCalledWith(
      well.id,
      expect.objectContaining({
        wellbore_id: wellbore.id,
        source_document_id: sourceDocument.id,
        selected_record_ids: ['record_1', 'record_2'],
        name: 'Reviewed DDR survey',
        kind: 'actual',
        expected_current_version: 0,
        base_trajectory_id: null,
        datum: 'unknown',
        reason: 'Synthetic review acceptance test',
      }),
      expect.any(String),
    )
    expect(apiMock.createSurvey).not.toHaveBeenCalled()
  })
})
