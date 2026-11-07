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
  SurveyTrajectoryResponse,
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
  purpose: 'main' as const,
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

function renderWorkspace() {
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
