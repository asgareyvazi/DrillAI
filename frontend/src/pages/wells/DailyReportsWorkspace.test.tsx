/**
 * DailyReportsWorkspace renders and mutates the versioned API contract rather than inventing a
 * client-side report lifecycle. Server authorization remains a second gate for every write.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type {
  DailyReport,
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
  listDailyReports: vi.fn(),
  getDailyReport: vi.fn(),
  getDailyReportHistory: vi.fn(),
  getDailyReportRevision: vi.fn(),
  exportDailyReportRevision: vi.fn(),
  listDocuments: vi.fn(),
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
    listDailyReports: mocks.listDailyReports,
    getDailyReport: mocks.getDailyReport,
    getDailyReportHistory: mocks.getDailyReportHistory,
    getDailyReportRevision: mocks.getDailyReportRevision,
    exportDailyReportRevision: mocks.exportDailyReportRevision,
    listDocuments: mocks.listDocuments,
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
  mocks.listDailyReports.mockReset().mockResolvedValue({ items: [report], total: 1, limit: 50, offset: 0 })
  mocks.getDailyReport.mockReset().mockResolvedValue(detail)
  mocks.getDailyReportHistory.mockReset().mockResolvedValue(history)
  mocks.getDailyReportRevision.mockReset().mockResolvedValue({ report, revision })
  mocks.exportDailyReportRevision.mockReset()
  mocks.listDocuments.mockReset().mockResolvedValue({ items: [sourceDocumentRow], total: 1, limit: 50, offset: 0 })
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
    renderWorkspace()

    await user.click(await screen.findByTestId('daily-report-import-open'))
    await user.selectOptions(await screen.findByTestId('daily-report-import-document'), 'doc_ddr_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-record'), 'record_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-summary-source'), 'payload.summary')
    await user.selectOptions(await screen.findByTestId('daily-report-import-operations_summary-source'), 'payload.operations_summary')
    await user.selectOptions(await screen.findByTestId('daily-report-import-safety_notes-source'), 'payload.safety_notes')
    await user.selectOptions(await screen.findByTestId('daily-report-import-next_period_plan-source'), 'payload.next_period_plan')
    await user.selectOptions(await screen.findByTestId('daily-report-import-environmental_notes-source'), 'payload.environmental_notes')
    await user.selectOptions(await screen.findByTestId('daily-report-import-wellbore'), 'wb_1')
    await user.type(screen.getByTestId('daily-report-import-date'), '2026-03-15')
    await user.type(screen.getByTestId('daily-report-import-timezone'), 'Europe/Amsterdam')
    await user.selectOptions(screen.getByTestId('daily-report-import-shift'), 'day')
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
      source_document_id: 'doc_ddr_1',
      source_record_id: 'record_1',
      field_sources: [
        { field_key: 'summary', source_record_id: 'record_1', source_path: 'payload.summary' },
        { field_key: 'operations_summary', source_record_id: 'record_1', source_path: 'payload.operations_summary' },
        { field_key: 'safety_notes', source_record_id: 'record_1', source_path: 'payload.safety_notes' },
        { field_key: 'next_period_plan', source_record_id: 'record_1', source_path: 'payload.next_period_plan' },
        { field_key: 'environmental_notes', source_record_id: 'record_1', source_path: 'payload.environmental_notes' },
      ],
      content: expect.objectContaining({
        summary: 'Mud check recorded from source.',
        operations_summary: 'Drilled ahead from source.',
        safety_notes: 'No safety incident reported in the source.',
        next_period_plan: 'Continue drilling from source.',
        environmental_notes: 'No environmental issue reported in the source.',
        source_document_id: 'doc_ddr_1',
        source_record_id: 'record_1',
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
    await user.selectOptions(await screen.findByTestId('daily-report-import-record'), 'record_1')
    await user.selectOptions(await screen.findByTestId('daily-report-import-summary-source'), 'payload.summary')
    await user.selectOptions(await screen.findByTestId('daily-report-import-wellbore'), 'wb_1')
    await user.type(screen.getByTestId('daily-report-import-date'), '2026-03-15')
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
    const importedRevision = {
      ...revision,
      source_kind: 'import',
      source_document_id: 'doc_ddr_1',
      source_record_id: 'record_1',
      reconciliation: [proposal],
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
    await user.click(await screen.findByText('Inspect cited extraction record and payload'))
    expect(await screen.findByText('Mud check recorded from source.')).toBeInTheDocument()
    const decision = screen.getByRole('combobox', { name: 'Decision for summary' })
    expect(screen.getByRole('option', { name: 'Mark corrected' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'Reject proposal' })).toBeDisabled()
    await user.selectOptions(decision, 'accepted')
    await user.type(screen.getByTestId('daily-report-reconciliation-reason'), 'Compared with the cited source record.')
    await user.click(screen.getByTestId('daily-report-reconciliation-submit'))

    await waitFor(() => expect(mocks.reconcileDailyReport).toHaveBeenCalledTimes(1))
    const [wellId, reportId, revisionNumber, payload, idempotencyKey] = mocks.reconcileDailyReport.mock.calls[0] as [string, string, number, Record<string, unknown>, string]
    expect([wellId, reportId, revisionNumber]).toEqual(['well_1', 'report_1', 1])
    expect(payload).toEqual({
      expected_edit_version: 1,
      reason: 'Compared with the cited source record.',
      decisions: [{ target_key: 'field:summary', decision: 'accepted' }],
    })
    expect(idempotencyKey).toBeTruthy()
  })

  it('sends status, shift, date and page filters to the server and rejects a reversed date range locally', async () => {
    const user = userEvent.setup()
    mocks.listDailyReports.mockResolvedValue({ items: [report], total: 60, limit: 50, offset: 0 })
    renderWorkspace()
    await screen.findByText('DDR-15')
    mocks.listDailyReports.mockClear()

    await user.selectOptions(screen.getByLabelText('Filter by latest revision status'), 'draft')
    await user.selectOptions(screen.getByLabelText('Filter by shift'), 'day')
    await user.type(screen.getByLabelText('Report date from'), '2026-03-01')
    await user.type(screen.getByLabelText('Report date to'), '2026-03-31')

    await waitFor(() => {
      expect(mocks.listDailyReports).toHaveBeenLastCalledWith(
        'well_1',
        expect.objectContaining({
          status: 'draft',
          shift_code: 'day',
          report_date_from: '2026-03-01',
          report_date_to: '2026-03-31',
          limit: 50,
          offset: 0,
        }),
        expect.any(AbortSignal),
      )
    })
    await user.click(screen.getByRole('button', { name: 'Next reports page' }))
    await waitFor(() => expect(mocks.listDailyReports).toHaveBeenLastCalledWith(
      'well_1',
      expect.objectContaining({ offset: 50, status: 'draft', shift_code: 'day' }),
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
