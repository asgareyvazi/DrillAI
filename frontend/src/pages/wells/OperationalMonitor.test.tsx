import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type {
  AlertEvidence,
  AlertRow,
  AlertsPage,
  TelemetryLatestReading,
  TelemetryWindowResponse,
  WellLatestTelemetryResponse,
} from '../../api/types'
import { I18nProvider } from '../../i18n'
import { OperationalMonitor } from './OperationalMonitor'

const WELL_ID = 'aaaaaaaa-1111-2222-3333-444444444444'

const mockWellLatestTelemetry = vi.fn<() => Promise<WellLatestTelemetryResponse>>()
const mockGetTimeseriesPoints = vi.fn<() => Promise<TelemetryWindowResponse>>()
const mockListAlerts = vi.fn<() => Promise<AlertsPage>>()
const mockAcknowledgeAlert = vi.fn<(...args: unknown[]) => Promise<AlertRow>>()
const mockClearAlert = vi.fn<(...args: unknown[]) => Promise<AlertRow>>()
const mockCancelAlert = vi.fn<(...args: unknown[]) => Promise<AlertRow>>()
const mockAlertEvidence = vi.fn<() => Promise<AlertEvidence>>()
const mockEvaluateWellAlerts = vi.fn()
const mockCommissionSyntheticTelemetry = vi.fn()

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    wellLatestTelemetry: (...args: unknown[]) => mockWellLatestTelemetry(...(args as [])),
    getTimeseriesPoints: (...args: unknown[]) => mockGetTimeseriesPoints(...(args as [])),
    listAlerts: (...args: unknown[]) => mockListAlerts(...(args as [])),
    acknowledgeAlert: (...args: unknown[]) => mockAcknowledgeAlert(...args),
    clearAlert: (...args: unknown[]) => mockClearAlert(...args),
    cancelAlert: (...args: unknown[]) => mockCancelAlert(...args),
    alertEvidence: (...args: unknown[]) => mockAlertEvidence(...(args as [])),
    evaluateWellAlerts: (...args: unknown[]) => mockEvaluateWellAlerts(...args),
    commissionSyntheticTelemetry: (...args: unknown[]) => mockCommissionSyntheticTelemetry(...args),
  },
}))

const mockUseWellLiveStream = vi.fn()
vi.mock('../../hooks/useWellLiveStream', () => ({
  useWellLiveStream: (...args: unknown[]) => mockUseWellLiveStream(...args),
}))

const sampleReadings: TelemetryLatestReading[] = [
  {
    channel_id: 'ch-spp',
    channel_key: 'spp',
    label: 'Standpipe Pressure',
    dimension: 'pressure',
    unit: 'Pa',
    value: 28957980,
    quality: 'good',
    quality_flags: [],
    is_trustworthy: true,
    observed_at: '2026-10-10T10:00:10Z',
    received_at: '2026-10-10T10:00:10Z',
    age_seconds: 4,
    freshness: 'live',
    source: 'witsml',
    source_ref: null,
    is_late: false,
  },
  {
    channel_id: 'ch-wob',
    channel_key: 'wob',
    label: 'Weight on Bit',
    dimension: 'force',
    unit: 'N',
    value: null,
    quality: 'missing',
    quality_flags: [],
    is_trustworthy: false,
    observed_at: null,
    received_at: null,
    age_seconds: null,
    freshness: 'missing',
    source: 'witsml',
    source_ref: null,
    is_late: false,
  },
  {
    channel_id: 'ch-torque',
    channel_key: 'torque',
    label: 'Surface Torque',
    dimension: 'torque',
    unit: 'N.m',
    value: 999999,
    quality: 'bad',
    quality_flags: [],
    is_trustworthy: false,
    observed_at: '2026-10-10T10:00:08Z',
    received_at: '2026-10-10T10:00:08Z',
    age_seconds: 6,
    freshness: 'live',
    source: 'sensor',
    source_ref: null,
    is_late: false,
  },
]

const sampleWindow: TelemetryWindowResponse = {
  channel: {
    id: 'ch-spp',
    well_id: WELL_ID,
    wellbore_id: null,
    operation_id: null,
    channel_key: 'spp',
    name: 'Standpipe Pressure',
    description: null,
    dimension: 'pressure',
    unit: 'Pa',
    src_unit: 'psi',
    is_realtime: true,
    source: 'witsml',
    source_ref: null,
    sampling_hint_seconds: 5,
    point_count: 2,
    first_ts: '2026-10-10T10:00:05Z',
    last_ts: '2026-10-10T10:00:10Z',
    created_at: '2026-10-10T09:00:00Z',
    updated_at: '2026-10-10T10:00:10Z',
  },
  window: { start: null, end: null, max_points: 120 },
  total_in_window: 2,
  count: 2,
  truncated: false,
  downsampled: true,
  items: [
    {
      id: 'pt-0',
      series_id: 'ch-spp',
      ts: '2026-10-10T10:00:05Z',
      received_at: '2026-10-10T10:00:05Z',
      value: 27500000,
      src_value: 3988,
      src_unit: 'psi',
      quality: 'good',
      is_late: false,
      is_out_of_order: false,
      source_point_id: 'sp-0',
      source_ref: null,
      sequence: 1,
      depth_md_si: null,
      revisions: 0,
    },
    {
      id: 'pt-1',
      series_id: 'ch-spp',
      ts: '2026-10-10T10:00:10Z',
      received_at: '2026-10-10T10:00:11Z',
      value: 28957980,
      src_value: 4200,
      src_unit: 'psi',
      quality: 'good',
      is_late: true,
      is_out_of_order: false,
      source_point_id: 'sp-1',
      source_ref: null,
      sequence: 2,
      depth_md_si: null,
      revisions: 0,
    },
  ],
}

const raisedAlert: AlertRow = {
  id: 'alert-raised-1',
  kind: 'threshold_breach',
  severity: 'high',
  status: 'raised',
  title: 'Standpipe pressure high: 4200 psi > 4000 psi',
  description: 'Rule standpipe_pressure_high breached on channel spp.',
  action_level: 'L1',
  well_id: WELL_ID,
  wellbore_id: null,
  operation_id: null,
  section_id: null,
  subject: { kind: 'timeseries_point', id: 'pt-1' },
  series_id: 'ch-spp',
  channel_key: 'spp',
  source_point_id: 'pt-1',
  rule_id: 'rule-spp-high',
  rule_ref: 'standpipe_pressure_high',
  observed: {
    value: 28957980,
    threshold: 27579029,
    unit: 'Pa',
    declared_threshold: 4000,
    declared_unit: 'psi',
    timestamp: '2026-10-10T10:00:10Z',
    sustained_seconds: 0,
    clear_value: null,
  },
  raised_at: '2026-10-10T10:00:10Z',
  raised_by: 'rule:standpipe_pressure_high',
  acknowledged_at: null,
  acknowledged_by: null,
  cleared_at: null,
  cancelled_reason: null,
  reason: null,
  allowed_transitions: ['acknowledged', 'cleared', 'cancelled'],
  version: '2026-10-10T10:00:10.000000+00:00',
  transitioned_at: '2026-10-10T10:00:10Z',
  evidence_url: '/api/v1/alerts/alert-raised-1/evidence',
  created_at: '2026-10-10T10:00:10Z',
  updated_at: '2026-10-10T10:00:10.000000+00:00',
}

const clearedAlert: AlertRow = {
  ...raisedAlert,
  id: 'alert-cleared-2',
  status: 'cleared',
  allowed_transitions: [],
  title: 'Hookload spike cleared',
  cleared_at: '2026-10-10T10:02:00Z',
  reason: 'Returned below hysteresis line',
}

const sampleEvidence: AlertEvidence = {
  alert: raisedAlert,
  rule: {
    id: 'rule-spp-high',
    rule_key: 'standpipe_pressure_high',
    name: 'Standpipe pressure high',
    description: 'High SPP threshold',
    channel_key: 'spp',
    well_id: WELL_ID,
    wellbore_id: null,
    operation_id: null,
    operator: 'gt',
    threshold: 27579029,
    unit: 'Pa',
    clear_operator: 'lte',
    clear_threshold: 26200077,
    clear_is_explicit: false,
    sustain_seconds: 0,
    clear_sustain_seconds: 0,
    cooldown_seconds: 60,
    severity: 'high',
    enabled: true,
    created_at: '2026-10-10T09:00:00Z',
    updated_at: '2026-10-10T09:00:00Z',
  },
  rule_snapshot: null,
  observed: {
    value: 28957980,
    threshold: 27579029,
    clear_observed_value: null,
    unit: 'Pa',
    observed_at: '2026-10-10T10:00:10Z',
    sustained_seconds: 0,
    declared_threshold: 4000,
    declared_unit: 'psi',
  },
  points: [
    {
      id: 'pt-1',
      ts: '2026-10-10T10:00:10Z',
      value: 28957980,
      quality: 'good',
      quality_flags: ['late'],
      is_late: true,
      is_out_of_order: false,
    },
  ],
  timeline: [
    {
      state: 'raised',
      at: '2026-10-10T10:00:10Z',
      by: 'rule:standpipe_pressure_high',
      reason: 'Standpipe pressure high breached',
    },
  ],
  audit_events: [
    {
      id: 'aud-1',
      action: 'alert.raised',
      actor_id: null,
      actor_kind: 'system',
      reason: 'Automatic rule evaluation',
      occurred_at: '2026-10-10T10:00:10Z',
      before: {},
      after: { status: 'raised' },
    },
  ],
  provenance: {
    series_id: 'ch-spp',
    channel_key: 'spp',
    dimension: 'pressure',
    point_id: 'pt-1',
    rule_id: 'rule-spp-high',
    rule_ref: 'standpipe_pressure_high',
    well_id: WELL_ID,
    wellbore_id: null,
    section_id: null,
    operation_id: null,
  },
}

function renderMonitor(initialUrl = `/wells/${WELL_ID}`, locale: 'en' | 'fa' = 'en') {
  if (locale === 'fa') {
    window.localStorage.setItem('drillai.locale', 'fa')
  }
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <I18nProvider>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialUrl]}>
          <Routes>
            <Route path="/wells/:wellId" element={<OperationalMonitor wellId={WELL_ID} />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </I18nProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  mockWellLatestTelemetry.mockResolvedValue({
    well_id: WELL_ID,
    generated_at: '2026-10-10T10:00:14Z',
    total: sampleReadings.length,
    fresh_seconds: 15,
    stale_seconds: 30,
    items: sampleReadings,
  })
  mockGetTimeseriesPoints.mockResolvedValue(sampleWindow)
  mockListAlerts.mockResolvedValue({
    items: [raisedAlert, clearedAlert],
    total: 2,
    limit: 100,
    offset: 0,
    statuses: ['raised', 'acknowledged', 'cleared', 'cancelled'],
    severities: ['low', 'medium', 'high', 'critical'],
  })
  mockAlertEvidence.mockResolvedValue(sampleEvidence)
  mockUseWellLiveStream.mockReturnValue({
    transportState: 'LIVE',
    status: {
      state: 'live',
      wellId: WELL_ID,
      afterSeq: 12,
      cursor: 'cur-12',
      attempts: 0,
      closeCode: null,
      lastError: null,
    },
    snapshot: {
      well_id: WELL_ID,
      generated_at: '2026-10-10T10:00:14Z',
      telemetry_as_of: '2026-10-10T10:00:10Z',
      operation_as_of: '2026-10-10T09:30:00Z',
      alerts_as_of: '2026-10-10T10:00:10Z',
      stream_position: 12,
      cursor: 'cur-12',
      latest: sampleReadings,
      freshness: { live: 2, stale: 0, missing: 1 },
      trends: {
        spp: {
          channel_id: 'ch-spp',
          channel_key: 'spp',
          unit: 'Pa',
          direction: 'insufficient_data',
          delta: null,
          rate_per_minute: null,
          samples: 2,
          window_seconds: 10,
          first_ts: '2026-10-10T10:00:05Z',
          last_ts: '2026-10-10T10:00:10Z',
          first_value: 27500000,
          last_value: 28957980,
          min_value: 27500000,
          max_value: 28957980,
        },
      },
      alerts: [raisedAlert],
    },
    trends: {
      spp: {
        channel_id: 'ch-spp',
        channel_key: 'spp',
        unit: 'Pa',
        direction: 'insufficient_data',
        delta: null,
        rate_per_minute: null,
        samples: 2,
        window_seconds: 10,
        first_ts: '2026-10-10T10:00:05Z',
        last_ts: '2026-10-10T10:00:10Z',
        first_value: 27500000,
        last_value: 28957980,
        min_value: 27500000,
        max_value: 28957980,
      },
    },
    recentEvents: [],
    diagnostics: {
      receivedFrames: 3,
      coalescedTelemetryFrames: 1,
      omittedTelemetryEvents: 0,
      lastGap: null,
      ignored: [],
    },
    isPollingFallback: false,
    isResyncing: false,
    resyncNow: vi.fn().mockResolvedValue(undefined),
  })
})

describe('OperationalMonitor', () => {
  it('renders KPI strip without fabricating 0 for missing channels, flags untrustworthy quality, and never labels insufficient_data trends as flat', async () => {
    renderMonitor()

    expect(await screen.findByTestId('live-transport-state')).toHaveTextContent('LIVE')
    expect(await screen.findByTestId('kpi-value-spp')).toBeInTheDocument()

    // Missing WOB channel must show "No reading recorded", never 0
    const missingWob = screen.getByTestId('kpi-missing-wob')
    expect(missingWob).toHaveTextContent('No reading recorded')
    expect(missingWob).not.toHaveTextContent(/^0/)

    // Untrustworthy torque channel (`quality: 'bad'`) must be flagged explicitly
    const untrustworthyTorque = screen.getByTestId('kpi-untrustworthy-torque')
    expect(untrustworthyTorque).toHaveTextContent(/Untrustworthy quality \(bad\)/i)

    // 2-sample trend must render "Insufficient samples for trend", never "Flat"
    expect(screen.getByTestId('kpi-trend-insufficient')).toHaveTextContent(
      'Insufficient samples for trend',
    )
    expect(screen.queryByText('Flat')).not.toBeInTheDocument()
  })

  it('honors server allowed_transitions, requires a reason for Clear/Cancel, and handles 409 version conflict', async () => {
    const user = userEvent.setup()
    renderMonitor()

    const raisedRow = await screen.findByTestId(`alert-row-${raisedAlert.id}`)
    const clearedRow = await screen.findByTestId(`alert-row-${clearedAlert.id}`)

    // Raised alert has Acknowledge, Clear, Cancel from `allowed_transitions`
    expect(within(raisedRow).getByTestId(`alert-ack-btn-${raisedAlert.id}`)).toBeInTheDocument()
    expect(within(raisedRow).getByTestId(`alert-clear-btn-${raisedAlert.id}`)).toBeInTheDocument()
    expect(within(raisedRow).getByTestId(`alert-cancel-btn-${raisedAlert.id}`)).toBeInTheDocument()

    // Terminal cleared alert has no transition buttons
    expect(within(clearedRow).queryByTestId(`alert-ack-btn-${clearedAlert.id}`)).not.toBeInTheDocument()
    expect(within(clearedRow).queryByTestId(`alert-clear-btn-${clearedAlert.id}`)).not.toBeInTheDocument()
    expect(within(clearedRow).queryByTestId(`alert-cancel-btn-${clearedAlert.id}`)).not.toBeInTheDocument()

    // Clicking Clear requires a non-empty reason
    await user.click(within(raisedRow).getByTestId(`alert-clear-btn-${raisedAlert.id}`))
    await user.click(within(raisedRow).getByTestId(`alert-confirm-btn-${raisedAlert.id}`))
    expect(await screen.findByTestId('alert-transition-error')).toHaveTextContent(
      /reason is required/i,
    )
    expect(mockClearAlert).not.toHaveBeenCalled()

    // Simulate 409 Conflict when submitting Clear with a reason
    mockClearAlert.mockRejectedValueOnce(
      new ApiError(409, 'Conflict', 'Alert version mismatch'),
    )
    await user.type(
      within(raisedRow).getByTestId(`alert-reason-input-${raisedAlert.id}`),
      'Pump pressure stabilized',
    )
    await user.click(within(raisedRow).getByTestId(`alert-confirm-btn-${raisedAlert.id}`))

    expect(await screen.findByTestId('alert-conflict-banner')).toHaveTextContent(
      /updated by another operator or rule evaluation/i,
    )
    expect(mockClearAlert).toHaveBeenCalledWith(
      raisedAlert.id,
      {
        expected_updated_at: raisedAlert.version,
        reason: 'Pump pressure stabilized',
      },
      expect.stringMatching(/^alert-cleared-/),
    )
  })

  it('opens the historical T1 AlertEvidenceDrawer and links to Operations Advisor', async () => {
    const user = userEvent.setup()
    renderMonitor()

    const evidenceBtn = await screen.findByTestId(`alert-evidence-btn-${raisedAlert.id}`)
    await user.click(evidenceBtn)

    const drawer = await screen.findByTestId('alert-evidence-drawer')
    expect(drawer).toBeInTheDocument()
    await waitFor(() => expect(mockAlertEvidence).toHaveBeenCalledWith(raisedAlert.id, { points: 25 }, expect.anything()))

    expect(await within(drawer).findByText('standpipe_pressure_high')).toBeInTheDocument()
    expect(within(drawer).getByTestId('evidence-observed-value')).toHaveTextContent('28,957,980')
    expect(within(drawer).getByTestId('evidence-points-table')).toHaveTextContent('late')
    expect(within(drawer).getByTestId('alert-evidence-advisor-link')).toHaveAttribute(
      'href',
      `/wells/${WELL_ID}/advisor?alert=${raisedAlert.id}`,
    )
  })

  it('renders Persian (fa) locale with RTL layout and LTR isolation on technical tokens', async () => {
    renderMonitor(`/wells/${WELL_ID}`, 'fa')

    expect(await screen.findByTestId('live-transport-state')).toHaveTextContent('زنده (LIVE)')
    const sppValue = await screen.findByTestId('kpi-value-spp')
    expect(sppValue).toHaveAttribute('dir', 'ltr')
  })
})
