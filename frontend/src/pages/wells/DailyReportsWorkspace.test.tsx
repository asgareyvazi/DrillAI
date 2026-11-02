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
  createDailyReport: vi.fn(),
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
    createDailyReport: mocks.createDailyReport,
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
  mocks.createDailyReport.mockReset().mockResolvedValue({ report, revision })
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
