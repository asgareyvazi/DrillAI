import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { drillingApi } from '../../api/endpoints'
import { ApiError } from '../../api/client'
import type {
  ConnectorChannelMapping,
  ConnectorCreatePayload,
  ConnectorPreviewResult,
  ConnectorReplayResponse,
  ConnectorRow,
  ConnectorRunManifest,
  ConnectorRunRow,
  ConnectorTestResult,
} from '../../api/types'
import { Badge, Button, Card, ErrorState } from '../../components/common'
import { useI18n } from '../../i18n'
import { useSession } from '../../stores/session'

const DEFAULT_MAPPINGS: ConnectorChannelMapping[] = [
  {
    source_mnemonic: 'SPP',
    channel_key: 'spp',
    name: 'Standpipe Pressure',
    dimension: 'pressure',
    unit: 'psi',
  },
  {
    source_mnemonic: 'WOB',
    channel_key: 'wob',
    name: 'Weight on Bit',
    dimension: 'force',
    unit: 'klbf',
  },
  {
    source_mnemonic: 'RPM',
    channel_key: 'rpm',
    name: 'Rotary Speed',
    dimension: 'rotary_speed',
    unit: 'rpm',
  },
]

const DIMENSIONS = [
  'pressure',
  'force',
  'rotary_speed',
  'flow_rate',
  'length',
  'torque',
  'density',
  'temperature',
  'velocity',
  'dimensionless',
] as const

function makeIdemKey(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
}

function statusTone(status: string): 'neutral' | 'info' | 'ok' | 'warning' | 'danger' {
  switch (status) {
    case 'running':
    case 'live':
    case 'fresh':
    case 'succeeded':
      return 'ok'
    case 'starting':
    case 'configured':
    case 'created':
      return 'info'
    case 'backing_off':
    case 'degraded':
    case 'stale':
    case 'stale_data':
    case 'low_quality_data':
    case 'no_data_yet':
    case 'lease_expired':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function ConnectorsPage() {
  const { t } = useI18n()
  const { devRoles } = useSession()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const initialWellFilter = searchParams.get('wellId') ?? ''
  const [wellFilter, setWellFilter] = useState<string>(initialWellFilter)
  const [profileFilter, setProfileFilter] = useState<string>('')
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [selectedId, setSelectedId] = useState<string | null>(
    searchParams.get('connectorId') ?? null,
  )

  // Form state (create / edit)
  const [editingConnector, setEditingConnector] = useState<ConnectorRow | null>(null)
  const [formKey, setFormKey] = useState<string>('rig-witsml-01')
  const [formName, setFormName] = useState<string>('Rig WITSML 1.4.1.1 Telemetry Store')
  const [formProfile, setFormProfile] = useState<string>('witsml.1.4.1.1.soap_http')
  const [formWellId, setFormWellId] = useState<string>(initialWellFilter)
  const [formEndpoint, setFormEndpoint] = useState<string>('https://witsml.rig01.example.com/store')
  const [formAuthMode, setFormAuthMode] = useState<string>('basic')
  const [formPollInterval, setFormPollInterval] = useState<string>('2.0')
  const [formTimeout, setFormTimeout] = useState<string>('10.0')
  const [formMaxPoints, setFormMaxPoints] = useState<string>('250')
  const [formMaxFailures, setFormMaxFailures] = useState<string>('5')
  const [formUsernameRef, setFormUsernameRef] = useState<string>(
    'env:DRILLAI_SECRET_HARNESS_WITSML_USER',
  )
  const [formPasswordRef, setFormPasswordRef] = useState<string>(
    'env:DRILLAI_SECRET_HARNESS_WITSML_PASS',
  )
  const [formBearerRef, setFormBearerRef] = useState<string>('')
  const [formMappings, setFormMappings] = useState<ConnectorChannelMapping[]>(DEFAULT_MAPPINGS)
  const [formUpdateReason, setFormUpdateReason] = useState<string>('')
  const [formError, setFormError] = useState<string | null>(null)
  const [conflictBanner, setConflictBanner] = useState<string | null>(null)

  // Lifecycle transition state
  const [pendingAction, setPendingAction] = useState<
    'start' | 'stop' | 'restart' | 'disable' | null
  >(null)
  const [transitionReason, setTransitionReason] = useState<string>('')
  const [actionError, setActionError] = useState<string | null>(null)

  // Test, preview, replay & run manifest inspector state
  const [testResult, setTestResult] = useState<ConnectorTestResult | null>(null)
  const [previewResult, setPreviewResult] = useState<ConnectorPreviewResult | null>(null)
  const [replayFromTs, setReplayFromTs] = useState<string>('')
  const [replayReason, setReplayReason] = useState<string>('')
  const [replayResult, setReplayResult] = useState<ConnectorReplayResponse | null>(null)
  const [inspectedRunId, setInspectedRunId] = useState<string | null>(null)

  // Queries
  const identityQuery = useQuery({
    queryKey: ['identity', devRoles],
    queryFn: ({ signal }) => drillingApi.identity(signal),
  })

  const wellsQuery = useQuery({
    queryKey: ['wells-for-connectors', devRoles],
    queryFn: ({ signal }) => drillingApi.listWells({ limit: 100 }, signal),
  })

  const profilesQuery = useQuery({
    queryKey: ['connector-profiles', devRoles],
    queryFn: ({ signal }) => drillingApi.connectorProfiles(signal),
  })

  const connectorsQuery = useQuery({
    queryKey: ['connectors', devRoles, { wellFilter, profileFilter, statusFilter }],
    queryFn: ({ signal }) =>
      drillingApi.listConnectors(
        {
          well_id: wellFilter || undefined,
          status: statusFilter || undefined,
          limit: 100,
        },
        signal,
      ),
    refetchInterval: 4000,
  })

  const filteredItems = useMemo(() => {
    const items = connectorsQuery.data?.items ?? []
    if (!profileFilter) return items
    return items.filter((item) => item.protocol_profile === profileFilter)
  }, [connectorsQuery.data?.items, profileFilter])

  useEffect(() => {
    if (!formWellId && wellsQuery.data?.items?.length) {
      const firstWell = wellsQuery.data.items[0]
      if (firstWell) {
        setFormWellId(initialWellFilter || firstWell.id)
      }
    }
  }, [wellsQuery.data?.items, formWellId, initialWellFilter])

  useEffect(() => {
    if (!selectedId && filteredItems.length > 0) {
      const first = filteredItems[0]
      if (first) setSelectedId(first.id)
    }
  }, [filteredItems, selectedId])

  const selectedDetailQuery = useQuery({
    queryKey: ['connectors', 'detail', selectedId, devRoles],
    queryFn: ({ signal }) => drillingApi.getConnector(selectedId!, signal),
    enabled: Boolean(selectedId),
    refetchInterval: 3000,
  })

  const selectedRunsQuery = useQuery({
    queryKey: ['connectors', 'runs', selectedId, devRoles],
    queryFn: ({ signal }) => drillingApi.listConnectorRuns(selectedId!, { limit: 25 }, signal),
    enabled: Boolean(selectedId),
    refetchInterval: 4000,
  })

  const permissions = identityQuery.data?.permissions ?? []
  const hasPermission = (perm: string) =>
    permissions.includes('*') ||
    permissions.includes(perm) ||
    permissions.includes(`${perm.split('.')[0]}.*`)

  const canManage = hasPermission('connector.manage')
  const canControl = hasPermission('connector.control')
  const canTest = hasPermission('connector.test')
  const isReadOnly = Boolean(identityQuery.data) && !canManage && !canControl && !canTest

  const invalidateConnectorQueries = async (cid?: string | null, wid?: string | null) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['connectors'] }),
      wid
        ? queryClient.invalidateQueries({ queryKey: ['well-live-snapshot', wid] })
        : Promise.resolve(),
      cid
        ? queryClient.invalidateQueries({ queryKey: ['connectors', 'detail', cid] })
        : Promise.resolve(),
      cid
        ? queryClient.invalidateQueries({ queryKey: ['connectors', 'runs', cid] })
        : Promise.resolve(),
    ])
  }

  const startEdit = (row: ConnectorRow) => {
    setEditingConnector(row)
    setFormError(null)
    setConflictBanner(null)
    setFormKey(row.key)
    setFormName(row.name)
    setFormProfile(row.protocol_profile)
    setFormWellId(row.well_id ?? '')
    setFormEndpoint(row.endpoint_url ?? '')
    const cfg = row.config ?? {}
    setFormAuthMode(String(cfg.auth_mode ?? 'none'))
    setFormPollInterval(String(cfg.poll_interval_seconds ?? 2.0))
    setFormTimeout(String(cfg.timeout_seconds ?? 10.0))
    setFormMaxPoints(String(cfg.max_points_per_poll ?? 250))
    setFormMaxFailures(String(cfg.max_consecutive_failures ?? 5))
    setFormUsernameRef(row.secret_refs?.username?.ref ?? '')
    setFormPasswordRef(row.secret_refs?.password?.ref ?? '')
    setFormBearerRef(row.secret_refs?.bearer_token?.ref ?? '')
    setFormMappings(
      row.channel_mappings?.length ? [...row.channel_mappings] : [...DEFAULT_MAPPINGS],
    )
    setFormUpdateReason('')
  }

  const resetFormToCreate = () => {
    setEditingConnector(null)
    setFormError(null)
    setConflictBanner(null)
    setFormUpdateReason('')
  }

  const applyPresetMutation = useMutation({
    mutationFn: async (preset: 'witsml' | 'etp_binary' | 'etp' | 'synthetic') => {
      setFormError(null)
      if (preset === 'synthetic') {
        setEditingConnector(null)
        setFormProfile('synthetic.v1')
        setFormKey(`synth-rig-${Date.now().toString(36).slice(-4)}`)
        setFormName('Synthetic Commissioning Rig Source')
        setFormEndpoint('synthetic://plan')
        setFormAuthMode('none')
        setFormPollInterval('1.0')
        setFormUsernameRef('')
        setFormPasswordRef('')
        setFormBearerRef('')
        setFormMappings([...DEFAULT_MAPPINGS])
        return null
      }
      const harness = await drillingApi.ensureProtocolHarness()
      if (preset === 'witsml') {
        setEditingConnector(null)
        setFormProfile('witsml.1.4.1.1.soap_http')
        setFormKey(`witsml-rig-${Date.now().toString(36).slice(-4)}`)
        setFormName('Local WITSML 1.4.1.1 SOAP Store')
        setFormEndpoint(harness.witsml.endpoint_url ?? 'http://127.0.0.1:18080/witsml/store')
        setFormAuthMode('basic')
        setFormPollInterval('1.0')
        setFormUsernameRef(
          harness.witsml.secret_refs.username ?? 'env:DRILLAI_SECRET_HARNESS_WITSML_USER',
        )
        setFormPasswordRef(
          harness.witsml.secret_refs.password ?? 'env:DRILLAI_SECRET_HARNESS_WITSML_PASS',
        )
        setFormBearerRef('')
        setFormMappings([...DEFAULT_MAPPINGS])
      } else if (preset === 'etp_binary') {
        setEditingConnector(null)
        setFormProfile('etp.1.2.binary_ws')
        setFormKey(`etp-bin-rig-${Date.now().toString(36).slice(-4)}`)
        setFormName('Local ETP 1.2 Binary Avro Stream')
        setFormEndpoint(
          harness.etp_binary?.endpoint_url ?? 'ws://127.0.0.1:18082/etp12-bin',
        )
        setFormAuthMode('bearer')
        setFormPollInterval('1.0')
        setFormUsernameRef('')
        setFormPasswordRef('')
        setFormBearerRef(
          harness.etp_binary?.secret_refs?.bearer_token ??
            'env:DRILLAI_SECRET_HARNESS_ETP_BINARY_TOKEN',
        )
        setFormMappings([...DEFAULT_MAPPINGS])
      } else {
        setEditingConnector(null)
        setFormProfile('etp.1.2.json_ws')
        setFormKey(`etp-rig-${Date.now().toString(36).slice(-4)}`)
        setFormName('Local ETP 1.2 WebSocket Stream')
        setFormEndpoint(harness.etp.endpoint_url ?? 'ws://127.0.0.1:18081/etp')
        setFormAuthMode('bearer')
        setFormPollInterval('1.0')
        setFormUsernameRef('')
        setFormPasswordRef('')
        setFormBearerRef(
          harness.etp.secret_refs.bearer_token ?? 'env:DRILLAI_SECRET_HARNESS_ETP_TOKEN',
        )
        setFormMappings([...DEFAULT_MAPPINGS])
      }
      return harness
    },
    onError: (err) => {
      setFormError(err instanceof Error ? err.message : String(err))
    },
  })

  const saveMutation = useMutation({
    mutationFn: async () => {
      setFormError(null)
      setConflictBanner(null)

      const secretRefs: Record<string, string> = {}
      if (formAuthMode === 'basic') {
        if (formUsernameRef.trim()) secretRefs.username = formUsernameRef.trim()
        if (formPasswordRef.trim()) secretRefs.password = formPasswordRef.trim()
      } else if (formAuthMode === 'bearer') {
        if (formBearerRef.trim()) secretRefs.bearer_token = formBearerRef.trim()
      }

      const configPayload: Record<string, unknown> = {
        auth_mode: formProfile === 'synthetic.v1' ? 'none' : formAuthMode,
        tls_verify: true,
        poll_interval_seconds: Number(formPollInterval) || 2.0,
        timeout_seconds: Number(formTimeout) || 10.0,
        max_points_per_poll: Number(formMaxPoints) || 250,
        max_consecutive_failures: Number(formMaxFailures) || 5,
        channel_mappings: formMappings.map((m) => ({
          source_mnemonic: m.source_mnemonic.trim(),
          channel_key: m.channel_key.trim().toLowerCase(),
          name: m.name.trim(),
          dimension: m.dimension,
          unit: m.unit.trim(),
        })),
      }

      if (editingConnector) {
        if (!formUpdateReason.trim()) {
          throw new Error('A reason is required to update connector configuration.')
        }
        return drillingApi.updateConnector(
          editingConnector.id,
          {
            expected_config_version: editingConnector.config_version,
            reason: formUpdateReason.trim(),
            name: formName.trim(),
            endpoint_url:
              formProfile === 'synthetic.v1' ? 'synthetic://plan' : formEndpoint.trim(),
            config: configPayload,
            secret_refs: secretRefs,
          },
          makeIdemKey('cnc-update'),
        )
      }

      const createPayload: ConnectorCreatePayload = {
        key: formKey.trim(),
        name: formName.trim(),
        protocol_profile: formProfile,
        well_id: formWellId,
        endpoint_url:
          formProfile === 'synthetic.v1' ? 'synthetic://plan' : formEndpoint.trim(),
        config: configPayload,
        secret_refs: secretRefs,
      }
      return drillingApi.createConnector(createPayload, makeIdemKey('cnc-create'))
    },
    onSuccess: async (saved) => {
      setSelectedId(saved.id)
      setEditingConnector(null)
      setFormUpdateReason('')
      setTestResult(null)
      setPreviewResult(null)
      await invalidateConnectorQueries(saved.id, saved.well_id)
    },
    onError: async (err) => {
      if (err instanceof ApiError && err.status === 409 && editingConnector) {
        const latest = await drillingApi.getConnector(editingConnector.id)
        startEdit(latest)
        setConflictBanner(
          'Connector configuration was updated by another operator. Reloaded latest version.',
        )
        return
      }
      setFormError(err instanceof Error ? err.message : String(err))
    },
  })

  const transitionMutation = useMutation({
    mutationFn: async ({
      connector,
      action,
      reason,
    }: {
      connector: ConnectorRow
      action: 'start' | 'stop' | 'restart' | 'disable'
      reason: string
    }) => {
      setActionError(null)
      const body = {
        reason: reason.trim() || undefined,
        expected_config_version: connector.config_version,
      }
      const idem = makeIdemKey(`cnc-${action}`)
      if (action === 'start') return drillingApi.startConnector(connector.id, body, idem)
      if (action === 'stop') return drillingApi.stopConnector(connector.id, body, idem)
      if (action === 'restart') return drillingApi.restartConnector(connector.id, body, idem)
      return drillingApi.disableConnector(connector.id, body, idem)
    },
    onSuccess: async (updated) => {
      setPendingAction(null)
      setTransitionReason('')
      await invalidateConnectorQueries(updated.id, updated.well_id)
    },
    onError: (err) => {
      setActionError(err instanceof Error ? err.message : String(err))
    },
  })

  const testMutation = useMutation({
    mutationFn: (connectorId: string) => drillingApi.testConnector(connectorId),
    onSuccess: async (res) => {
      setTestResult(res)
      await invalidateConnectorQueries(res.connector_id, selectedDetailQuery.data?.well_id)
    },
    onError: (err) => {
      setActionError(err instanceof Error ? err.message : String(err))
    },
  })

  const previewMutation = useMutation({
    mutationFn: (connectorId: string) => drillingApi.previewConnector(connectorId, 12),
    onSuccess: (res) => {
      setPreviewResult(res)
    },
    onError: (err) => {
      setActionError(err instanceof Error ? err.message : String(err))
    },
  })

  const pollNowMutation = useMutation({
    mutationFn: (connectorId: string) => drillingApi.pollConnector(connectorId),
    onSuccess: async (res) => {
      await invalidateConnectorQueries(res.connector.id, res.connector.well_id)
    },
    onError: (err) => {
      setActionError(err instanceof Error ? err.message : String(err))
    },
  })

  const replayMutation = useMutation({
    mutationFn: async ({
      connector,
      dryRun,
    }: {
      connector: ConnectorRow
      dryRun: boolean
    }) => {
      setActionError(null)
      if (!replayReason.trim()) {
        throw new Error('A governance reason is required to execute or preview a replay.')
      }
      return drillingApi.replayConnector(
        connector.id,
        {
          reason: replayReason.trim(),
          from_timestamp: replayFromTs.trim() || null,
          dry_run: dryRun,
          expected_config_version: connector.config_version,
        },
        makeIdemKey(dryRun ? 'cnc-replay-dry' : 'cnc-replay-live'),
      )
    },
    onSuccess: async (res) => {
      setReplayResult(res)
      setInspectedRunId(res.run.id)
      await invalidateConnectorQueries(res.connector.id, res.connector.well_id)
    },
    onError: (err) => {
      setActionError(err instanceof Error ? err.message : String(err))
    },
  })

  const getRemediationText = (category: string | null | undefined): string => {
    switch (category) {
      case 'ssrf_blocked':
        return t('connectors.remediationSsrf')
      case 'dns_failure':
        return t('connectors.remediationDns')
      case 'auth_failure':
        return t('connectors.remediationAuth')
      case 'secret_resolution_error':
        return t('connectors.remediationSecret')
      case 'protocol_error':
      case 'malformed_payload':
        return t('connectors.remediationProtocol')
      case 'network_timeout':
      case 'transport_error':
        return t('connectors.remediationTimeout')
      default:
        return t('connectors.remediationDefault')
    }
  }

  const selected = selectedDetailQuery.data ?? null
  const runsList: ConnectorRunRow[] = selectedRunsQuery.data?.items ?? []
  const inspectedRun: ConnectorRunRow | null =
    runsList.find((r) => r.id === inspectedRunId) ?? runsList[0] ?? null
  const inspectedManifest: ConnectorRunManifest | null =
    inspectedRun &&
    typeof inspectedRun.details === 'object' &&
    inspectedRun.details !== null &&
    'manifest' in inspectedRun.details
      ? (inspectedRun.details.manifest as ConnectorRunManifest)
      : null

  return (
    <div className="space-y-6" data-testid="connectors-workspace">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">{t('connectors.title')}</h1>
        <p className="mt-1 text-xs text-slate-400">{t('connectors.subtitle')}</p>
      </div>

      {isReadOnly && (
        <div
          role="status"
          data-testid="connectors-readonly-banner"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200"
        >
          {t('connectors.readOnlyBanner')}
        </div>
      )}

      {/* Protocol Profile Capability & Verification Honesty Cards */}
      {profilesQuery.data?.profiles && (
        <div
          className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4"
          data-testid="connector-profile-cards"
        >
          {profilesQuery.data.profiles.map((spec) => (
            <Card key={spec.profile} className="p-4" data-testid={`profile-card-${spec.profile}`}>
              <div className="flex items-center justify-between gap-2">
                <span
                  dir="ltr"
                  className="font-mono text-xs font-semibold text-sky-300"
                >
                  {spec.profile}
                </span>
                <Badge tone={spec.is_synthetic ? 'warning' : 'info'}>
                  {spec.is_synthetic
                    ? t('connectors.syntheticBadge')
                    : t('connectors.externalBadge')}
                </Badge>
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
                {spec.wire_encoding && (
                  <Badge
                    tone={spec.is_test_only_encoding ? 'warning' : 'info'}
                    data-testid={`profile-encoding-${spec.profile}`}
                  >
                    {spec.wire_encoding}
                  </Badge>
                )}
                {spec.is_test_only_encoding && (
                  <Badge tone="warning">{t('connectors.etpJsonTestBadge')}</Badge>
                )}
                {spec.conformance_verified && (
                  <Badge tone="ok">{t('connectors.conformanceVerifiedBadge')}</Badge>
                )}
                <Badge tone="ok">{t('connectors.localHarnessVerified')}</Badge>
                {!spec.external_vendor_verified && (
                  <Badge tone="neutral">{t('connectors.externalVendorUnverified')}</Badge>
                )}
              </div>
              <p className="mt-2 text-xs text-slate-400">{spec.limitations}</p>
            </Card>
          ))}
        </div>
      )}

      {/* Filter Bar */}
      <Card className="p-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <label
              htmlFor="filter-connector-well"
              className="mb-1 block text-xs font-medium text-slate-300"
            >
              {t('connectors.filterWell')}
            </label>
            <select
              id="filter-connector-well"
              data-testid="filter-connector-well"
              value={wellFilter}
              onChange={(e) => {
                const next = e.target.value
                setWellFilter(next)
                if (next) {
                  setSearchParams({ wellId: next })
                } else {
                  setSearchParams({})
                }
              }}
              className="w-full rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
            >
              <option value="">{t('connectors.allWells')}</option>
              {(wellsQuery.data?.items ?? []).map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name} ({w.uwi ?? w.id})
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="filter-connector-profile"
              className="mb-1 block text-xs font-medium text-slate-300"
            >
              {t('connectors.filterProfile')}
            </label>
            <select
              id="filter-connector-profile"
              data-testid="filter-connector-profile"
              value={profileFilter}
              onChange={(e) => setProfileFilter(e.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
            >
              <option value="">{t('connectors.allProfiles')}</option>
              {(profilesQuery.data?.profiles ?? []).map((p) => (
                <option key={p.profile} value={p.profile}>
                  {p.profile}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="filter-connector-status"
              className="mb-1 block text-xs font-medium text-slate-300"
            >
              {t('connectors.filterStatus')}
            </label>
            <select
              id="filter-connector-status"
              data-testid="filter-connector-status"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
            >
              <option value="">{t('connectors.allStatuses')}</option>
              {(profilesQuery.data?.runtime_statuses ?? []).map((st) => (
                <option key={st} value={st}>
                  {st}
                </option>
              ))}
            </select>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Left column: Connector Registry List + Create/Edit Form */}
        <div className="space-y-6 lg:col-span-5">
          <Card className="p-4">
            <h2 className="text-sm font-semibold text-slate-100">
              {t('connectors.title')} ({filteredItems.length})
            </h2>

            {connectorsQuery.isLoading ? (
              <div className="py-8 text-xs text-slate-400">{t('common.loading')}</div>
            ) : connectorsQuery.isError ? (
              <ErrorState
                error={connectorsQuery.error}
                onRetry={() => void connectorsQuery.refetch()}
              />
            ) : filteredItems.length === 0 ? (
              <p
                className="py-6 text-sm text-slate-400"
                data-testid="connectors-empty-state"
              >
                {t('connectors.emptyRegistry')}
              </p>
            ) : (
              <div className="mt-3 space-y-2" data-testid="connectors-list">
                {filteredItems.map((item) => {
                  const isSelected = item.id === selectedId
                  return (
                    <button
                      key={item.id}
                      type="button"
                      data-testid={`connector-row-${item.key}`}
                      onClick={() => {
                        setSelectedId(item.id)
                        setTestResult(null)
                        setPreviewResult(null)
                        setActionError(null)
                      }}
                      className={`w-full rounded-md border p-3 text-start transition ${
                        isSelected
                          ? 'border-sky-500 bg-sky-950/30'
                          : 'border-slate-800 bg-slate-900/60 hover:border-slate-700'
                      }`}
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-medium text-slate-100">{item.name}</span>
                        <div className="flex items-center gap-1.5">
                          <Badge tone={statusTone(item.status)}>{item.status}</Badge>
                          <Badge tone={statusTone(item.health.health_state)}>
                            {item.health.is_live ? 'LIVE' : item.health.health_state}
                          </Badge>
                        </div>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                        <span dir="ltr" className="font-mono text-slate-300">
                          {item.key}
                        </span>
                        <span>•</span>
                        <span dir="ltr" className="font-mono">
                          {item.protocol_profile}
                        </span>
                        <span>•</span>
                        <Badge tone={item.is_synthetic ? 'warning' : 'info'}>
                          {item.is_synthetic
                            ? t('connectors.syntheticBadge')
                            : t('connectors.externalBadge')}
                        </Badge>
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </Card>

          {/* Create / Update Connector Form (Engineers & Data Managers) */}
          {canManage && (
            <Card className="p-4" data-testid="connector-form-card">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold text-slate-100">
                  {editingConnector
                    ? `${t('connectors.editTitle')}: ${editingConnector.key}`
                    : t('connectors.createTitle')}
                </h2>
                {editingConnector && (
                  <Button
                    variant="ghost"
                    onClick={resetFormToCreate}
                    data-testid="connector-cancel-edit"
                  >
                    {t('connectors.cancelEditButton')}
                  </Button>
                )}
              </div>

              {!editingConnector && (
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    variant="default"
                    onClick={() => applyPresetMutation.mutate('witsml')}
                    disabled={applyPresetMutation.isPending}
                    data-testid="preset-local-witsml"
                  >
                    {t('connectors.useLocalWitsmlPreset')}
                  </Button>
                  <Button
                    variant="default"
                    onClick={() => applyPresetMutation.mutate('etp_binary')}
                    disabled={applyPresetMutation.isPending}
                    data-testid="preset-local-etp-binary"
                  >
                    {t('connectors.useLocalEtpBinaryPreset')}
                  </Button>
                  <Button
                    variant="default"
                    onClick={() => applyPresetMutation.mutate('etp')}
                    disabled={applyPresetMutation.isPending}
                    data-testid="preset-local-etp"
                  >
                    {t('connectors.useLocalEtpPreset')}
                  </Button>
                  <Button
                    variant="default"
                    onClick={() => applyPresetMutation.mutate('synthetic')}
                    disabled={applyPresetMutation.isPending}
                    data-testid="preset-synthetic"
                  >
                    {t('connectors.useSyntheticPreset')}
                  </Button>
                </div>
              )}

              {conflictBanner && (
                <div
                  role="alert"
                  data-testid="connector-conflict-banner"
                  className="mt-3 rounded border border-amber-500/50 bg-amber-500/10 p-2.5 text-xs text-amber-200"
                >
                  {conflictBanner}
                </div>
              )}

              {formError && (
                <div
                  role="alert"
                  data-testid="connector-form-error"
                  className="mt-3 rounded border border-rose-500/50 bg-rose-500/10 p-2.5 text-xs text-rose-200"
                >
                  {formError}
                </div>
              )}

              <form
                className="mt-4 space-y-3"
                onSubmit={(e) => {
                  e.preventDefault()
                  saveMutation.mutate()
                }}
              >
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label
                      htmlFor="connector-key-input"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.keyLabel')}
                    </label>
                    <input
                      id="connector-key-input"
                      data-testid="connector-key-input"
                      dir="ltr"
                      disabled={Boolean(editingConnector)}
                      value={formKey}
                      onChange={(e) => setFormKey(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 font-mono text-xs text-slate-100 disabled:opacity-50"
                      required
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="connector-name-input"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.nameLabel')}
                    </label>
                    <input
                      id="connector-name-input"
                      data-testid="connector-name-input"
                      value={formName}
                      onChange={(e) => setFormName(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-100"
                      required
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label
                      htmlFor="connector-profile-select"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.profileLabel')}
                    </label>
                    <select
                      id="connector-profile-select"
                      data-testid="connector-profile-select"
                      disabled={Boolean(editingConnector)}
                      value={formProfile}
                      onChange={(e) => {
                        const next = e.target.value
                        setFormProfile(next)
                        if (next === 'synthetic.v1') {
                          setFormEndpoint('synthetic://plan')
                          setFormAuthMode('none')
                        } else if (
                          next === 'etp.1.2.binary_ws' ||
                          next === 'etp.1.2.json_ws'
                        ) {
                          setFormAuthMode('bearer')
                        } else {
                          setFormAuthMode('basic')
                        }
                      }}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 font-mono text-xs text-slate-100 disabled:opacity-50"
                    >
                      <option value="witsml.1.4.1.1.soap_http">
                        witsml.1.4.1.1.soap_http
                      </option>
                      <option value="etp.1.2.binary_ws">etp.1.2.binary_ws</option>
                      <option value="etp.1.2.json_ws">etp.1.2.json_ws</option>
                      <option value="synthetic.v1">synthetic.v1</option>
                    </select>
                  </div>

                  <div>
                    <label
                      htmlFor="connector-well-select"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.wellLabel')}
                    </label>
                    <select
                      id="connector-well-select"
                      data-testid="connector-well-select"
                      disabled={Boolean(editingConnector)}
                      value={formWellId}
                      onChange={(e) => setFormWellId(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-100 disabled:opacity-50"
                      required
                    >
                      {(wellsQuery.data?.items ?? []).map((w) => (
                        <option key={w.id} value={w.id}>
                          {w.name} ({w.uwi ?? w.id})
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                {formProfile !== 'synthetic.v1' && (
                  <div>
                    <label
                      htmlFor="connector-endpoint-input"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.endpointLabel')}
                    </label>
                    <input
                      id="connector-endpoint-input"
                      data-testid="connector-endpoint-input"
                      dir="ltr"
                      value={formEndpoint}
                      onChange={(e) => setFormEndpoint(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 font-mono text-xs text-slate-100"
                      required
                    />
                  </div>
                )}

                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <div>
                    <label
                      htmlFor="connector-auth-mode"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.authModeLabel')}
                    </label>
                    <select
                      id="connector-auth-mode"
                      data-testid="connector-auth-mode"
                      disabled={formProfile === 'synthetic.v1'}
                      value={formProfile === 'synthetic.v1' ? 'none' : formAuthMode}
                      onChange={(e) => setFormAuthMode(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-100"
                    >
                      <option value="none">none</option>
                      <option value="basic">basic</option>
                      <option value="bearer">bearer</option>
                    </select>
                  </div>

                  <div>
                    <label
                      htmlFor="connector-poll-interval"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.pollIntervalLabel')}
                    </label>
                    <input
                      id="connector-poll-interval"
                      data-testid="connector-poll-interval"
                      dir="ltr"
                      type="number"
                      step="any"
                      min="0.05"
                      value={formPollInterval}
                      onChange={(e) => setFormPollInterval(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1.5 font-mono text-xs text-slate-100"
                    />
                  </div>

                  <div>
                    <label
                      htmlFor="connector-timeout"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.timeoutLabel')}
                    </label>
                    <input
                      id="connector-timeout"
                      dir="ltr"
                      type="number"
                      step="any"
                      min="0.5"
                      value={formTimeout}
                      onChange={(e) => setFormTimeout(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1.5 font-mono text-xs text-slate-100"
                    />
                  </div>

                  <div>
                    <label
                      htmlFor="connector-max-points"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.maxPointsLabel')}
                    </label>
                    <input
                      id="connector-max-points"
                      data-testid="connector-max-points"
                      dir="ltr"
                      type="number"
                      min="1"
                      max="5000"
                      value={formMaxPoints}
                      onChange={(e) => setFormMaxPoints(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1.5 font-mono text-xs text-slate-100"
                    />
                  </div>

                  <div>
                    <label
                      htmlFor="connector-max-failures"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.maxFailuresLabel')}
                    </label>
                    <input
                      id="connector-max-failures"
                      data-testid="connector-max-failures"
                      dir="ltr"
                      type="number"
                      min="1"
                      max="50"
                      value={formMaxFailures}
                      onChange={(e) => setFormMaxFailures(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1.5 font-mono text-xs text-slate-100"
                    />
                  </div>
                </div>

                {/* Secret References */}
                {formProfile !== 'synthetic.v1' && formAuthMode !== 'none' && (
                  <div className="rounded border border-slate-800 bg-slate-950/50 p-3">
                    <p className="mb-2 text-xs text-slate-400">
                      {t('connectors.secretRefHint')}
                    </p>
                    {formAuthMode === 'basic' ? (
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <div>
                          <label
                            htmlFor="secret-ref-username"
                            className="mb-1 block text-xs text-slate-300"
                          >
                            {t('connectors.secretSlotUsername')}
                          </label>
                          <input
                            id="secret-ref-username"
                            data-testid="secret-ref-username"
                            dir="ltr"
                            value={formUsernameRef}
                            onChange={(e) => setFormUsernameRef(e.target.value)}
                            placeholder="env:DRILLAI_SECRET_..."
                            className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-xs text-slate-100"
                          />
                        </div>
                        <div>
                          <label
                            htmlFor="secret-ref-password"
                            className="mb-1 block text-xs text-slate-300"
                          >
                            {t('connectors.secretSlotPassword')}
                          </label>
                          <input
                            id="secret-ref-password"
                            data-testid="secret-ref-password"
                            dir="ltr"
                            value={formPasswordRef}
                            onChange={(e) => setFormPasswordRef(e.target.value)}
                            placeholder="env:DRILLAI_SECRET_..."
                            className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-xs text-slate-100"
                          />
                        </div>
                      </div>
                    ) : (
                      <div>
                        <label
                          htmlFor="secret-ref-bearer"
                          className="mb-1 block text-xs text-slate-300"
                        >
                          {t('connectors.secretSlotBearer')}
                        </label>
                        <input
                          id="secret-ref-bearer"
                          data-testid="secret-ref-bearer"
                          dir="ltr"
                          value={formBearerRef}
                          onChange={(e) => setFormBearerRef(e.target.value)}
                          placeholder="env:DRILLAI_SECRET_..."
                          className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-xs text-slate-100"
                        />
                      </div>
                    )}
                  </div>
                )}

                {/* Channel Mappings Editor */}
                <div className="space-y-2 rounded border border-slate-800 bg-slate-950/50 p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-200">
                      {t('connectors.mappingsTitle')} ({formMappings.length})
                    </span>
                    <Button
                      variant="ghost"
                      onClick={() =>
                        setFormMappings((prev) => [
                          ...prev,
                          {
                            source_mnemonic: `CH${prev.length + 1}`,
                            channel_key: `ch_${prev.length + 1}`,
                            name: `Channel ${prev.length + 1}`,
                            dimension: 'pressure',
                            unit: 'psi',
                          },
                        ])
                      }
                      data-testid="add-channel-mapping-btn"
                    >
                      + {t('connectors.addMapping')}
                    </Button>
                  </div>

                  {formMappings.map((m, idx) => (
                    <div
                      key={idx}
                      className="grid grid-cols-12 items-center gap-1.5 text-xs"
                      data-testid={`mapping-row-${idx}`}
                    >
                      <input
                        dir="ltr"
                        aria-label={`${t('connectors.sourceMnemonicLabel')} ${idx + 1}`}
                        value={m.source_mnemonic}
                        onChange={(e) => {
                          const val = e.target.value
                          setFormMappings((prev) =>
                            prev.map((row, i) =>
                              i === idx ? { ...row, source_mnemonic: val } : row,
                            ),
                          )
                        }}
                        placeholder="Mnemonic"
                        className="col-span-2 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 font-mono text-slate-100"
                      />
                      <input
                        dir="ltr"
                        aria-label={`${t('connectors.targetKeyLabel')} ${idx + 1}`}
                        value={m.channel_key}
                        onChange={(e) => {
                          const val = e.target.value
                          setFormMappings((prev) =>
                            prev.map((row, i) =>
                              i === idx ? { ...row, channel_key: val } : row,
                            ),
                          )
                        }}
                        placeholder="key"
                        className="col-span-2 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 font-mono text-slate-100"
                      />
                      <input
                        aria-label={`${t('connectors.channelNameLabel')} ${idx + 1}`}
                        value={m.name}
                        onChange={(e) => {
                          const val = e.target.value
                          setFormMappings((prev) =>
                            prev.map((row, i) => (i === idx ? { ...row, name: val } : row)),
                          )
                        }}
                        placeholder="Name"
                        className="col-span-3 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 text-slate-100"
                      />
                      <select
                        dir="ltr"
                        aria-label={`${t('connectors.dimensionLabel')} ${idx + 1}`}
                        value={m.dimension}
                        onChange={(e) => {
                          const val = e.target.value
                          setFormMappings((prev) =>
                            prev.map((row, i) =>
                              i === idx ? { ...row, dimension: val } : row,
                            ),
                          )
                        }}
                        className="col-span-2 rounded border border-slate-700 bg-slate-900 px-1 py-1 font-mono text-slate-100"
                      >
                        {DIMENSIONS.map((d) => (
                          <option key={d} value={d}>
                            {d}
                          </option>
                        ))}
                      </select>
                      <input
                        dir="ltr"
                        aria-label={`${t('connectors.unitLabel')} ${idx + 1}`}
                        value={m.unit}
                        onChange={(e) => {
                          const val = e.target.value
                          setFormMappings((prev) =>
                            prev.map((row, i) => (i === idx ? { ...row, unit: val } : row)),
                          )
                        }}
                        placeholder="Unit"
                        className="col-span-2 rounded border border-slate-700 bg-slate-900 px-1.5 py-1 font-mono text-slate-100"
                      />
                      <button
                        type="button"
                        onClick={() =>
                          setFormMappings((prev) => prev.filter((_, i) => i !== idx))
                        }
                        disabled={formMappings.length <= 1}
                        className="col-span-1 text-rose-400 hover:text-rose-300 disabled:opacity-30"
                        title={t('connectors.removeMapping')}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>

                {editingConnector && (
                  <div>
                    <label
                      htmlFor="connector-update-reason"
                      className="mb-1 block text-xs text-slate-300"
                    >
                      {t('connectors.updateReasonLabel')}
                    </label>
                    <input
                      id="connector-update-reason"
                      data-testid="connector-update-reason"
                      value={formUpdateReason}
                      onChange={(e) => setFormUpdateReason(e.target.value)}
                      placeholder={t('connectors.updateReasonPlaceholder')}
                      className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-100"
                      required
                    />
                  </div>
                )}

                <div className="pt-1">
                  <Button
                    type="submit"
                    variant="primary"
                    disabled={saveMutation.isPending}
                    data-testid="connector-submit-btn"
                  >
                    {editingConnector
                      ? t('connectors.saveUpdateButton')
                      : t('connectors.createButton')}
                  </Button>
                </div>
              </form>
            </Card>
          )}
        </div>

        {/* Right column: Selected Connector Detail, Lifecycle Controls, Test/Preview & Run Ledger */}
        <div className="space-y-6 lg:col-span-7">
          {!selected ? (
            <Card className="p-6 text-sm text-slate-400">
              {t('connectors.emptyRegistry')}
            </Card>
          ) : (
            <>
              <Card className="p-5" data-testid="connector-detail-card">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <h2
                        className="text-base font-semibold text-slate-100"
                        data-testid="selected-connector-name"
                      >
                        {selected.name}
                      </h2>
                      <Badge tone={selected.is_synthetic ? 'warning' : 'info'}>
                        {selected.is_synthetic
                          ? t('connectors.syntheticBadge')
                          : t('connectors.externalBadge')}
                      </Badge>
                      {selected.protocol_profile === 'etp.1.2.binary_ws' && (
                        <Badge tone="ok" data-testid="detail-profile-honesty-badge">
                          {t('connectors.etpBinaryBadge')}
                        </Badge>
                      )}
                      {selected.is_test_only_encoding && (
                        <Badge tone="warning" data-testid="detail-profile-honesty-badge">
                          {t('connectors.etpJsonTestBadge')}
                        </Badge>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                      <span dir="ltr" className="font-mono text-slate-300">
                        {selected.id}
                      </span>
                      <span>•</span>
                      <span dir="ltr" className="font-mono text-sky-300">
                        {selected.key}
                      </span>
                      <span>•</span>
                      <span dir="ltr" className="font-mono">
                        {selected.protocol_profile}
                      </span>
                      {selected.well_id && (
                        <>
                          <span>•</span>
                          <Link
                            to={`/wells/${selected.well_id}/cockpit?tab=live`}
                            className="text-sky-400 underline hover:text-sky-300"
                            data-testid="connector-open-monitor-link"
                          >
                            {t('connectors.openMonitorLink')}
                          </Link>
                        </>
                      )}
                    </div>
                  </div>

                  {/* Action Buttons */}
                  <div className="flex flex-wrap items-center gap-2">
                    {canManage && (
                      <Button
                        variant="default"
                        onClick={() => startEdit(selected)}
                        data-testid="connector-edit-btn"
                      >
                        {t('connectors.editButton')}
                      </Button>
                    )}
                    {canTest && (
                      <>
                        <Button
                          variant="default"
                          onClick={() => testMutation.mutate(selected.id)}
                          disabled={testMutation.isPending}
                          data-testid="connector-test-btn"
                        >
                          {t('connectors.testButton')}
                        </Button>
                        <Button
                          variant="default"
                          onClick={() => previewMutation.mutate(selected.id)}
                          disabled={previewMutation.isPending}
                          data-testid="connector-preview-btn"
                        >
                          {t('connectors.previewButton')}
                        </Button>
                      </>
                    )}
                    {canControl && selected.is_enabled && (
                      <Button
                        variant="default"
                        onClick={() => pollNowMutation.mutate(selected.id)}
                        disabled={pollNowMutation.isPending}
                        data-testid="connector-poll-now-btn"
                      >
                        {t('connectors.pollNowButton')}
                      </Button>
                    )}
                  </div>
                </div>

                {/* Governed Lifecycle Transition Controls */}
                {canControl && (
                  <div
                    className="mt-4 border-t border-slate-800 pt-4"
                    data-testid="connector-lifecycle-controls"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      {selected.allowed_actions.includes('start') && (
                        <Button
                          variant="primary"
                          onClick={() =>
                            transitionMutation.mutate({
                              connector: selected,
                              action: 'start',
                              reason: 'Operator started connector',
                            })
                          }
                          disabled={transitionMutation.isPending}
                          data-testid="connector-start-btn"
                        >
                          {t('connectors.startButton')}
                        </Button>
                      )}
                      {selected.allowed_actions.includes('stop') && (
                        <Button
                          variant="default"
                          onClick={() => setPendingAction('stop')}
                          disabled={transitionMutation.isPending}
                          data-testid="connector-stop-btn"
                        >
                          {t('connectors.stopButton')}
                        </Button>
                      )}
                      {selected.allowed_actions.includes('restart') && (
                        <Button
                          variant="default"
                          onClick={() => setPendingAction('restart')}
                          disabled={transitionMutation.isPending}
                          data-testid="connector-restart-btn"
                        >
                          {t('connectors.restartButton')}
                        </Button>
                      )}
                      {selected.allowed_actions.includes('disable') && (
                        <Button
                          variant="danger"
                          onClick={() => setPendingAction('disable')}
                          disabled={transitionMutation.isPending}
                          data-testid="connector-disable-btn"
                        >
                          {t('connectors.disableButton')}
                        </Button>
                      )}
                    </div>

                    {pendingAction && (
                      <form
                        className="mt-3 flex flex-wrap items-end gap-2 rounded border border-slate-700 bg-slate-900 p-3"
                        data-testid="connector-transition-form"
                        onSubmit={(e) => {
                          e.preventDefault()
                          transitionMutation.mutate({
                            connector: selected,
                            action: pendingAction,
                            reason: transitionReason,
                          })
                        }}
                      >
                        <div className="min-w-[240px] flex-1">
                          <label
                            htmlFor="connector-transition-reason"
                            className="mb-1 block text-xs text-slate-300"
                          >
                            {t('connectors.transitionReasonLabel')} ({pendingAction})
                          </label>
                          <input
                            id="connector-transition-reason"
                            data-testid="connector-transition-reason"
                            value={transitionReason}
                            onChange={(e) => setTransitionReason(e.target.value)}
                            placeholder={t('connectors.transitionReasonPlaceholder')}
                            className="w-full rounded border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-100"
                            required
                          />
                        </div>
                        <Button
                          type="submit"
                          variant="primary"
                          disabled={transitionMutation.isPending}
                          data-testid="connector-confirm-transition-btn"
                        >
                          {t('connectors.confirmTransitionButton')}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => {
                            setPendingAction(null)
                            setTransitionReason('')
                          }}
                        >
                          {t('common.cancel')}
                        </Button>
                      </form>
                    )}
                  </div>
                )}

                {actionError && (
                  <div
                    role="alert"
                    data-testid="connector-action-error"
                    className="mt-3 rounded border border-rose-500/50 bg-rose-500/10 p-2.5 text-xs text-rose-200"
                  >
                    {actionError}
                  </div>
                )}

                {/* Non-conflated Status, Health, Freshness & Worker Lease Grid */}
                <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.desiredStateLabel')}
                    </div>
                    <div
                      className="mt-1 font-mono text-xs font-semibold text-slate-100"
                      dir="ltr"
                      data-testid="detail-desired-state"
                    >
                      {selected.desired_state}
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.runtimeStatusLabel')}
                    </div>
                    <div className="mt-1" data-testid="detail-runtime-status">
                      <Badge tone={statusTone(selected.status)}>{selected.status}</Badge>
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.healthStateLabel')}
                    </div>
                    <div className="mt-1" data-testid="detail-health-state">
                      <Badge tone={statusTone(selected.health.health_state)}>
                        {selected.health.is_live ? 'LIVE' : selected.health.health_state}
                      </Badge>
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.dataFreshnessLabel')}
                    </div>
                    <div className="mt-1" data-testid="detail-data-freshness">
                      <Badge tone={statusTone(selected.health.data_freshness)}>
                        {selected.health.data_freshness}
                      </Badge>
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.workerLeaseLabel')}
                    </div>
                    <div
                      dir="ltr"
                      className="mt-1 truncate font-mono text-xs text-slate-200"
                      data-testid="detail-worker-id"
                    >
                      {selected.worker.worker_id ?? 'unleased'}
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.fencingTokenLabel')}
                    </div>
                    <div
                      dir="ltr"
                      className="mt-1 font-mono text-xs text-slate-200"
                      data-testid="detail-fencing-token"
                    >
                      #{selected.worker.fencing_token} (v{selected.config_version})
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.consecutiveFailuresLabel')}
                    </div>
                    <div
                      dir="ltr"
                      className="mt-1 font-mono text-xs text-slate-200"
                      data-testid="detail-consecutive-failures"
                    >
                      {selected.health.consecutive_failures} (backoff:{' '}
                      {selected.health.backoff_seconds}s)
                    </div>
                  </div>

                  <div className="rounded border border-slate-800 bg-slate-950/60 p-2.5">
                    <div className="text-[11px] text-slate-400">
                      {t('connectors.reconnectCountLabel')}
                    </div>
                    <div
                      dir="ltr"
                      className="mt-1 font-mono text-xs text-slate-200"
                      data-testid="detail-reconnect-count"
                    >
                      {selected.health.reconnect_count}
                    </div>
                  </div>
                </div>

                {/* Endpoint, Watermark & Error Diagnostics */}
                <div className="mt-4 space-y-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-slate-400">{t('connectors.endpointLabel')}:</span>
                    <span
                      dir="ltr"
                      className="font-mono text-slate-200"
                      data-testid="detail-endpoint-url"
                    >
                      {selected.endpoint_url ?? 'synthetic://plan'}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-slate-400">
                      {t('connectors.watermarkCursorLabel')}:
                    </span>
                    <code
                      dir="ltr"
                      className="rounded bg-slate-950 px-2 py-0.5 font-mono text-[11px] text-sky-300"
                      data-testid="detail-watermark-cursor"
                    >
                      {JSON.stringify(selected.cursor ?? {})}
                    </code>
                  </div>

                  {selected.health.last_error && (
                    <div
                      className="rounded border border-rose-500/40 bg-rose-950/30 p-2.5 text-rose-200"
                      data-testid="detail-last-error"
                    >
                      <div>
                        <span dir="ltr" className="font-mono font-semibold">
                          [{selected.health.last_error_category ?? 'error'}]
                        </span>{' '}
                        <span dir="ltr">{selected.health.last_error}</span>
                      </div>
                      <div
                        className="mt-1.5 border-t border-rose-500/20 pt-1.5 text-[11px] text-amber-200"
                        data-testid="detail-error-remediation"
                      >
                        <span className="font-semibold">
                          {t('connectors.remediationTitle')}:
                        </span>{' '}
                        {getRemediationText(selected.health.last_error_category)}
                      </div>
                    </div>
                  )}
                </div>

                {/* Masked Secret References */}
                <div className="mt-4 border-t border-slate-800 pt-3">
                  <h3 className="text-xs font-semibold text-slate-200">
                    {t('connectors.secretRefsTitle')}
                  </h3>
                  {Object.keys(selected.secret_refs ?? {}).length === 0 ? (
                    <p className="mt-1 text-xs text-slate-400">
                      {t('connectors.noSecretRefs')}
                    </p>
                  ) : (
                    <div
                      className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2"
                      data-testid="detail-secret-refs"
                    >
                      {Object.values(selected.secret_refs).map((sr) => (
                        <div
                          key={sr.slot}
                          className="flex items-center justify-between rounded border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-xs"
                        >
                          <span dir="ltr" className="font-mono text-slate-300">
                            {sr.slot}: {sr.ref}
                          </span>
                          <span
                            dir="ltr"
                            className="font-mono text-amber-300"
                            data-testid={`masked-secret-${sr.slot}`}
                          >
                            {sr.masked_value}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Channel Mappings Table */}
                <div className="mt-4 border-t border-slate-800 pt-3">
                  <h3 className="text-xs font-semibold text-slate-200">
                    {t('connectors.mappingsTitle')} ({selected.channel_mappings.length})
                  </h3>
                  <div className="mt-2 overflow-x-auto">
                    <table
                      className="w-full text-start text-xs"
                      data-testid="detail-mappings-table"
                    >
                      <thead>
                        <tr className="border-b border-slate-800 text-slate-400">
                          <th className="py-1.5 pe-3 text-start">
                            {t('connectors.sourceMnemonicLabel')}
                          </th>
                          <th className="py-1.5 pe-3 text-start">
                            {t('connectors.targetKeyLabel')}
                          </th>
                          <th className="py-1.5 pe-3 text-start">
                            {t('connectors.channelNameLabel')}
                          </th>
                          <th className="py-1.5 pe-3 text-start">
                            {t('connectors.dimensionLabel')}
                          </th>
                          <th className="py-1.5 text-start">{t('connectors.unitLabel')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selected.channel_mappings.map((m) => (
                          <tr key={m.channel_key} className="border-b border-slate-900">
                            <td className="py-1.5 pe-3 font-mono text-sky-300" dir="ltr">
                              {m.source_mnemonic}
                            </td>
                            <td className="py-1.5 pe-3 font-mono text-slate-200" dir="ltr">
                              {m.channel_key}
                            </td>
                            <td className="py-1.5 pe-3 text-slate-200">{m.name}</td>
                            <td className="py-1.5 pe-3 font-mono text-slate-300" dir="ltr">
                              {m.dimension}
                            </td>
                            <td className="py-1.5 font-mono text-slate-300" dir="ltr">
                              {m.unit}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </Card>

              {/* Connection Test Result */}
              {testResult && (
                <Card className="p-4" data-testid="connector-test-result">
                  <div className="flex items-center justify-between">
                    <h3 className="text-xs font-semibold text-slate-100">
                      {t('connectors.testResultTitle')}
                    </h3>
                    <Badge tone={testResult.ok ? 'ok' : 'danger'}>
                      {testResult.ok
                        ? t('connectors.testSucceeded')
                        : t('connectors.testFailed')}
                    </Badge>
                  </div>
                  <div className="mt-2 text-xs text-slate-300">
                    {testResult.ok ? (
                      <div>
                        Discovered {testResult.discovered_channels.length} channels in{' '}
                        <span dir="ltr" className="font-mono">
                          {testResult.duration_ms} ms
                        </span>
                        .
                      </div>
                    ) : (
                      <div className="text-rose-300" dir="ltr">
                        [{testResult.error_category}] {testResult.error_message}
                      </div>
                    )}
                  </div>
                  {testResult.milestones && testResult.milestones.length > 0 && (
                    <div
                      className="mt-3 border-t border-slate-800 pt-2.5"
                      data-testid="connector-test-milestones"
                    >
                      <div className="mb-1.5 text-[11px] font-semibold text-slate-300">
                        {t('connectors.testMilestonesTitle')}
                      </div>
                      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                        {testResult.milestones.map((m) => (
                          <div
                            key={m.stage}
                            data-testid={`test-milestone-${m.stage}`}
                            className="flex items-center justify-between gap-2 rounded border border-slate-800/80 bg-slate-950/60 px-2 py-1 text-[11px]"
                          >
                            <span dir="ltr" className="font-mono text-slate-300">
                              {m.stage}
                            </span>
                            <Badge
                              tone={
                                m.status === 'passed'
                                  ? 'ok'
                                  : m.status === 'failed'
                                    ? 'danger'
                                    : 'neutral'
                              }
                            >
                              {m.status}
                            </Badge>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </Card>
              )}

              {/* Sample Preview Result */}
              {previewResult && (
                <Card className="p-4" data-testid="connector-preview-result">
                  <h3 className="text-xs font-semibold text-slate-100">
                    {t('connectors.previewResultTitle')} ({previewResult.sample_count})
                  </h3>
                  <div className="mt-2 max-h-48 overflow-y-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-slate-800 text-slate-400">
                          <th className="py-1 text-start">Channel</th>
                          <th className="py-1 text-start">Timestamp</th>
                          <th className="py-1 text-start">Value</th>
                          <th className="py-1 text-start">Unit</th>
                          <th className="py-1 text-start">Quality</th>
                        </tr>
                      </thead>
                      <tbody>
                        {previewResult.samples.map((s, idx) => (
                          <tr key={idx} className="border-b border-slate-900">
                            <td className="py-1 font-mono text-sky-300" dir="ltr">
                              {s.channel_key}
                            </td>
                            <td className="py-1 font-mono text-slate-300" dir="ltr">
                              {s.ts}
                            </td>
                            <td className="py-1 font-mono text-slate-100" dir="ltr">
                              {s.value ?? 'NULL'}
                            </td>
                            <td className="py-1 font-mono text-slate-300" dir="ltr">
                              {s.unit}
                            </td>
                            <td className="py-1 font-mono text-slate-300" dir="ltr">
                              {s.quality}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}

              {/* Governed Replay & Backfill Workbench */}
              {canControl && (
                <Card className="p-4" data-testid="connector-replay-card">
                  <h3 className="text-xs font-semibold text-slate-100">
                    {t('connectors.replayTitle')}
                  </h3>
                  <p className="mt-1 text-xs text-slate-400">
                    {t('connectors.replaySubtitle')}
                  </p>
                  <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div>
                      <label
                        htmlFor="connector-replay-from-ts"
                        className="mb-1 block text-xs text-slate-300"
                      >
                        {t('connectors.replayFromTsLabel')}
                      </label>
                      <input
                        id="connector-replay-from-ts"
                        data-testid="connector-replay-from-ts"
                        dir="ltr"
                        value={replayFromTs}
                        onChange={(e) => setReplayFromTs(e.target.value)}
                        placeholder={t('connectors.replayFromTsPlaceholder')}
                        className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 font-mono text-xs text-slate-100"
                      />
                    </div>
                    <div>
                      <label
                        htmlFor="connector-replay-reason"
                        className="mb-1 block text-xs text-slate-300"
                      >
                        {t('connectors.replayReasonLabel')}
                      </label>
                      <input
                        id="connector-replay-reason"
                        data-testid="connector-replay-reason"
                        value={replayReason}
                        onChange={(e) => setReplayReason(e.target.value)}
                        placeholder={t('connectors.replayReasonPlaceholder')}
                        className="w-full rounded border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-100"
                      />
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <Button
                      variant="default"
                      disabled={replayMutation.isPending}
                      onClick={() =>
                        replayMutation.mutate({ connector: selected, dryRun: true })
                      }
                      data-testid="connector-replay-dryrun-btn"
                    >
                      {t('connectors.replayDryRunBtn')}
                    </Button>
                    <Button
                      variant="primary"
                      disabled={replayMutation.isPending}
                      onClick={() =>
                        replayMutation.mutate({ connector: selected, dryRun: false })
                      }
                      data-testid="connector-replay-execute-btn"
                    >
                      {t('connectors.replayExecuteBtn')}
                    </Button>
                  </div>

                  {replayResult && replayResult.connector_id === selected.id && (
                    <div
                      className="mt-3 rounded border border-sky-500/30 bg-sky-950/20 p-3 text-xs"
                      data-testid="connector-replay-result"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-semibold text-slate-100">
                          {t('connectors.replayResultTitle')}
                        </span>
                        <Badge tone={replayResult.dry_run ? 'info' : 'ok'}>
                          {replayResult.dry_run
                            ? t('connectors.replayDryRunBadge')
                            : t('connectors.replayLiveBadge')}
                        </Badge>
                      </div>
                      <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-slate-200" dir="ltr">
                        <div>frames: {replayResult.run.frames_received}</div>
                        <div>accepted: {replayResult.run.points_accepted}</div>
                        <div>duplicates: {replayResult.run.points_duplicates}</div>
                      </div>
                    </div>
                  )}
                </Card>
              )}

              {/* Bounded Connector Run Ledger & Disposition Manifest Inspector */}
              <Card className="p-4" data-testid="connector-runs-card">
                <h3 className="text-xs font-semibold text-slate-100">
                  {t('connectors.runsTitle')} ({selectedRunsQuery.data?.total ?? 0})
                </h3>
                {!selectedRunsQuery.data?.items?.length ? (
                  <p className="mt-2 text-xs text-slate-400">{t('connectors.noRuns')}</p>
                ) : (
                  <>
                    <div className="mt-2 overflow-x-auto">
                      <table
                        className="w-full text-xs"
                        data-testid="connector-runs-table"
                      >
                        <thead>
                          <tr className="border-b border-slate-800 text-slate-400">
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runKindCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runStatusCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runFramesCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runAcceptedCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runDuplicatesCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runDurationCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runTokenCol')}
                            </th>
                            <th className="py-1.5 pe-2 text-start">
                              {t('connectors.runErrorCol')}
                            </th>
                            <th className="py-1.5 text-end">
                              {t('connectors.inspectRunBtn')}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedRunsQuery.data.items.map((run) => {
                            const isInspected = inspectedRun?.id === run.id
                            return (
                              <tr
                                key={run.id}
                                className={`border-b border-slate-900 ${
                                  isInspected ? 'bg-sky-950/30' : ''
                                }`}
                              >
                                <td
                                  className="py-1.5 pe-2 font-mono text-slate-300"
                                  dir="ltr"
                                >
                                  {run.run_kind}
                                </td>
                                <td className="py-1.5 pe-2">
                                  <Badge tone={statusTone(run.status)}>
                                    {run.status}
                                  </Badge>
                                </td>
                                <td
                                  className="py-1.5 pe-2 font-mono text-slate-200"
                                  dir="ltr"
                                >
                                  {run.frames_received}
                                </td>
                                <td
                                  className="py-1.5 pe-2 font-mono text-slate-200"
                                  dir="ltr"
                                >
                                  {run.points_accepted}
                                </td>
                                <td
                                  className="py-1.5 pe-2 font-mono text-slate-400"
                                  dir="ltr"
                                >
                                  {run.points_duplicates}
                                </td>
                                <td
                                  className="py-1.5 pe-2 font-mono text-slate-300"
                                  dir="ltr"
                                >
                                  {run.duration_ms ?? 0} ms
                                </td>
                                <td
                                  className="py-1.5 pe-2 font-mono text-slate-400"
                                  dir="ltr"
                                >
                                  #{run.fencing_token}
                                </td>
                                <td
                                  className="py-1.5 pe-2 font-mono text-rose-300"
                                  dir="ltr"
                                >
                                  {run.error_category
                                    ? `[${run.error_category}] ${run.error_message ?? ''}`
                                    : '—'}
                                </td>
                                <td className="py-1.5 text-end">
                                  <button
                                    type="button"
                                    data-testid={`inspect-run-${run.id}`}
                                    onClick={() => setInspectedRunId(run.id)}
                                    className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[11px] text-sky-300 hover:border-sky-500"
                                  >
                                    {t('connectors.inspectRunBtn')}
                                  </button>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>

                    {/* Selected Run Data Disposition & Checkpoint Manifest */}
                    {inspectedRun && (
                      <div
                        className="mt-4 rounded border border-slate-800 bg-slate-950/80 p-3 text-xs"
                        data-testid="selected-run-manifest"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-slate-100">
                              {t('connectors.manifestTitle')}
                            </span>
                            <span dir="ltr" className="font-mono text-slate-400">
                              ({inspectedRun.id})
                            </span>
                          </div>
                          {inspectedManifest && (
                            <div className="flex flex-wrap items-center gap-1.5">
                              {inspectedManifest.data_outcome && (
                                <Badge
                                  tone={
                                    inspectedManifest.data_outcome ===
                                    'all_valid_accepted'
                                      ? 'ok'
                                      : inspectedManifest.data_outcome ===
                                          'rejected_only'
                                        ? 'danger'
                                        : inspectedManifest.data_outcome ===
                                              'partial_with_rejections' ||
                                            inspectedManifest.data_outcome ===
                                              'partial_batch_capped'
                                          ? 'warning'
                                          : 'neutral'
                                  }
                                  data-testid="manifest-data-outcome"
                                >
                                  {inspectedManifest.data_outcome ===
                                  'all_valid_accepted'
                                    ? t('connectors.dataOutcomeAllValid')
                                    : inspectedManifest.data_outcome ===
                                        'partial_with_rejections'
                                      ? t('connectors.dataOutcomePartialRejections')
                                      : inspectedManifest.data_outcome ===
                                          'rejected_only'
                                        ? t('connectors.dataOutcomeRejectedOnly')
                                        : inspectedManifest.data_outcome ===
                                            'partial_batch_capped'
                                          ? t('connectors.dataOutcomePartialCapped')
                                          : t('connectors.dataOutcomeNoData')}
                                </Badge>
                              )}
                              <Badge
                                tone={inspectedManifest.reconciled ? 'ok' : 'warning'}
                              >
                                {inspectedManifest.reconciled
                                  ? t('connectors.manifestReconciled')
                                  : t('connectors.manifestNotReconciled')}
                              </Badge>
                              {inspectedManifest.checkpoint_status && (
                                <Badge tone="info">
                                  {inspectedManifest.checkpoint_status}
                                </Badge>
                              )}
                            </div>
                          )}
                        </div>

                        <div className="mt-2.5 grid grid-cols-2 gap-2 sm:grid-cols-4">
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestDiscovered')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-slate-100"
                              data-testid="manifest-discovered"
                            >
                              {inspectedManifest?.source_records_discovered ??
                                inspectedRun.frames_received}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestEmitted')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-slate-100"
                              data-testid="manifest-emitted"
                            >
                              {inspectedManifest?.mapped_frames_emitted ??
                                inspectedRun.frames_received}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestAccepted')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-emerald-300"
                              data-testid="manifest-accepted"
                            >
                              {inspectedRun.points_accepted}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestDuplicates')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-slate-200"
                              data-testid="manifest-duplicates"
                            >
                              {inspectedRun.points_duplicates}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestRejected')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-rose-300"
                              data-testid="manifest-rejected"
                            >
                              {inspectedRun.points_rejected}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestUnmapped')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-slate-300"
                              data-testid="manifest-unmapped"
                            >
                              {inspectedManifest?.unmapped_skipped ?? 0}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestWatermarkSuppressed')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-slate-300"
                              data-testid="manifest-watermark-suppressed"
                            >
                              {inspectedManifest?.watermark_suppressed ?? 0}
                            </div>
                          </div>
                          <div className="rounded border border-slate-800/80 bg-slate-900/60 p-2">
                            <div className="text-[11px] text-slate-400">
                              {t('connectors.manifestDeferredOverCap')}
                            </div>
                            <div
                              dir="ltr"
                              className="mt-0.5 font-mono font-semibold text-amber-300"
                              data-testid="manifest-deferred-over-cap"
                            >
                              {inspectedManifest?.deferred_over_cap ?? 0}
                            </div>
                          </div>
                        </div>

                        <div className="mt-2.5 space-y-1 text-[11px]">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-slate-400">
                              {t('connectors.cursorBeforeLabel')}:
                            </span>
                            <code
                              dir="ltr"
                              className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-slate-300"
                              data-testid="manifest-cursor-before"
                            >
                              {JSON.stringify(inspectedRun.cursor_before ?? {})}
                            </code>
                          </div>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-slate-400">
                              {t('connectors.cursorAfterLabel')}:
                            </span>
                            <code
                              dir="ltr"
                              className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-sky-300"
                              data-testid="manifest-cursor-after"
                            >
                              {JSON.stringify(inspectedRun.cursor_after ?? {})}
                            </code>
                          </div>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default ConnectorsPage
