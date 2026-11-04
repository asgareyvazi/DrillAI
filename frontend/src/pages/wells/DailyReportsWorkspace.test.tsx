/**
 * DailyReportsWorkspace renders and mutates the versioned API contract rather than inventing a
 * client-side report lifecycle. Server authorization remains a second gate for every write.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type {
  DailyReport,
  DailyReportActivityMetrics,
  DailyReportPeriodIntegrity,
  DailyReportCatalogue,
  DailyReportDetail,
  DailyReportHistory,
  DailyReportRevision,
  Wellbore,
} from '../../api/types'
import { I18nProvider } from '../../i18n'
import DailyReportsWorkspace from './DailyReportsWorkspace'

const mocks = vi.hoisted(() => ({
  gateDisabled: false,
  dailyReportCatalogue: vi.fn(),
  listWellbores: vi.fn(),
  listOperations: vi.fn(),
  listEvents: vi.fn(),
  previewDailyReportActivityClassification: vi.fn(),
  listDailyReports: vi.fn(),
  getDailyReport: vi.fn(),
  getDailyReportActivityMetrics: vi.fn(),
  getDailyReportPeriodIntegrity: vi.fn(),
  getDailyReportHistory: vi.fn(),
  getDailyReportRevision: vi.fn(),
  exportDailyReportRevision: vi.fn(),
  listDocuments: vi.fn(),
  listEvidence: vi.fn(),
  getDocument: vi.fn(),
  createDailyReport: vi.fn(),
  importDailyReport: vi.fn(),
  reconcileDailyReport: vi.fn(),
  updateDailyReportDraft: vi.fn(),
  createDailyReportCorrection: vi.fn(),
  submitDailyReport: vi.fn(),
  approveDailyReport: vi.fn(),
  rejectDailyReport: vi.fn(),
  withdrawDailyReport: vi.fn(),
}))

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    dailyReportCatalogue: mocks.dailyReportCatalogue,
    listWellbores: mocks.listWellbores,
    listOperations: mocks.listOperations,
    listEvents: mocks.listEvents,
    previewDailyReportActivityClassification: mocks.previewDailyReportActivityClassification,
    listDailyReports: mocks.listDailyReports,
    getDailyReport: mocks.getDailyReport,
    getDailyReportActivityMetrics: mocks.getDailyReportActivityMetrics,
    getDailyReportPeriodIntegrity: mocks.getDailyReportPeriodIntegrity,
    getDailyReportHistory: mocks.getDailyReportHistory,
    getDailyReportRevision: mocks.getDailyReportRevision,
    exportDailyReportRevision: mocks.exportDailyReportRevision,
    listDocuments: mocks.listDocuments,
    listEvidence: mocks.listEvidence,
    getDocument: mocks.getDocument,
    createDailyReport: mocks.createDailyReport,
    importDailyReport: mocks.importDailyReport,
    reconcileDailyReport: mocks.reconcileDailyReport,
    updateDailyReportDraft: mocks.updateDailyReportDraft,
    createDailyReportCorrection: mocks.createDailyReportCorrection,
    submitDailyReport: mocks.submitDailyReport,
    approveDailyReport: mocks.approveDailyReport,
    rejectDailyReport: mocks.rejectDailyReport,
    withdrawDailyReport: mocks.withdrawDailyReport,
  },
}))

vi.mock('../../hooks/useActionGate', () => ({
  useActionGate: (actionKey: string) => ({
    state: mocks.gateDisabled ? 'no-permission' : 'ready',
    level: 'write',
    ceiling: 'write',
    suggestDisabled: mocks.gateDisabled,
    reason: mocks.gateDisabled ? `Missing permission for ${actionKey}` : '',
  }),
}))

vi.mock('../../hooks/useIdentity', () => ({
  useIdentity: () => ({ data: { principal_id: 'usr_author' } }),
}))

const catalogue = {
  parameters: [
    { key: 'depth_md', dimension: 'length', canonical_unit: 'm', accepted_units: ['m', 'ft'], required_depth_reference: 'md' },
    { key: 'mud_weight', dimension: 'density', canonical_unit: 'kg/m3', accepted_units: ['kg/m3', 'ppg'], required_depth_reference: null },
  ],
  activity_kinds: ['operation', 'npt', 'event', 'safety', 'milestone', 'other'],
  operation_kinds: ['drilling', 'connection', 'other'],
  npt_categories: ['stuck_pipe', 'well_control', 'unclassified'],
  cause_bases: ['recorded', 'inferred', 'unknown'],
  shift_codes: ['day', 'night', '24h', 'custom'],
  revision_statuses: ['draft', 'submitted', 'approved', 'rejected', 'withdrawn'],
  source_kinds: ['manual', 'import', 'connector'],
  time_bases: ['observed', 'reported', 'calculated', 'planned', 'inferred', 'unknown'],
  depth_references: ['md', 'tvd', 'tvdss', 'unknown'],
  reconciliation_states: ['manual', 'proposed', 'accepted', 'corrected', 'rejected', 'unresolved'],
} satisfies DailyReportCatalogue

const wellbore = {
  id: 'wb_1',
  well_id: 'well_1',
  name: 'Main bore',
  purpose: 'original',
  sequence: 1,
  parent_wellbore_id: null,
  status: 'drilling',
  is_active: true,
  datum: 'rkb',
  kickoff_md_si: null,
  planned_td_md_si: null,
  planned_td_tvd_si: null,
  actual_td_md_si: null,
  actual_td_tvd_si: null,
} satisfies Wellbore

const alternateWellbore = {
  ...wellbore,
  id: 'wb_2',
  name: 'Sidetrack bore',
  purpose: 'sidetrack',
  sequence: 2,
  parent_wellbore_id: 'wb_1',
  is_active: false,
  status: 'planned',
} satisfies Wellbore

const revision = {
  id: 'rev_1',
  report_id: 'report_1',
  revision_number: 1,
  edit_version: 1,
  status: 'draft',
  source_kind: 'manual',
  source_document_id: null,
  source_record_id: null,
  source_region_id: null,
  source_page_number: null,
  period_start_at: null,
  period_end_at: null,
  summary: 'Mud check recorded',
  operations_summary: null,
  safety_notes: null,
  next_period_plan: null,
  environmental_notes: null,
  supersedes_revision_id: null,
  created_by: 'usr_author',
  created_at: '2026-03-15T19:00:00+00:00',
  updated_at: '2026-03-15T19:00:00+00:00',
  submitted_by: null,
  submitted_at: null,
  decided_by: null,
  decided_at: null,
  decision_reason: null,
  content_hash: 'sha256:daily-report-content',
  content_hash_verified: true,
  activities: [],
  measurements: [],
  reconciliation: [],
} satisfies DailyReportRevision

const report = {
  id: 'report_1',
  well_id: 'well_1',
  wellbore_id: 'wb_1',
  report_date: '2026-03-15',
  report_timezone: 'Europe/Amsterdam',
  shift_code: 'day',
  shift_label: null,
  report_number: 'DDR-15',
  identity_key: 'well_1:wb_1:2026-03-15:day',
  latest_revision_number: 1,
  latest_approved_revision_number: null,
  created_by: 'usr_author',
  created_at: '2026-03-15T19:00:00+00:00',
  updated_at: '2026-03-15T19:00:00+00:00',
  latest_revision: revision,
} satisfies DailyReport

const detail = {
  report,
  latest_revision: revision,
  latest_approved_revision: null,
} satisfies DailyReportDetail

const history = {
  report,
  items: [revision],
  total: 1,
  limit: 50,
  offset: 0,
} satisfies DailyReportHistory

const activityMetrics = {
  well_id: 'well_1',
  wellbore_id: 'wb_1',
  report_id: 'report_1',
  report_date: '2026-03-15',
  report_timezone: 'Europe/Amsterdam',
  revision_id: 'rev_1',
  revision_number: 1,
  revision_edit_version: 1,
  revision_status: 'draft',
  revision_source_kind: 'manual',
  revision_content_hash: 'sha256:daily-report-content',
  period_start_at: null,
  period_end_at: null,
  activity_count: 0,
  npt_activity_count: 0,
  npt_duration_included_count: 0,
  npt_duration_excluded_count: 0,
  npt_duration_excluded_by_basis: { planned: 0, inferred: 0, unknown: 0 },
  npt_duration_total: { value: null, unit: 's' },
  npt_duration_coverage: 'not_applicable',
  calculation_method: 'sum_explicit_npt_durations_v1',
  note: 'Synthetic test fixture only.',
} satisfies DailyReportActivityMetrics

const periodIntegrity = {
  well_id: 'well_1',
  wellbore_id: null,
  items: [],
  comparisons: [],
  total: 0,
  limit: 50,
  truncated: false,
  bounded_period_count: 0,
  unassessed_period_count: 0,
  unassessed_comparison_count: 0,
  comparison_method: 'approved_explicit_periods_by_report_date_and_start_v2',
  note: 'Synthetic test fixture only.',
} satisfies DailyReportPeriodIntegrity

const sourceDocumentRow = {
  id: 'doc_ddr_1',
  title: 'Field DDR 15',
  status: 'extracted',
  doc_type: 'ddr',
}
const extractedRecord = {
  id: 'record_1',
  record_type: 'report_header',
  validation_state: 'extracted',
  payload: {
    well_name_text: 'Synthetic West Well',
    report_date: '2026-03-15',
    report_number: 'DDR-007',
    summary: 'Mud check recorded from source.',
    operations_summary: 'Drilled ahead from source.',
    safety_notes: 'No safety incident reported in the source.',
    next_period_plan: 'Continue drilling from source.',
    environmental_notes: 'No environmental issue reported in the source.',
  },
  payload_truncated: false,
  method: 'regex_header',
  confidence: 0.83,
  page_number: 1,
  region_id: 'region_1',
  review_version: 1,
}
const extractedDocument = {
  document: sourceDocumentRow,
  records: [extractedRecord],
  ingestion_jobs: [],
  region_count: 1,
  chunks: [],
  evidence_links: [],
}

function renderWorkspace(path = '/wells/well_1/daily-reports') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/wells/:wellId/daily-reports" element={children} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<DailyReportsWorkspace />, { wrapper: Wrapper })
}

beforeEach(() => {
  window.localStorage.clear()
  mocks.gateDisabled = false
  mocks.dailyReportCatalogue.mockReset().mockResolvedValue(catalogue)
  mocks.listWellbores.mockReset().mockResolvedValue({ items: [wellbore], total: 1, limit: 50, offset: 0 })
  mocks.listOperations.mockReset().mockResolvedValue({ items: [], total: 0, limit: 500, offset: 0 })
  mocks.listEvents.mockReset().mockResolvedValue({ items: [], total: 0, limit: 500, offset: 0 })
  mocks.previewDailyReportActivityClassification.mockReset().mockResolvedValue({
    source_document_id: 'doc_ddr_1',
    source_record_id: 'record_1',
    source_region_id: 'region_1',
    source_page_number: 1,
    source_record_type: 'operation_row',
    source_validation_state: 'extracted',
    source_review_version: 1,
    source_fingerprint: 'sha256:source',
    source_extraction_method: 'rule',
    source_extraction_method_version: '1.2.0',
    method: 'deterministic_content_rules',
    source_text_truncated: false,
    notice: 'Rule matches are review aids, not calibrated accuracy estimates.',
    operation: {
      kind: 'drilling',
      matched_text: 'Drilled ahead',
      matched_pattern: '\\bdrill(ing|ed)?\\s+ahead\\b',
      heuristic_score: 0.8,
    },
    npt: {
      category: null,
      code: null,
      matched_text: null,
      matched_pattern: null,
      heuristic_score: 0,
      is_operator_controllable: null,
    },
  })
  mocks.listDailyReports.mockReset().mockResolvedValue({ items: [report], total: 1, limit: 50, offset: 0 })
  mocks.getDailyReport.mockReset().mockResolvedValue(detail)
  mocks.getDailyReportActivityMetrics.mockReset().mockResolvedValue(activityMetrics)
  mocks.getDailyReportPeriodIntegrity.mockReset().mockResolvedValue(periodIntegrity)
  mocks.getDailyReportHistory.mockReset().mockResolvedValue(history)
  mocks.getDailyReportRevision.mockReset().mockResolvedValue({ report, revision })
  mocks.exportDailyReportRevision.mockReset()
  mocks.listDocuments.mockReset().mockResolvedValue({ items: [sourceDocumentRow], total: 1, limit: 50, offset: 0 })
  mocks.listEvidence.mockReset().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 })
  mocks.getDocument.mockReset().mockResolvedValue(extractedDocument)
  mocks.createDailyReport.mockReset().mockResolvedValue({ report, revision })
  mocks.importDailyReport.mockReset().mockResolvedValue({ report, revision })
  mocks.reconcileDailyReport.mockReset().mockResolvedValue({ report, revision })
  mocks.updateDailyReportDraft.mockReset().mockResolvedValue({ report, revision })
  mocks.createDailyReportCorrection.mockReset().mockResolvedValue({ report, revision })
  mocks.submitDailyReport.mockReset().mockResolvedValue({ report, revision })
  mocks.approveDailyReport.mockReset().mockResolvedValue({ report, revision })
  mocks.rejectDailyReport.mockReset().mockResolvedValue({ report, revision })
  mocks.withdrawDailyReport.mockReset().mockResolvedValue({ report, revision })
})

describe('DailyReportsWorkspace', () => {
  it('creates a manual draft with explicit local scope, timestamp offset and unit provenance', async () => {
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(await screen.findByTestId('daily-report-new'))
    await user.selectOptions(await screen.findByTestId('daily-report-wellbore'), 'wb_1')
    await user.type(screen.getByTestId('daily-report-date'), '2026-03-15')
    await user.type(screen.getByTestId('daily-report-timezone'), 'Europe/Amsterdam')
    await user.selectOptions(screen.getByTestId('daily-report-shift'), 'day')
    await user.type(screen.getByLabelText('Summary'), 'Mud check recorded')
    await user.type(screen.getByTestId('daily-report-period-start'), '2026-03-15T08:00:00')
    await user.click(screen.getByTestId('daily-report-add-measurement'))
    await user.selectOptions(screen.getByTestId('daily-report-measurement-parameter'), 'mud_weight')
    await user.type(screen.getByTestId('daily-report-measurement-value'), '9.2')
    await user.selectOptions(screen.getByTestId('daily-report-measurement-unit'), 'ppg')

    await user.click(screen.getByTestId('daily-report-create-submit'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a timestamp with an explicit UTC offset')
    expect(mocks.createDailyReport).not.toHaveBeenCalled()

    await user.clear(screen.getByTestId('daily-report-period-start'))
    await user.type(screen.getByTestId('daily-report-period-start'), '2026-03-15T08:00:00+01:00')
    await user.click(screen.getByTestId('daily-report-create-submit'))

    await waitFor(() => expect(mocks.createDailyReport).toHaveBeenCalledTimes(1))
    const [wellId, payload, idempotencyKey] = mocks.createDailyReport.mock.calls[0] as [string, Record<string, unknown>, string]
    expect(wellId).toBe('well_1')
    expect(idempotencyKey).toBeTruthy()
    expect(payload).toEqual(expect.objectContaining({
      wellbore_id: 'wb_1',
      report_date: '2026-03-15',
      report_timezone: 'Europe/Amsterdam',
      shift_code: 'day',
      content: expect.objectContaining({
        period_start_at: '2026-03-15T08:00:00+01:00',
        summary: 'Mud check recorded',
        measurements: [expect.objectContaining({
          parameter_key: 'mud_weight',
          value: 9.2,
          unit: 'ppg',
          value_basis: 'reported',
        })],
      }),
    }))
    expect(await screen.findByText('Current revision')).toBeInTheDocument()
    expect(screen.queryByText('Content differs from its stored fingerprint; do not approve it.')).not.toBeInTheDocument()
  })

  it('renders revision-linked NPT duration coverage and keeps missing duration distinct from zero', async () => {
    mocks.getDailyReportActivityMetrics.mockResolvedValue({
      ...activityMetrics,
      activity_count: 3,
      npt_activity_count: 2,
      npt_duration_included_count: 1,
      npt_duration_excluded_count: 1,
      npt_duration_excluded_by_basis: { planned: 1, inferred: 0, unknown: 0 },
      npt_duration_total: { value: 7200, unit: 's' },
      npt_duration_coverage: 'partial',
    })
    renderWorkspace('/wells/well_1/daily-reports?report=report_1')

    const metrics = await screen.findByTestId('daily-report-activity-metrics')
    await screen.findByText('Some NPT durations are excluded')
    expect(metrics).toHaveTextContent('Some NPT durations are excluded')
    expect(screen.getByTestId('daily-report-reported-npt-duration')).toHaveTextContent('2 h')
    expect(metrics).toHaveTextContent('Planned-duration rows excluded')
    expect(metrics).toHaveTextContent('Revision content fingerprint')
    expect(metrics).toHaveTextContent('It does not approve or reconcile any row')
    await waitFor(() => expect(mocks.getDailyReportActivityMetrics).toHaveBeenCalledWith('well_1', 'report_1', 1, expect.any(AbortSignal)))

  })

  it('renders boundary comparisons with provenance and keeps unassessed periods explicit', async () => {
    mocks.getDailyReportPeriodIntegrity.mockResolvedValue({
      ...periodIntegrity,
      items: [
        {
          report_id: 'period_report_1',
          wellbore_id: 'wb_1',
          report_date: '2026-04-01',
          report_timezone: 'UTC',
          shift_code: 'day',
          shift_label: null,
          report_number: 'DDR-1',
          approved_revision_number: 2,
          revision_status: 'approved',
          revision_content_hash: 'sha256:period-one',
          revision_fingerprint_verified: true,
          period_start_at: '2026-04-01T06:00:00Z',
          period_end_at: '2026-04-02T12:00:00Z',
          period_state: 'bounded',
        },
        {
          report_id: 'period_report_2',
          wellbore_id: 'wb_1',
          report_date: '2026-04-02',
          report_timezone: 'UTC',
          shift_code: 'day',
          shift_label: null,
          report_number: 'DDR-2',
          approved_revision_number: 1,
          revision_status: 'approved',
          revision_content_hash: 'sha256:period-two',
          revision_fingerprint_verified: true,
          period_start_at: '2026-04-02T13:00:00Z',
          period_end_at: '2026-04-02T18:00:00Z',
          period_state: 'bounded',
        },
        {
          report_id: 'period_report_3',
          wellbore_id: 'wb_1',
          report_date: '2026-04-02',
          report_timezone: 'UTC',
          shift_code: 'night',
          shift_label: null,
          report_number: 'DDR-3',
          approved_revision_number: 1,
          revision_status: 'approved',
          revision_content_hash: 'sha256:period-three',
          revision_fingerprint_verified: true,
          period_start_at: '2026-04-02T17:30:00Z',
          period_end_at: '2026-04-03T12:00:00Z',
          period_state: 'bounded',
        },
        {
          report_id: 'period_report_4',
          wellbore_id: 'wb_1',
          report_date: '2026-04-04',
          report_timezone: 'UTC',
          shift_code: 'day',
          shift_label: null,
          report_number: 'DDR-4',
          approved_revision_number: null,
          revision_status: null,
          revision_content_hash: null,
          revision_fingerprint_verified: false,
          period_start_at: null,
          period_end_at: null,
          period_state: 'no_approved_revision',
        },
        {
          report_id: 'period_report_5',
          wellbore_id: 'wb_1',
          report_date: '2026-04-05',
          report_timezone: 'UTC',
          shift_code: 'day',
          shift_label: null,
          report_number: 'DDR-5',
          approved_revision_number: 1,
          revision_status: 'approved',
          revision_content_hash: 'sha256:period-five',
          revision_fingerprint_verified: true,
          period_start_at: '2026-04-05T06:00:00Z',
          period_end_at: null,
          period_state: 'missing_end_bound',
        },
      ],
      comparisons: [
        {
          wellbore_id: 'wb_1',
          ordering_basis: 'consecutive_report_dates_with_explicit_start_order_v1',
          previous_report_id: 'period_report_1',
          previous_report_date: '2026-04-01',
          previous_report_number: 'DDR-1',
          previous_revision_number: 2,
          previous_period_end_at: '2026-04-02T12:00:00Z',
          next_report_id: 'period_report_2',
          next_report_date: '2026-04-02',
          next_report_number: 'DDR-2',
          next_revision_number: 1,
          next_period_start_at: '2026-04-02T13:00:00Z',
          relationship: 'gap',
          gap_seconds: 3600,
          overlap_seconds: null,
          unit: 's',
        },
        {
          wellbore_id: 'wb_1',
          ordering_basis: 'explicit_period_start_at_within_report_date_v1',
          previous_report_id: 'period_report_2',
          previous_report_date: '2026-04-02',
          previous_report_number: 'DDR-2',
          previous_revision_number: 1,
          previous_period_end_at: '2026-04-02T18:00:00Z',
          next_report_id: 'period_report_3',
          next_report_date: '2026-04-02',
          next_report_number: 'DDR-3',
          next_revision_number: 1,
          next_period_start_at: '2026-04-02T17:30:00Z',
          relationship: 'overlap',
          gap_seconds: null,
          overlap_seconds: 1800,
          unit: 's',
        },
      ],
      total: 5,
      bounded_period_count: 3,
      unassessed_period_count: 2,
      unassessed_comparison_count: 2,
      note: 'API note is not used as a UI translation.',
    })
    renderWorkspace()

    const panel = await screen.findByTestId('daily-report-period-integrity')
    await screen.findByText('Reported period gap')
    expect(panel).toHaveTextContent('Reported-period boundary review')
    expect(panel).toHaveTextContent('1 h')
    expect(panel).toHaveTextContent('0.5 h')
    expect(panel).toHaveTextContent('Reported period gap')
    expect(panel).toHaveTextContent('Reported period overlap')
    expect(panel).toHaveTextContent('Ordering basis')
    expect(panel).toHaveTextContent('Consecutive local report dates with increasing explicit period-start instants')
    expect(panel).toHaveTextContent('Distinct explicit period-start instants within one local report date')
    expect(panel).toHaveTextContent('Adjacent identity pairs not compared')
    expect(panel).not.toHaveTextContent('API note is not used as a UI translation')
    expect(screen.getByTestId('daily-report-period-comparisons').children).toHaveLength(2)
    await userEvent.setup().click(screen.getByText('Report revisions and period-assessment states'))
    expect(screen.getByTestId('daily-report-period-items')).toHaveTextContent('No approved revision')
    expect(screen.getByTestId('daily-report-period-items')).toHaveTextContent('Period end missing')
    expect(screen.getByTestId('daily-report-period-items')).toHaveTextContent('2026-04-05T06:00:00Z')
    expect(screen.getByTestId('daily-report-period-items')).toHaveTextContent('Verified revision hash')
    await waitFor(() => expect(mocks.getDailyReportPeriodIntegrity).toHaveBeenCalledWith(
      'well_1',
      { report_date_from: undefined, report_date_to: undefined, limit: 50 },
      expect.any(AbortSignal),
    ))
  })

  it('does not render unavailable NPT duration as zero hours', async () => {
    mocks.getDailyReportActivityMetrics.mockResolvedValue({
      ...activityMetrics,
      npt_activity_count: 1,
      npt_duration_excluded_count: 1,
      npt_duration_excluded_by_basis: { planned: 0, inferred: 0, unknown: 1 },
      npt_duration_coverage: 'unavailable',
    })
    renderWorkspace('/wells/well_1/daily-reports?report=report_1')

    const duration = await screen.findByTestId('daily-report-reported-npt-duration')
    expect(duration).toHaveTextContent('No eligible NPT duration; this is not zero hours.')
    expect(duration).not.toHaveTextContent(/0(?:\.00)?\s*h/)
  })

  it('downloads an exact approved revision in the selected locale and shows export failures', async () => {
    const approvedRevision = {
      ...revision,
      status: 'approved',
      submitted_by: 'usr_author',
      decided_by: 'usr_supervisor',
      content_hash_verified: true,
    } satisfies DailyReportRevision
    const approvedReport = {
      ...report,
      latest_revision: approvedRevision,
      latest_approved_revision_number: 1,
    } satisfies DailyReport
    const approvedDetail = {
      report: approvedReport,
      latest_revision: approvedRevision,
      latest_approved_revision: approvedRevision,
    } satisfies DailyReportDetail
    const approvedHistory = {
      report: approvedReport,
      items: [approvedRevision],
      total: 1,
      limit: 50,
      offset: 0,
    } satisfies DailyReportHistory
    const pdf = new Blob(['%PDF-1.4 synthetic revision'], { type: 'application/pdf' })
    mocks.listDailyReports.mockResolvedValue({ items: [approvedReport], total: 1, limit: 50, offset: 0 })
    mocks.getDailyReport.mockResolvedValue(approvedDetail)
    mocks.getDailyReportHistory.mockResolvedValue(approvedHistory)
    mocks.exportDailyReportRevision.mockResolvedValue(pdf)

    const originalCreate = Object.getOwnPropertyDescriptor(URL, 'createObjectURL')
    const originalRevoke = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')
    const createObjectURL = vi.fn().mockReturnValue('blob:daily-report-export')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
    const downloadedNames: string[] = []
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloadedNames.push(this.download)
    })

    try {
      const user = userEvent.setup()
      renderWorkspace('/wells/well_1/daily-reports?report=report_1')
      const exportButton = await screen.findByTestId('daily-report-export-pdf')
      expect(exportButton).toBeEnabled()
      expect(screen.getByText('Downloads exactly revision 1, including its source provenance and review status. Export does not imply additional engineering approval.')).toBeInTheDocument()
      await user.click(exportButton)

      await waitFor(() => expect(mocks.exportDailyReportRevision).toHaveBeenCalledWith('well_1', 'report_1', 1, 'pdf', 'en'))
      expect(await screen.findByText('Revision export downloaded.')).toBeInTheDocument()
      expect(createObjectURL).toHaveBeenCalledWith(pdf)
      expect(downloadedNames).toEqual(['daily-report-revision-1.pdf'])

      mocks.exportDailyReportRevision.mockRejectedValueOnce(new ApiError(
        503,
        'http_503',
        'The export service is temporarily unavailable.',
        {},
        null,
        true,
        { kind: 'server', messageFromServer: true },
      ))
      await user.click(screen.getByTestId('daily-report-export-json'))
      expect(await screen.findByText('The export service is temporarily unavailable.')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Retry' }))
      await waitFor(() => expect(mocks.exportDailyReportRevision).toHaveBeenLastCalledWith('well_1', 'report_1', 1, 'json', 'en'))
    } finally {
      clickSpy.mockRestore()
      if (originalCreate) Object.defineProperty(URL, 'createObjectURL', originalCreate)
      else Reflect.deleteProperty(URL, 'createObjectURL')
      if (originalRevoke) Object.defineProperty(URL, 'revokeObjectURL', originalRevoke)
      else Reflect.deleteProperty(URL, 'revokeObjectURL')
    }
  })

  it('imports a source-linked draft without treating extraction as approval and maps each supported narrative field', async () => {
    const user = userEvent.setup()
    mocks.listEvidence.mockResolvedValue({
      items: [{
        id: 'evidence_record_1',
        subject_kind: 'extracted_record.report_header',
        subject_id: 'record_1',
        evidence_kind: 'document_region',
        evidence_id: 'region_1',
        document_id: 'doc_ddr_1',
        region_id: 'region_1',
        page_number: 1,
        well_id: 'well_1',
        locator: {
          source: { format: 'csv', kind: 'table_row', row: 2 },
          field_locations: { duration_hours: { format: 'csv', row: 2, column: 4 } },
        },
        excerpt: 'Drill ahead, 6.0 h',
        confidence: 0.83,
        relevance: 1,
        weight: 1,
        method: 'table_row',
        quote_verified: true,
        quote_check: 'region_text',
        created_at: '2026-11-01T10:00:00Z',
      }],
      total: 1,
      limit: 50,
      offset: 0,
    })
    renderWorkspace()

    await user.click(await screen.findByTestId('daily-report-import-open'))
    await user.selectOptions(await screen.findByTestId('daily-report-import-document'), 'doc_ddr_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-record'), 'record_1')
    expect(mocks.listDocuments).toHaveBeenCalledWith(
      { well_id: 'well_1', doc_types: ['ddr', 'drilling_document'], limit: 500 },
      expect.anything(),
    )
    const sourceEvidence = await screen.findByTestId('daily-report-import-record-evidence')
    expect(sourceEvidence).toHaveTextContent('Source locator')
    expect(sourceEvidence).toHaveTextContent('field_locations')
    expect(sourceEvidence).toHaveTextContent('table_row')
    await user.selectOptions(await screen.findByTestId('daily-report-import-identity-record'), 'record_1')
    expect(screen.getByTestId('daily-report-import-header-sources')).toHaveTextContent('Synthetic West Well')
    await user.selectOptions(await screen.findByTestId('daily-report-import-identity-source-report-date'), 'payload.report_date')
    await user.selectOptions(await screen.findByTestId('daily-report-import-identity-source-report-number'), 'payload.report_number')
    expect(screen.getByTestId('daily-report-import-date')).toHaveValue('2026-03-15')
    expect(screen.getByTestId('daily-report-import-number')).toHaveValue('DDR-007')
    expect(screen.getByTestId('daily-report-import-wellbore')).toHaveValue('')
    await user.selectOptions(await screen.findByTestId('daily-report-import-summary-source'), 'payload.summary')
    await user.selectOptions(await screen.findByTestId('daily-report-import-operations_summary-source'), 'payload.operations_summary')
    await user.selectOptions(await screen.findByTestId('daily-report-import-safety_notes-source'), 'payload.safety_notes')
    await user.selectOptions(await screen.findByTestId('daily-report-import-next_period_plan-source'), 'payload.next_period_plan')
    await user.selectOptions(await screen.findByTestId('daily-report-import-environmental_notes-source'), 'payload.environmental_notes')
    mocks.listOperations.mockResolvedValue({
      items: [{ id: 'op_1', sequence: 1, name: 'Drilling phase', status: 'in_progress', operation_class: 'actual' }],
      total: 1,
      limit: 500,
      offset: 0,
    })
    mocks.listEvents.mockResolvedValue({
      items: [{ id: 'event_1', title: 'Drilling observation', kind: 'observation', status: 'open', operation_id: 'op_1' }],
      total: 1,
      limit: 500,
      offset: 0,
    })
    await user.selectOptions(await screen.findByTestId('daily-report-import-wellbore'), 'wb_1')
    await user.type(screen.getByTestId('daily-report-import-timezone'), 'Europe/Amsterdam')
    await user.selectOptions(screen.getByTestId('daily-report-import-shift'), 'day')
    await user.click(screen.getByTestId('daily-report-import-add-activity'))
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Activity kind' }), 'operation')
    await user.type(screen.getByLabelText(/title/i), 'Drilled ahead from source')
    await user.selectOptions(await screen.findByTestId('daily-report-import-activity-source-0'), 'record_1')
    await user.click(screen.getByTestId('daily-report-classification-preview-0'))
    expect(await screen.findByTestId('daily-report-classification-result-0')).toHaveTextContent('Drilling')
    expect(mocks.previewDailyReportActivityClassification).toHaveBeenCalledWith(
      'well_1',
      { wellbore_id: 'wb_1', source_record_id: 'record_1', source_document_id: 'doc_ddr_1' },
      expect.anything(),
    )
    await user.click(screen.getByTestId('daily-report-apply-operation-classification-0'))
    await screen.findByRole('option', { name: /Drilling phase/ })
    await user.selectOptions(await screen.findByTestId('daily-report-import-activity-operation-link-0'), 'op_1')
    await screen.findByRole('option', { name: /Drilling observation/ })
    await user.selectOptions(await screen.findByTestId('daily-report-import-activity-event-link-0'), 'event_1')
    await user.click(screen.getByTestId('daily-report-import-add-measurement'))
    await screen.findByTestId('daily-report-import-measurement-row')
    await user.selectOptions(screen.getByTestId('daily-report-import-measurement-0-parameter'), 'mud_weight')
    await user.type(screen.getByTestId('daily-report-import-measurement-0-value'), '9.2')
    await user.selectOptions(screen.getByTestId('daily-report-import-measurement-0-unit'), 'ppg')
    await user.selectOptions(screen.getByTestId('daily-report-import-measurement-0-source'), 'record_1')

    expect(screen.getByLabelText('Summary')).toHaveValue('Mud check recorded from source.')
    expect(screen.getByText('Machine-extracted data remains unvalidated until a person checks it against the cited evidence.')).toBeInTheDocument()
    await user.click(screen.getByTestId('daily-report-import-submit'))

    await waitFor(() => expect(mocks.importDailyReport).toHaveBeenCalledTimes(1))
    const [wellId, payload, idempotencyKey] = mocks.importDailyReport.mock.calls[0] as [string, Record<string, unknown>, string]
    expect(wellId).toBe('well_1')
    expect(idempotencyKey).toBeTruthy()
    expect(payload).toEqual(expect.objectContaining({
      wellbore_id: 'wb_1',
      report_date: '2026-03-15',
      report_timezone: 'Europe/Amsterdam',
      shift_code: 'day',
      report_number: 'DDR-007',
      source_document_id: 'doc_ddr_1',
      source_record_id: 'record_1',
      field_sources: expect.arrayContaining([
        { field_key: 'report_date', source_record_id: 'record_1', source_path: 'payload.report_date' },
        { field_key: 'report_number', source_record_id: 'record_1', source_path: 'payload.report_number' },
        { field_key: 'summary', source_record_id: 'record_1', source_path: 'payload.summary' },
        { field_key: 'operations_summary', source_record_id: 'record_1', source_path: 'payload.operations_summary' },
        { field_key: 'safety_notes', source_record_id: 'record_1', source_path: 'payload.safety_notes' },
        { field_key: 'next_period_plan', source_record_id: 'record_1', source_path: 'payload.next_period_plan' },
        { field_key: 'environmental_notes', source_record_id: 'record_1', source_path: 'payload.environmental_notes' },
      ]),
      content: expect.objectContaining({
        summary: 'Mud check recorded from source.',
        operations_summary: 'Drilled ahead from source.',
        safety_notes: 'No safety incident reported in the source.',
        next_period_plan: 'Continue drilling from source.',
        environmental_notes: 'No environmental issue reported in the source.',
        source_document_id: 'doc_ddr_1',
        source_record_id: 'record_1',
        activities: [expect.objectContaining({
          activity_kind: 'operation',
          operation_kind: 'drilling',
          title: 'Drilled ahead from source',
          source_document_id: 'doc_ddr_1',
          source_record_id: 'record_1',
          operation_id: 'op_1',
          event_id: 'event_1',
        })],
        measurements: [expect.objectContaining({
          parameter_key: 'mud_weight',
          value: 9.2,
          unit: 'ppg',
          source_document_id: 'doc_ddr_1',
          source_record_id: 'record_1',
          source_region_id: 'region_1',
          source_page_number: 1,
        })],
      }),
    }))
  })

  it('opens the existing report on duplicate import identity instead of creating or overwriting a report', async () => {
    const user = userEvent.setup()
    mocks.importDailyReport.mockRejectedValue(new ApiError(
      409,
      'daily_report.identity_conflict',
      'A report with this identity already exists.',
      { report_id: 'report_1' },
      null,
      false,
      { kind: 'conflict', messageFromServer: true },
    ))
    renderWorkspace()

    await user.click(await screen.findByTestId('daily-report-import-open'))
    await user.selectOptions(await screen.findByTestId('daily-report-import-document'), 'doc_ddr_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-identity-record'), 'record_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-identity-source-report-date'), 'payload.report_date')
    await user.selectOptions(await screen.findByTestId('daily-report-import-identity-source-report-number'), 'payload.report_number')
    await user.selectOptions(await screen.findByTestId('daily-report-import-record'), 'record_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-summary-source'), 'payload.summary')
    await user.selectOptions(await screen.findByTestId('daily-report-import-wellbore'), 'wb_1')
    await user.type(screen.getByTestId('daily-report-import-timezone'), 'Europe/Amsterdam')
    await user.selectOptions(screen.getByTestId('daily-report-import-shift'), 'day')
    await user.click(screen.getByTestId('daily-report-import-submit'))

    const openExisting = await screen.findByTestId('daily-report-import-open-existing')
    expect(screen.getByText('The existing report was not overwritten and no duplicate was created. Open it to review its current revision or start a reasoned correction.')).toBeInTheDocument()
    await user.click(openExisting)
    expect(await screen.findByTestId('daily-report-detail')).toBeInTheDocument()
    await waitFor(() => expect(mocks.getDailyReport).toHaveBeenCalledWith('well_1', 'report_1', expect.any(AbortSignal)))
    expect(mocks.importDailyReport).toHaveBeenCalledTimes(1)
    expect(mocks.createDailyReport).not.toHaveBeenCalled()
  })

  it('shows cited extraction evidence and submits a reasoned, versioned reconciliation decision', async () => {
    const user = userEvent.setup()
    const proposal = {
      id: 'reconciliation_1',
      target_key: 'field:summary',
      target_kind: 'revision_field',
      target_id: null,
      field_key: 'summary',
      source_document_id: 'doc_ddr_1',
      source_record_id: 'record_1',
      source_region_id: 'region_1',
      source_page_number: 1,
      source_path: 'payload.summary',
      source_review_version: 1,
      source_fingerprint: 'sha256:source',
      source_value_hash: 'sha256:source-value',
      proposed_value_hash: 'sha256:proposal',
      proposal_matches_current: true,
      resolution_state: 'proposed',
      reason: null,
      decided_by: null,
      decided_at: null,
    } as const
    const identityProposal = {
      ...proposal,
      id: 'reconciliation_identity_date',
      target_key: 'identity:report_date',
      target_kind: 'report_identity',
      field_key: 'report_date',
      source_path: 'payload.report_date',
    } as const
    const importedRevision = {
      ...revision,
      source_kind: 'import',
      source_document_id: 'doc_ddr_1',
      source_record_id: 'record_1',
      reconciliation: [proposal, identityProposal],
    } satisfies DailyReportRevision
    const importedReport = { ...report, latest_revision: importedRevision } satisfies DailyReport
    const importedDetail = {
      report: importedReport,
      latest_revision: importedRevision,
      latest_approved_revision: null,
    } satisfies DailyReportDetail
    const importedHistory = {
      report: importedReport,
      items: [importedRevision],
      total: 1,
      limit: 50,
      offset: 0,
    } satisfies DailyReportHistory
    mocks.listDailyReports.mockResolvedValue({ items: [importedReport], total: 1, limit: 50, offset: 0 })
    mocks.getDailyReport.mockResolvedValue(importedDetail)
    mocks.getDailyReportHistory.mockResolvedValue(importedHistory)
    mocks.reconcileDailyReport.mockResolvedValue({ report: importedReport, revision: importedRevision })

    renderWorkspace('/wells/well_1/daily-reports?report=report_1')
    await screen.findByTestId('daily-report-reconciliation')
    expect(screen.getByTestId('daily-report-submit')).toBeDisabled()
    expect(screen.getByText('Resolve every imported or unresolved source proposal before submitting this report for review.')).toBeInTheDocument()
    await user.click((await screen.findAllByText('Inspect cited extraction record and payload'))[0]!)
    expect(await screen.findByText('Mud check recorded from source.')).toBeInTheDocument()
    const decision = screen.getByRole('combobox', { name: 'Decision for summary' })
    expect(within(decision).getByRole('option', { name: 'Mark corrected' })).toBeDisabled()
    expect(within(decision).getByRole('option', { name: 'Reject proposal' })).toBeDisabled()
    await user.selectOptions(decision, 'accepted')
    const identityDecision = screen.getByRole('combobox', { name: 'Decision for report_date' })
    expect(within(identityDecision).getByRole('option', { name: 'Mark corrected' })).toBeDisabled()
    expect(screen.getByText('Report date and number are fixed when the report is created. Reject a conflicting source proposal; a different identity requires a separate report.')).toBeInTheDocument()
    await user.selectOptions(identityDecision, 'accepted')
    await user.type(screen.getByTestId('daily-report-reconciliation-reason'), 'Compared the proposal and immutable report identity with cited evidence.')
    await user.click(screen.getByTestId('daily-report-reconciliation-submit'))

    await waitFor(() => expect(mocks.reconcileDailyReport).toHaveBeenCalledTimes(1))
    const [wellId, reportId, revisionNumber, payload, idempotencyKey] = mocks.reconcileDailyReport.mock.calls[0] as [string, string, number, Record<string, unknown>, string]
    expect([wellId, reportId, revisionNumber]).toEqual(['well_1', 'report_1', 1])
    expect(payload).toEqual({
      expected_edit_version: 1,
      reason: 'Compared the proposal and immutable report identity with cited evidence.',
      decisions: [
        { target_key: 'field:summary', decision: 'accepted' },
        { target_key: 'identity:report_date', decision: 'accepted' },
      ],
    })
    expect(idempotencyKey).toBeTruthy()
  })

  it('sends exact report-number, wellbore, status, shift, date and page filters and rejects reversed dates locally', async () => {
    const user = userEvent.setup()
    mocks.listWellbores.mockResolvedValue({ items: [wellbore, alternateWellbore], total: 2, limit: 50, offset: 0 })
    mocks.listDailyReports.mockResolvedValue({ items: [report], total: 60, limit: 50, offset: 0 })
    renderWorkspace()
    await screen.findByText('DDR-15')
    mocks.listDailyReports.mockClear()

    await user.selectOptions(screen.getByLabelText('Filter by wellbore'), 'wb_2')
    await user.selectOptions(screen.getByLabelText('Filter by latest revision status'), 'draft')
    await user.selectOptions(screen.getByLabelText('Filter by shift'), 'day')
    await user.type(screen.getByLabelText('Report date from'), '2026-03-01')
    await user.type(screen.getByLabelText('Report date to'), '2026-03-31')
    await user.type(screen.getByLabelText('Exact report number'), ' DDR-15 ')

    await waitFor(() => {
      expect(mocks.listDailyReports).toHaveBeenLastCalledWith(
        'well_1',
        expect.objectContaining({
          status: 'draft',
          shift_code: 'day',
          report_date_from: '2026-03-01',
          report_date_to: '2026-03-31',
          report_number: 'DDR-15',
          wellbore_id: 'wb_2',
          limit: 50,
          offset: 0,
        }),
        expect.any(AbortSignal),
      )
    })
    await user.click(screen.getByRole('button', { name: 'Next reports page' }))
    await waitFor(() => expect(mocks.listDailyReports).toHaveBeenLastCalledWith(
      'well_1',
      expect.objectContaining({ offset: 50, status: 'draft', shift_code: 'day', report_number: 'DDR-15', wellbore_id: 'wb_2' }),
      expect.any(AbortSignal),
    ))

    await user.selectOptions(screen.getByLabelText('Filter by wellbore'), 'wb_1')
    await waitFor(() => expect(mocks.listDailyReports).toHaveBeenLastCalledWith(
      'well_1',
      expect.objectContaining({ offset: 0, wellbore_id: 'wb_1' }),
      expect.any(AbortSignal),
    ))

    await user.clear(screen.getByLabelText('Exact report number'))
    await user.type(screen.getByLabelText('Exact report number'), ' DDR-16 ')
    await waitFor(() => expect(mocks.listDailyReports).toHaveBeenLastCalledWith(
      'well_1',
      expect.objectContaining({ offset: 0, report_number: 'DDR-16', wellbore_id: 'wb_1' }),
      expect.any(AbortSignal),
    ))

    await user.clear(screen.getByLabelText('Report date from'))
    await user.type(screen.getByLabelText('Report date from'), '2026-04-01')
    await expect(screen.findByRole('alert')).resolves.toHaveTextContent('The start date must not be after the end date.')
    expect(mocks.listDailyReports.mock.calls.at(-1)?.[1]).not.toEqual(expect.objectContaining({ report_date_from: '2026-04-01', report_date_to: '2026-03-31' }))
  })

  it('withholds creation when the server catalogue says the current role lacks permission', async () => {
    mocks.gateDisabled = true
    renderWorkspace()

    const createButton = await screen.findByTestId('daily-report-new')
    expect(createButton).toBeDisabled()
    expect(screen.getByTestId('daily-report-create-gate')).toHaveTextContent('Missing permission for daily_report.create')
    expect(mocks.createDailyReport).not.toHaveBeenCalled()
  })

  it('renders Persian copy and RTL direction while keeping operational identifiers readable', async () => {
    window.localStorage.setItem('drillai.locale', 'fa')
    renderWorkspace('/wells/well_1/daily-reports?report=report_1')

    expect(await screen.findByRole('heading', { level: 1, name: 'گزارش‌های روزانه حفاری' })).toBeInTheDocument()
    await waitFor(() => expect(document.documentElement.dir).toBe('rtl'))
    expect(screen.getByText('Europe/Amsterdam')).toHaveAttribute('dir', 'ltr')
  })
})
