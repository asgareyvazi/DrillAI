import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  ConnectorProfilesCatalogue,
  ConnectorRow,
  ConnectorsPage as ConnectorsPagePayload,
} from '../../api/types'
import { I18nProvider } from '../../i18n'
import { ConnectorsPage } from './ConnectorsPage'

const mockIdentity = vi.fn()
const mockListWells = vi.fn()
const mockConnectorProfiles = vi.fn<() => Promise<ConnectorProfilesCatalogue>>()
const mockListConnectors = vi.fn<() => Promise<ConnectorsPagePayload>>()
const mockGetConnector = vi.fn<() => Promise<ConnectorRow>>()
const mockListConnectorRuns = vi.fn()
const mockCreateConnector = vi.fn()
const mockUpdateConnector = vi.fn()
const mockTestConnector = vi.fn()
const mockPreviewConnector = vi.fn()
const mockStartConnector = vi.fn()
const mockStopConnector = vi.fn()
const mockRestartConnector = vi.fn()
const mockDisableConnector = vi.fn()
const mockPollConnector = vi.fn()
const mockEnsureProtocolHarness = vi.fn()

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    identity: (...args: unknown[]) => mockIdentity(...args),
    listWells: (...args: unknown[]) => mockListWells(...args),
    connectorProfiles: (...args: unknown[]) => mockConnectorProfiles(...(args as [])),
    listConnectors: (...args: unknown[]) => mockListConnectors(...(args as [])),
    getConnector: (...args: unknown[]) => mockGetConnector(...(args as [])),
    listConnectorRuns: (...args: unknown[]) => mockListConnectorRuns(...args),
    createConnector: (...args: unknown[]) => mockCreateConnector(...args),
    updateConnector: (...args: unknown[]) => mockUpdateConnector(...args),
    testConnector: (...args: unknown[]) => mockTestConnector(...args),
    previewConnector: (...args: unknown[]) => mockPreviewConnector(...args),
    startConnector: (...args: unknown[]) => mockStartConnector(...args),
    stopConnector: (...args: unknown[]) => mockStopConnector(...args),
    restartConnector: (...args: unknown[]) => mockRestartConnector(...args),
    disableConnector: (...args: unknown[]) => mockDisableConnector(...args),
    pollConnector: (...args: unknown[]) => mockPollConnector(...args),
    ensureProtocolHarness: (...args: unknown[]) => mockEnsureProtocolHarness(...args),
  },
}))

const sampleWitsmlConnector: ConnectorRow = {
  id: 'cnc_witsml_01',
  org_id: 'org_default',
  well_id: 'wel_01',
  wellbore_id: null,
  operation_id: null,
  project_id: null,
  key: 'rig01-witsml',
  name: 'Rig 01 WITSML Store',
  provider: 'witsml.1.4.1.1.soap_http',
  protocol_profile: 'witsml.1.4.1.1.soap_http',
  profile_spec: null,
  is_synthetic: false,
  source_classification: 'external_protocol',
  direction: 'read_only_poll',
  endpoint_url: 'https://witsml.rig01.example.com/store',
  description: null,
  desired_state: 'stopped',
  is_enabled: false,
  status: 'stopped',
  config_version: 2,
  version: '2:2026-10-10T10:00:00Z',
  config: {
    auth_mode: 'basic',
    tls_verify: true,
    poll_interval_seconds: 2.0,
    timeout_seconds: 10.0,
    max_points_per_poll: 250,
    max_consecutive_failures: 5,
  },
  channel_mappings: [
    {
      source_mnemonic: 'SPP',
      channel_key: 'spp',
      name: 'Standpipe Pressure',
      dimension: 'pressure',
      unit: 'psi',
    },
  ],
  mapped_channel_count: 1,
  secret_refs: {
    username: {
      slot: 'username',
      ref: 'env:DRILLAI_SECRET_RIG_USER',
      backend: 'env',
      masked_value: '********',
      configured: true,
    },
    password: {
      slot: 'password',
      ref: 'env:DRILLAI_SECRET_RIG_PASS',
      backend: 'env',
      masked_value: '********',
      configured: true,
    },
  },
  cursor: { watermark_ts: '2026-10-10T10:00:00Z' },
  worker: {
    worker_id: null,
    lease_expires_at: null,
    last_heartbeat_at: null,
    fencing_token: 4,
    lease_active: false,
  },
  health: {
    health_state: 'stopped',
    is_live: false,
    lease_active: false,
    data_freshness: 'stale',
    has_low_quality: false,
    trustworthy_channels: 1,
    seconds_since_last_poll: 120,
    seconds_since_last_ingest: 120,
    consecutive_failures: 0,
    backoff_seconds: 0,
    next_poll_at: null,
    reconnect_count: 1,
    last_transition_at: '2026-10-10T09:58:00Z',
    last_connected_at: '2026-10-10T09:58:00Z',
    last_poll_at: '2026-10-10T09:58:00Z',
    last_successful_poll_at: '2026-10-10T09:58:00Z',
    last_frame_at: '2026-10-10T09:58:00Z',
    last_ingest_at: '2026-10-10T09:58:00Z',
    last_error_at: null,
    last_error_category: null,
    last_error: null,
    last_trace_id: null,
  },
  allowed_actions: ['start', 'disable', 'test', 'update'],
  created_by: 'usr_eng',
  created_at: '2026-10-10T09:00:00Z',
  updated_at: '2026-10-10T10:00:00Z',
}

function renderWorkspace(initialPath = '/connectors') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/connectors" element={<ConnectorsPage />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockListWells.mockResolvedValue({
    items: [{ id: 'wel_01', name: 'Well Alpha', uwi: 'NO-01' }],
    total: 1,
    limit: 100,
    offset: 0,
  })
  mockConnectorProfiles.mockResolvedValue({
    profiles: [
      {
        profile: 'witsml.1.4.1.1.soap_http',
        provider: 'witsml.1.4.1.1.soap_http',
        standard_version: 'WITSML 1.4.1.1',
        transport: 'HTTP/HTTPS SOAP 1.1 XML (WMLS_GetFromStore)',
        auth_modes: ['none', 'basic', 'bearer'],
        supported_operations: ['WMLS_GetFromStore'],
        supported_objects: ['log (time-indexed channelData)'],
        resume_mechanism: 'startDateTimeIndex watermark',
        is_synthetic: false,
        local_harness_verified: true,
        external_vendor_verified: false,
        limitations: 'Read-only WITSML 1.4.1.1 log polling.',
      },
      {
        profile: 'synthetic.v1',
        provider: 'synthetic.v1',
        standard_version: 'DrillAI Synthetic v1',
        transport: 'In-process deterministic frame generator',
        auth_modes: ['none'],
        supported_operations: ['poll'],
        supported_objects: ['synthetic channel plan'],
        resume_mechanism: 'step_index cursor',
        is_synthetic: true,
        local_harness_verified: true,
        external_vendor_verified: false,
        limitations: 'Synthetic commissioning source only.',
      },
    ],
    allowed_secret_slots: ['username', 'password', 'bearer_token'],
    desired_states: ['enabled', 'stopped', 'disabled'],
    runtime_statuses: ['configured', 'running', 'stopped', 'failed', 'disabled'],
  })
  mockListConnectors.mockResolvedValue({
    items: [sampleWitsmlConnector],
    total: 1,
    limit: 100,
    offset: 0,
    profiles: ['witsml.1.4.1.1.soap_http', 'synthetic.v1'],
    statuses: ['stopped'],
  })
  mockGetConnector.mockResolvedValue(sampleWitsmlConnector)
  mockListConnectorRuns.mockResolvedValue({
    items: [
      {
        id: 'cnr_01',
        org_id: 'org_default',
        connector_id: 'cnc_witsml_01',
        worker_id: 'wrk_01',
        fencing_token: 4,
        run_kind: 'poll',
        status: 'succeeded',
        started_at: '2026-10-10T09:58:00Z',
        finished_at: '2026-10-10T09:58:01Z',
        duration_ms: 82,
        frames_received: 1,
        points_received: 3,
        points_accepted: 3,
        points_duplicates: 0,
        points_rejected: 0,
        bad_quality_points: 0,
        cursor_before: {},
        cursor_after: { watermark_ts: '2026-10-10T10:00:00Z' },
        error_category: null,
        error_message: null,
        diagnostics: {},
      },
    ],
    total: 1,
    limit: 25,
    offset: 0,
  })
})

describe('ConnectorsPage', () => {
  it('renders connector registry, masks secrets, never reports stopped connector as LIVE, and executes governed lifecycle & test actions', async () => {
    mockIdentity.mockResolvedValue({
      principal_id: 'usr_eng',
      permissions: ['connector.read', 'connector.manage', 'connector.control', 'connector.test'],
    })
    mockTestConnector.mockResolvedValue({
      ok: true,
      connector_id: 'cnc_witsml_01',
      protocol_profile: 'witsml.1.4.1.1.soap_http',
      endpoint_url: 'https://witsml.rig01.example.com/store',
      duration_ms: 42,
      discovered_channels: [
        { key: 'spp', label: 'Standpipe Pressure', dimension: 'pressure', unit: 'psi' },
      ],
      mapped_channels: sampleWitsmlConnector.channel_mappings,
      error_category: null,
      error_message: null,
    })
    mockDisableConnector.mockResolvedValue({
      ...sampleWitsmlConnector,
      desired_state: 'disabled',
      status: 'disabled',
    })

    renderWorkspace()

    await waitFor(() => {
      expect(screen.getByTestId('selected-connector-name')).toHaveTextContent(
        'Rig 01 WITSML Store',
      )
    })

    // Stopped connector must never display LIVE
    expect(screen.getByTestId('detail-runtime-status')).toHaveTextContent('stopped')
    expect(screen.getByTestId('detail-health-state')).toHaveTextContent('stopped')
    expect(screen.getByTestId('detail-health-state')).not.toHaveTextContent('LIVE')

    // Masked secret refs are shown as ********
    expect(screen.getByTestId('masked-secret-username')).toHaveTextContent('********')
    expect(screen.getByTestId('masked-secret-password')).toHaveTextContent('********')

    // Run connection test
    await userEvent.click(screen.getByTestId('connector-test-btn'))
    await waitFor(() => {
      expect(screen.getByTestId('connector-test-result')).toHaveTextContent('42 ms')
    })

    // Governed disable requires a reason
    await userEvent.click(screen.getByTestId('connector-disable-btn'))
    await userEvent.type(
      screen.getByTestId('connector-transition-reason'),
      'Maintenance window on rig store',
    )
    await userEvent.click(screen.getByTestId('connector-confirm-transition-btn'))

    await waitFor(() => {
      expect(mockDisableConnector).toHaveBeenCalledWith(
        'cnc_witsml_01',
        {
          reason: 'Maintenance window on rig store',
          expected_config_version: 2,
        },
        expect.stringMatching(/^cnc-disable-/),
      )
    })
  })

  it('enforces read-only view when acting as viewer without connector management permissions', async () => {
    mockIdentity.mockResolvedValue({
      principal_id: 'usr_viewer',
      permissions: ['connector.read', 'well.read'],
    })

    renderWorkspace()

    await waitFor(() => {
      expect(screen.getByTestId('connectors-readonly-banner')).toBeInTheDocument()
    })

    expect(screen.queryByTestId('connector-form-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('connector-lifecycle-controls')).not.toBeInTheDocument()
    expect(screen.queryByTestId('connector-test-btn')).not.toBeInTheDocument()
  })
})
