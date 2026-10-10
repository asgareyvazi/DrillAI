/**
 * Operational Monitor & Alert Workflow for a well (`WellCockpit`).
 *
 * Implements the CP10 live operations vertical slice:
 * - Transport state indicator (`LIVE | RECONNECTING | RESYNCING | POLLING FALLBACK | STALE | DISCONNECTED | ERROR`)
 * - Temporal `as_of` stamps (`telemetry_as_of`, `alerts_as_of`, `operation_as_of`) and per-well stream cursor
 * - Live Drilling KPI strip (never renders missing channels as 0; flags untrustworthy quality and stale data;
 *   never labels `insufficient_data` trends as flat/stable)
 * - Bounded SVG channel history chart with explicit gap breaks, quality markers, and downsample/truncation badges
 * - Deterministic Alert Workflow driven strictly by server `allowed_transitions`, `expected_updated_at`
 *   optimistic concurrency, required reasons on `clear`/`cancel`, and `Idempotency-Key`
 * - Historical T1 Alert Evidence Drawer (`GET /alerts/{id}/evidence`) with deep link to Operations Advisor
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import type {
  AlertRow,
  DrillingState,
  TelemetryLatestReading,
  TelemetryPoint,
  TelemetryTrend,
} from '../../api/types'
import { Badge, Button, Card, EmptyState, ErrorState } from '../../components/common'
import { useWellLiveStream } from '../../hooks/useWellLiveStream'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber, formatStatus, humanise } from '../../lib/format'
import type { WellTransportState } from '../../lib/wellLiveStream'

const CANONICAL_KPI_ORDER = [
  'spp',
  'wob',
  'rpm',
  'flow_rate',
  'torque',
  'hookload',
  'rop',
  'ecd',
] as const

function transportBadgeTone(state: WellTransportState): 'ok' | 'warning' | 'danger' | 'info' | 'neutral' {
  switch (state) {
    case 'LIVE':
      return 'ok'
    case 'RESYNCING':
    case 'RECONNECTING':
      return 'info'
    case 'POLLING FALLBACK':
    case 'STALE':
      return 'warning'
    case 'ERROR':
      return 'danger'
    case 'DISCONNECTED':
    default:
      return 'neutral'
  }
}

function severityRank(severity: string): number {
  switch (severity) {
    case 'critical':
      return 4
    case 'high':
      return 3
    case 'medium':
      return 2
    case 'low':
      return 1
    default:
      return 0
  }
}

function severityTone(severity: string): 'danger' | 'warning' | 'info' | 'neutral' {
  switch (severity) {
    case 'critical':
    case 'high':
      return 'danger'
    case 'medium':
      return 'warning'
    case 'low':
      return 'info'
    default:
      return 'neutral'
  }
}

function freshnessTone(freshness: string): 'ok' | 'warning' | 'neutral' {
  if (freshness === 'live') return 'ok'
  if (freshness === 'stale') return 'warning'
  return 'neutral'
}

function qualityTone(quality: string): 'ok' | 'warning' | 'danger' | 'neutral' {
  if (quality === 'good' || quality === 'estimated') return 'ok'
  if (quality === 'suspect') return 'warning'
  if (quality === 'bad') return 'danger'
  return 'neutral'
}

function makeIdempotencyKey(prefix: string, id: string): string {
  return `${prefix}-${id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

// --------------------------------------------------------------------------- Bounded SVG Chart

function ChannelHistoryChart({
  points,
  unit,
  downsampled,
  truncated,
  totalInWindow,
}: {
  points: TelemetryPoint[]
  unit: string
  downsampled: boolean
  truncated: boolean
  totalInWindow: number
}) {
  const { t, locale } = useI18n()

  const ordered = useMemo(
    () =>
      [...points].sort(
        (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
      ),
    [points],
  )

  const geometry = useMemo(() => {
    const validValues = ordered
      .filter((pt) => pt.value !== null && Number.isFinite(pt.value) && pt.quality !== 'bad')
      .map((pt) => pt.value as number)
    if (ordered.length === 0 || validValues.length === 0) {
      return null
    }
    const minVal = Math.min(...validValues)
    const maxVal = Math.max(...validValues)
    const spanVal = maxVal - minVal || Math.max(Math.abs(maxVal) * 0.1, 1)
    const low = minVal - spanVal * 0.1
    const high = maxVal + spanVal * 0.1

    const times = ordered.map((pt) => new Date(pt.ts).getTime())
    const minTs = Math.min(...times)
    const maxTs = Math.max(...times)
    const spanTs = Math.max(maxTs - minTs, 1000)

    // Estimate median step to detect genuine time gaps (> 4x median step and > 60s)
    const diffs: number[] = []
    for (let i = 1; i < times.length; i += 1) {
      const tCurr = times[i] ?? 0
      const tPrev = times[i - 1] ?? 0
      const d = tCurr - tPrev
      if (d > 0) diffs.push(d)
    }
    diffs.sort((a, b) => a - b)
    const medianDiff =
      diffs.length > 0 ? (diffs[Math.floor(diffs.length / 2)] ?? 10_000) : 10_000
    const gapThresholdMs = Math.max(60_000, medianDiff * 4)

    const width = 640
    const height = 180
    const padX = 16
    const padY = 16
    const plotW = width - padX * 2
    const plotH = height - padY * 2

    const segments: string[][] = []
    let currentSegment: string[] = []
    let gapCount = 0
    const plotted = ordered.map((pt, idx) => {
      const tsMs = times[idx] ?? minTs
      const prevTsMs = idx > 0 ? (times[idx - 1] ?? minTs) : tsMs
      const x =
        ordered.length === 1
          ? width / 2
          : padX + ((tsMs - minTs) / spanTs) * plotW
      const usable =
        pt.value !== null &&
        Number.isFinite(pt.value) &&
        pt.quality !== 'bad' &&
        pt.quality !== 'missing'
      const y = usable
        ? padY + plotH - (((pt.value as number) - low) / (high - low)) * plotH
        : padY + plotH / 2

      if (idx > 0 && tsMs - prevTsMs > gapThresholdMs) {
        gapCount += 1
        if (currentSegment.length > 0) {
          segments.push(currentSegment)
          currentSegment = []
        }
      }

      if (usable) {
        currentSegment.push(`${x.toFixed(1)},${y.toFixed(1)}`)
      } else if (currentSegment.length > 0) {
        segments.push(currentSegment)
        currentSegment = []
      }

      return { pt, x, y, usable }
    })
    if (currentSegment.length > 0) {
      segments.push(currentSegment)
    }

    return {
      width,
      height,
      minVal,
      maxVal,
      segments: segments.filter((seg) => seg.length >= 2),
      plotted,
      gapCount,
    }
  }, [ordered])

  if (ordered.length === 0) {
    return <EmptyState message={t('liveMonitor.noChannelPoints')} />
  }

  return (
    <div className="space-y-3" data-testid="channel-history-chart">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-graphite-500">
        <span>
          {t('liveMonitor.windowPointsCount', {
            shown: ordered.length,
            total: totalInWindow,
          })}
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          {downsampled && <Badge tone="info">{t('liveMonitor.downsampledBadge')}</Badge>}
          {truncated && <Badge tone="warning">{t('liveMonitor.truncatedBadge')}</Badge>}
          {geometry && geometry.gapCount > 0 && (
            <Badge tone="warning">
              {geometry.gapCount} gap{geometry.gapCount > 1 ? 's' : ''}
            </Badge>
          )}
        </div>
      </div>

      {geometry && (
        <div className="rounded-md border border-graphite-100 bg-graphite-50/50 p-2 dark:border-graphite-800 dark:bg-graphite-900/40">
          <div className="mb-1 flex items-center justify-between font-mono text-[11px] text-graphite-500" dir="ltr">
            <span>
              max: {formatNumber(geometry.maxVal, locale, 2)} {unit}
            </span>
            <span>
              min: {formatNumber(geometry.minVal, locale, 2)} {unit}
            </span>
          </div>
          <svg
            viewBox={`0 0 ${geometry.width} ${geometry.height}`}
            className="h-40 w-full overflow-visible"
            role="img"
            aria-label={`${t('liveMonitor.historyChartTitle')} (${ordered.length})`}
          >
            {geometry.segments.map((seg, idx) => (
              <polyline
                key={idx}
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className="text-signal-deep dark:text-signal-light"
                points={seg.join(' ')}
              />
            ))}
            {geometry.plotted.map(({ pt, x, y, usable }) => (
              <circle
                key={pt.id}
                cx={x}
                cy={y}
                r={usable ? 3.5 : 4.5}
                className={
                  !usable
                    ? 'fill-danger stroke-white dark:stroke-graphite-900'
                    : pt.is_late || pt.is_out_of_order
                      ? 'fill-warning stroke-white dark:stroke-graphite-900'
                      : 'fill-signal-deep dark:fill-signal-light'
                }
              />
            ))}
          </svg>
        </div>
      )}

      <div className="max-h-52 overflow-auto rounded-md border border-graphite-100 dark:border-graphite-800">
        <table className="w-full text-start text-xs">
          <thead className="bg-graphite-50 text-[11px] text-graphite-500 uppercase dark:bg-graphite-900">
            <tr>
              <th className="px-2.5 py-1.5 text-start">{t('liveMonitor.pointTimestamp')}</th>
              <th className="px-2.5 py-1.5 text-end">{t('liveMonitor.pointValue')}</th>
              <th className="px-2.5 py-1.5 text-start">{t('liveMonitor.pointQuality')}</th>
              <th className="px-2.5 py-1.5 text-start">{t('liveMonitor.pointFlags')}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-graphite-100 dark:divide-graphite-800">
            {[...ordered].reverse().slice(0, 15).map((pt) => (
              <tr key={pt.id}>
                <td className="px-2.5 py-1.5 font-mono text-[11px]" dir="ltr">
                  {formatDateTime(pt.ts, locale)}
                </td>
                <td className="px-2.5 py-1.5 text-end font-mono text-[11px]" dir="ltr">
                  {pt.value !== null && pt.quality !== 'bad' && pt.quality !== 'missing' ? (
                    <>
                      {formatNumber(pt.value, locale, 2)} <span className="text-graphite-500">{unit}</span>
                    </>
                  ) : (
                    <span className="text-danger">{t('liveMonitor.untrustworthyReading')}</span>
                  )}
                </td>
                <td className="px-2.5 py-1.5">
                  <Badge tone={qualityTone(pt.quality)}>{pt.quality}</Badge>
                </td>
                <td className="px-2.5 py-1.5">
                  <span className="inline-flex flex-wrap gap-1">
                    {pt.is_late && <Badge tone="warning">{t('liveMonitor.lateBadge')}</Badge>}
                    {pt.is_out_of_order && (
                      <Badge tone="warning">{t('liveMonitor.outOfOrderBadge')}</Badge>
                    )}
                    {!pt.is_late && !pt.is_out_of_order && (
                      <span className="text-graphite-400">—</span>
                    )}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------- Alert Evidence Drawer

function AlertEvidenceDrawer({
  alertId,
  wellId,
  onClose,
}: {
  alertId: string | null
  wellId: string
  onClose: () => void
}) {
  const { t, locale } = useI18n()
  const titleId = useId()

  const evidenceQuery = useQuery({
    queryKey: ['alert-evidence', alertId],
    queryFn: ({ signal }) => drillingApi.alertEvidence(alertId as string, { points: 25 }, signal),
    enabled: Boolean(alertId),
  })

  useEffect(() => {
    if (!alertId) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [alertId, onClose])

  if (!alertId) return null

  const data = evidenceQuery.data
  const ruleSnap = (data?.rule ?? data?.rule_snapshot ?? null) as Record<string, unknown> | null

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/40 backdrop-blur-[1px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      data-testid="alert-evidence-drawer"
    >
      <div className="flex h-full w-full max-w-2xl flex-col overflow-hidden bg-white shadow-xl dark:bg-graphite-900">
        <div className="flex items-center justify-between border-b border-graphite-200 px-4 py-3 dark:border-graphite-800">
          <div>
            <h2 id={titleId} className="text-base font-semibold">
              {t('liveMonitor.evidenceDrawerTitle')}
            </h2>
            <p className="font-mono text-xs text-graphite-500" dir="ltr">
              {alertId}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Link
              to={`/wells/${wellId}/advisor?alert=${encodeURIComponent(alertId)}`}
              data-testid="alert-evidence-advisor-link"
            >
              <Button size="sm" variant="default">
                {t('liveMonitor.askAdvisorLink')}
              </Button>
            </Link>
            <Button size="sm" onClick={onClose} data-testid="close-alert-evidence-btn">
              {t('common.close')}
            </Button>
          </div>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          {evidenceQuery.isLoading && (
            <p className="text-sm text-graphite-500">{t('common.loading')}</p>
          )}
          {evidenceQuery.isError && (
            <ErrorState error={evidenceQuery.error} onRetry={() => evidenceQuery.refetch()} />
          )}
          {data && (
            <>
              <Card title={data.alert.title}>
                <div className="space-y-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={severityTone(data.alert.severity)}>
                      {humanise(data.alert.severity)}
                    </Badge>
                    <Badge tone={data.alert.status === 'cleared' ? 'ok' : 'warning'}>
                      {formatStatus(data.alert.status)}
                    </Badge>
                    {data.provenance.channel_key && (
                      <Badge tone="info">
                        <span dir="ltr">{data.provenance.channel_key}</span>
                      </Badge>
                    )}
                  </div>
                  {data.alert.description && (
                    <p className="text-graphite-600 dark:text-graphite-300">
                      {data.alert.description}
                    </p>
                  )}
                </div>
              </Card>

              <Card title={t('liveMonitor.evidenceRuleSection')}>
                {ruleSnap ? (
                  <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                    <div>
                      <dt className="text-graphite-500">{t('liveMonitor.ruleKeyLabel')}</dt>
                      <dd className="font-mono font-medium" dir="ltr">
                        {String(ruleSnap.rule_key ?? data.provenance.rule_ref ?? '—')}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-graphite-500">{t('liveMonitor.comparatorLabel')}</dt>
                      <dd className="font-mono font-medium" dir="ltr">
                        {String(ruleSnap.operator ?? '—')}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-graphite-500">{t('liveMonitor.thresholdLabel')}</dt>
                      <dd className="font-mono font-medium" dir="ltr">
                        {data.observed.declared_threshold !== null &&
                        data.observed.declared_threshold !== undefined
                          ? `${formatNumber(data.observed.declared_threshold, locale, 2)} ${data.observed.declared_unit ?? ''}`
                          : `${formatNumber(data.observed.threshold, locale, 2)} ${data.observed.unit ?? ''}`}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-graphite-500">{t('liveMonitor.clearLineLabel')}</dt>
                      <dd className="font-mono font-medium" dir="ltr">
                        {String(ruleSnap.clear_operator ?? '—')}{' '}
                        {ruleSnap.clear_threshold !== null && ruleSnap.clear_threshold !== undefined
                          ? String(ruleSnap.clear_threshold)
                          : 'auto'}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-graphite-500">{t('liveMonitor.sustainLabel')}</dt>
                      <dd className="font-mono" dir="ltr">
                        {String(ruleSnap.sustain_seconds ?? 0)}s
                      </dd>
                    </div>
                    <div>
                      <dt className="text-graphite-500">{t('liveMonitor.cooldownLabel')}</dt>
                      <dd className="font-mono" dir="ltr">
                        {String(ruleSnap.cooldown_seconds ?? 0)}s
                      </dd>
                    </div>
                  </dl>
                ) : (
                  <p className="text-xs text-graphite-500">{t('common.unknown')}</p>
                )}
              </Card>

              <Card title={t('liveMonitor.evidenceObservedSection')}>
                <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                  <div>
                    <dt className="text-graphite-500">{t('liveMonitor.observedValueLabel')}</dt>
                    <dd
                      className="font-mono font-semibold"
                      dir="ltr"
                      data-testid="evidence-observed-value"
                    >
                      {formatNumber(data.observed.value, locale, 2)} {data.observed.unit ?? ''}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('liveMonitor.observedAtLabel')}</dt>
                    <dd
                      className="font-mono"
                      dir="ltr"
                      data-testid="evidence-observed-at"
                    >
                      {formatDateTime(data.observed.observed_at, locale)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('liveMonitor.clearObservedValueLabel')}</dt>
                    <dd className="font-mono" dir="ltr">
                      {data.observed.clear_observed_value !== null
                        ? `${formatNumber(data.observed.clear_observed_value, locale, 2)} ${data.observed.unit ?? ''}`
                        : '—'}
                    </dd>
                  </div>
                </dl>
              </Card>

              <Card
                title={t('liveMonitor.evidencePointsSection')}
                subtitle={t('liveMonitor.evidencePointsNote')}
              >
                {data.points.length === 0 ? (
                  <EmptyState message={t('liveMonitor.noChannelPoints')} />
                ) : (
                  <div className="max-h-48 overflow-auto">
                    <table
                      className="w-full text-start text-xs"
                      data-testid="evidence-points-table"
                    >
                      <thead className="text-[11px] text-graphite-500 uppercase">
                        <tr>
                          <th className="px-2 py-1 text-start">{t('liveMonitor.pointTimestamp')}</th>
                          <th className="px-2 py-1 text-end">{t('liveMonitor.pointValue')}</th>
                          <th className="px-2 py-1 text-start">{t('liveMonitor.pointQuality')}</th>
                          <th className="px-2 py-1 text-start">{t('liveMonitor.pointFlags')}</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-graphite-100 dark:divide-graphite-800">
                        {data.points.map((pt) => (
                          <tr key={pt.id}>
                            <td className="px-2 py-1 font-mono text-[11px]" dir="ltr">
                              {formatDateTime(pt.ts, locale)}
                            </td>
                            <td className="px-2 py-1 text-end font-mono text-[11px]" dir="ltr">
                              {formatNumber(pt.value, locale, 2)} {data.observed.unit ?? ''}
                            </td>
                            <td className="px-2 py-1">
                              <Badge tone={qualityTone(pt.quality)}>{pt.quality}</Badge>
                            </td>
                            <td className="px-2 py-1">
                              {(pt.quality_flags ?? []).length > 0 ? (
                                <span className="inline-flex gap-1" dir="ltr">
                                  {(pt.quality_flags ?? []).map((flag) => (
                                    <Badge key={flag} tone="warning">
                                      {flag}
                                    </Badge>
                                  ))}
                                </span>
                              ) : (
                                '—'
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              <Card title={t('liveMonitor.evidenceTimelineSection')}>
                <ul className="space-y-1.5 text-xs" data-testid="evidence-timeline-list">
                  {data.timeline.map((step, idx) => (
                    <li
                      key={`${step.state}-${idx}`}
                      className="flex flex-wrap items-center justify-between gap-2 rounded border border-graphite-100 px-2.5 py-1.5 dark:border-graphite-800"
                    >
                      <div className="flex items-center gap-2">
                        <Badge tone={step.state === 'cleared' ? 'ok' : 'info'}>
                          {formatStatus(step.state)}
                        </Badge>
                        {step.by && (
                          <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
                            {step.by}
                          </span>
                        )}
                        {step.reason && <span>{step.reason}</span>}
                      </div>
                      <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
                        {formatDateTime(step.at, locale)}
                      </span>
                    </li>
                  ))}
                </ul>
              </Card>

              {data.audit_events && data.audit_events.length > 0 && (
                <Card title={t('liveMonitor.evidenceAuditSection')}>
                  <ul className="space-y-1 text-xs" data-testid="evidence-audit-list">
                    {data.audit_events.map((aud) => (
                      <li
                        key={aud.id}
                        className="flex flex-wrap items-center justify-between gap-2 border-b border-graphite-100 py-1 last:border-b-0 dark:border-graphite-800"
                      >
                        <span className="font-mono text-[11px]" dir="ltr">
                          {aud.action} · {aud.actor_id ?? aud.actor_kind ?? 'system'}
                        </span>
                        <span className="text-graphite-500">{aud.reason || '—'}</span>
                        <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
                          {formatDateTime(aud.occurred_at, locale)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </Card>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------- Main Operational Monitor

export function OperationalMonitor({
  wellId,
  drillingState,
}: {
  wellId: string
  drillingState?: DrillingState | null
}) {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [selectedChannelKey, setSelectedChannelKey] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [severityFilter, setSeverityFilter] = useState<string>('all')
  const [channelFilter, setChannelFilter] = useState<string>('all')
  const [sortBy, setSortBy] = useState<'severity' | 'newest'>('severity')

  const [activeTransition, setActiveTransition] = useState<{
    alertId: string
    target: 'acknowledged' | 'cleared' | 'cancelled'
  } | null>(null)
  const [transitionReason, setTransitionReason] = useState<string>('')
  const [transitionError, setTransitionError] = useState<string | null>(null)
  const [conflictBanner, setConflictBanner] = useState<string | null>(null)

  const selectedAlertId = searchParams.get('alert')
  const setSelectedAlertId = (alertId: string | null) => {
    const next = new URLSearchParams(searchParams)
    if (alertId) next.set('alert', alertId)
    else next.delete('alert')
    setSearchParams(next, { replace: true })
  }

  // Queries
  const latestQuery = useQuery({
    queryKey: ['well-telemetry-latest', wellId],
    queryFn: ({ signal }) => drillingApi.wellLatestTelemetry(wellId, { limit: 100 }, signal),
  })

  const alertsQuery = useQuery({
    queryKey: ['alerts', wellId, statusFilter, severityFilter],
    queryFn: ({ signal }) =>
      drillingApi.listAlerts(
        {
          well_id: wellId,
          status: statusFilter === 'all' ? undefined : statusFilter,
          severity: severityFilter === 'all' ? undefined : severityFilter,
          limit: 100,
        },
        signal,
      ),
  })

  const liveStream = useWellLiveStream(wellId, {
    enabled: true,
    latestReadings: latestQuery.data?.items,
  })

  const readings: TelemetryLatestReading[] = useMemo(() => {
    const raw = latestQuery.data?.items ?? liveStream.snapshot?.latest ?? []
    const rankMap = new Map<string, number>(
      CANONICAL_KPI_ORDER.map((k, idx) => [k, idx]),
    )
    return [...raw].sort((a, b) => {
      const ra = rankMap.get(a.channel_key) ?? 999
      const rb = rankMap.get(b.channel_key) ?? 999
      if (ra !== rb) return ra - rb
      return a.channel_key.localeCompare(b.channel_key)
    })
  }, [latestQuery.data?.items, liveStream.snapshot?.latest])

  const effectiveChannel = useMemo(() => {
    if (readings.length === 0) return null
    if (selectedChannelKey) {
      const found = readings.find((r) => r.channel_key === selectedChannelKey)
      if (found) return found
    }
    return readings[0]
  }, [readings, selectedChannelKey])

  const windowQuery = useQuery({
    queryKey: ['timeseries-points', wellId, effectiveChannel?.channel_id],
    queryFn: ({ signal }) =>
      drillingApi.getTimeseriesPoints(
        effectiveChannel!.channel_id,
        { limit: 120 },
        signal,
      ),
    enabled: Boolean(effectiveChannel?.channel_id),
  })

  const evaluateMutation = useMutation({
    mutationFn: () => drillingApi.evaluateWellAlerts(wellId, { mode: 'manual' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['alerts', wellId] })
      void queryClient.invalidateQueries({ queryKey: ['well-live-snapshot', wellId] })
    },
  })

  const commissionMutation = useMutation({
    mutationFn: () => {
      const startIso = new Date(Date.now() - 15_000).toISOString()
      return drillingApi.commissionSyntheticTelemetry(
        wellId,
        {
          channels: [
            {
              channel_key: 'spp',
              name: 'Standpipe Pressure',
              dimension: 'pressure',
              unit: 'psi',
              values: [3650, 4180, 4290],
              start: startIso,
              step_seconds: 5,
            },
            {
              channel_key: 'wob',
              name: 'Weight on Bit',
              dimension: 'force',
              unit: 'klbf',
              values: [24, 26, 25],
              start: startIso,
              step_seconds: 5,
            },
            {
              channel_key: 'rpm',
              name: 'Rotary Speed',
              dimension: 'rotary_speed',
              unit: 'rpm',
              values: [115, 118, 120],
              start: startIso,
              step_seconds: 5,
            },
            {
              channel_key: 'flow_rate',
              name: 'Mud Flow In',
              dimension: 'flow_rate',
              unit: 'gpm',
              values: [540, 545, 550],
              start: startIso,
              step_seconds: 5,
            },
          ],
        },
        makeIdempotencyKey('commission', wellId),
      )
    },
    onSuccess: () => {
      void liveStream.resyncNow()
    },
  })

  const transitionMutation = useMutation({
    mutationFn: async ({
      alert,
      target,
      reason,
    }: {
      alert: AlertRow
      target: 'acknowledged' | 'cleared' | 'cancelled'
      reason: string
    }) => {
      const expectedVersion = alert.version ?? alert.updated_at ?? ''
      const idemKey = makeIdempotencyKey(`alert-${target}`, alert.id)
      if (target === 'acknowledged') {
        return drillingApi.acknowledgeAlert(
          alert.id,
          { expected_updated_at: expectedVersion, reason: reason.trim() || undefined },
          idemKey,
        )
      }
      if (target === 'cleared') {
        return drillingApi.clearAlert(
          alert.id,
          { expected_updated_at: expectedVersion, reason: reason.trim() },
          idemKey,
        )
      }
      return drillingApi.cancelAlert(
        alert.id,
        { expected_updated_at: expectedVersion, reason: reason.trim() },
        idemKey,
      )
    },
    onSuccess: (updated) => {
      setActiveTransition(null)
      setTransitionReason('')
      setTransitionError(null)
      setConflictBanner(null)
      void queryClient.invalidateQueries({ queryKey: ['alerts', wellId] })
      void queryClient.invalidateQueries({ queryKey: ['alert-evidence', updated.id] })
      void queryClient.invalidateQueries({ queryKey: ['well-live-snapshot', wellId] })
    },
    onError: (err) => {
      if (err instanceof ApiError && err.isConflict) {
        setConflictBanner(t('liveMonitor.versionConflictBanner'))
        setActiveTransition(null)
        setTransitionReason('')
        void queryClient.invalidateQueries({ queryKey: ['alerts', wellId] })
        if (selectedAlertId) {
          void queryClient.invalidateQueries({ queryKey: ['alert-evidence', selectedAlertId] })
        }
        return
      }
      setTransitionError(err instanceof Error ? err.message : t('errors.generic'))
    },
  })

  const filteredAlerts = useMemo(() => {
    const items = alertsQuery.data?.items ?? []
    const byChannel =
      channelFilter === 'all'
        ? items
        : items.filter(
            (a) =>
              a.channel_key === channelFilter ||
              a.observed?.unit === channelFilter ||
              a.rule_ref?.includes(channelFilter),
          )
    return [...byChannel].sort((a, b) => {
      if (sortBy === 'severity') {
        const diff = severityRank(b.severity) - severityRank(a.severity)
        if (diff !== 0) return diff
      }
      const ta = a.raised_at ? new Date(a.raised_at).getTime() : 0
      const tb = b.raised_at ? new Date(b.raised_at).getTime() : 0
      return tb - ta
    })
  }, [alertsQuery.data?.items, channelFilter, sortBy])

  const renderTransportLabel = (state: WellTransportState): string => {
    switch (state) {
      case 'LIVE':
        return t('liveMonitor.stateLive')
      case 'RECONNECTING':
        return t('liveMonitor.stateReconnecting')
      case 'RESYNCING':
        return t('liveMonitor.stateResyncing')
      case 'POLLING FALLBACK':
        return t('liveMonitor.statePollingFallback')
      case 'STALE':
        return t('liveMonitor.stateStale')
      case 'ERROR':
        return t('liveMonitor.stateError')
      case 'DISCONNECTED':
      default:
        return t('liveMonitor.stateDisconnected')
    }
  }

  const renderTrendBadge = (trend: TelemetryTrend | undefined) => {
    if (!trend) return null
    if (trend.direction === 'insufficient_data') {
      return (
        <span className="text-[11px] text-graphite-400" data-testid="kpi-trend-insufficient">
          {t('liveMonitor.trendInsufficient')}
        </span>
      )
    }
    if (trend.direction === 'rising') {
      return <Badge tone="warning">↑ {t('liveMonitor.trendRising')}</Badge>
    }
    if (trend.direction === 'falling') {
      return <Badge tone="info">↓ {t('liveMonitor.trendFalling')}</Badge>
    }
    return <Badge tone="neutral">→ {t('liveMonitor.trendFlat')}</Badge>
  }

  return (
    <div className="space-y-4" data-testid="operational-monitor">
      {/* Transport & Temporal Consistency Strip */}
      <Card
        title={t('liveMonitor.title')}
        subtitle={t('liveMonitor.subtitle')}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              tone={transportBadgeTone(liveStream.transportState)}
            >
              <span data-testid="live-transport-state">
                {renderTransportLabel(liveStream.transportState)}
              </span>
            </Badge>
            <Button
              size="sm"
              variant="default"
              onClick={() => void liveStream.resyncNow()}
              data-testid="live-resync-btn"
            >
              {t('liveMonitor.resyncButton')}
            </Button>
            <Button
              size="sm"
              variant="default"
              onClick={() => evaluateMutation.mutate()}
              disabled={evaluateMutation.isPending}
              data-testid="evaluate-rules-btn"
            >
              {t('liveMonitor.evaluateRulesButton')}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => commissionMutation.mutate()}
              disabled={commissionMutation.isPending}
              data-testid="commission-synthetic-btn"
            >
              {commissionMutation.isPending
                ? t('liveMonitor.commissioningInProgress')
                : t('liveMonitor.commissionSyntheticButton')}
            </Button>
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-graphite-500">
          <span>
            {t('liveMonitor.cursorPosition')}:{' '}
            <strong className="font-mono text-graphite-800 dark:text-graphite-200" dir="ltr">
              #{liveStream.status.afterSeq}
            </strong>
          </span>
          {liveStream.snapshot?.telemetry_as_of && (
            <span>
              {t('liveMonitor.telemetryAsOf')}:{' '}
              <span className="font-mono" dir="ltr">
                {formatDateTime(liveStream.snapshot.telemetry_as_of, locale)}
              </span>
            </span>
          )}
          {liveStream.snapshot?.alerts_as_of && (
            <span>
              {t('liveMonitor.alertsAsOf')}:{' '}
              <span className="font-mono" dir="ltr">
                {formatDateTime(liveStream.snapshot.alerts_as_of, locale)}
              </span>
            </span>
          )}
          {liveStream.snapshot?.operation_as_of && (
            <span>
              {t('liveMonitor.operationAsOf')}:{' '}
              <span className="font-mono" dir="ltr">
                {formatDateTime(liveStream.snapshot.operation_as_of, locale)}
              </span>
            </span>
          )}
          {liveStream.diagnostics.lastGap && (
            <Badge tone="warning">
              gap {liveStream.diagnostics.lastGap.fromSeq}→{liveStream.diagnostics.lastGap.toSeq} (
              {liveStream.diagnostics.lastGap.reason})
            </Badge>
          )}
        </div>
        {drillingState?.operation?.current && (
          <div className="mt-2 rounded border border-graphite-100 bg-graphite-50/60 px-3 py-1.5 text-xs dark:border-graphite-800 dark:bg-graphite-900/50">
            <span className="font-medium">{t('cockpit.currentOperation')}:</span>{' '}
            <span>{drillingState.operation.current.name}</span>{' '}
            <Badge tone="ok">{drillingState.operation.current.operation_class}</Badge>
            {drillingState.progress.current_md_si !== null && (
              <span className="ms-2 font-mono text-[11px] text-graphite-500" dir="ltr">
                MD: {formatNumber(drillingState.progress.current_md_si, locale, 1)} m (
                {drillingState.progress.current_md_source ?? 'measured'})
              </span>
            )}
          </div>
        )}
      </Card>

      {/* Live KPI Strip */}
      <Card
        title={t('liveMonitor.kpiStripTitle')}
        subtitle={t('liveMonitor.kpiStripSubtitle')}
      >
        {latestQuery.isLoading && readings.length === 0 ? (
          <p className="text-sm text-graphite-500">{t('common.loading')}</p>
        ) : latestQuery.isError ? (
          <ErrorState error={latestQuery.error} onRetry={() => latestQuery.refetch()} />
        ) : readings.length === 0 ? (
          <EmptyState
            message={t('liveMonitor.noTelemetryChannels')}
            hint={t('liveMonitor.noTelemetryHint')}
          />
        ) : (
          <div
            className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4"
            data-testid="live-kpi-strip"
          >
            {readings.map((reading) => {
              const isSelected = effectiveChannel?.channel_id === reading.channel_id
              const trend = liveStream.trends[reading.channel_key]
              const isMissing = reading.value === null || reading.freshness === 'missing'
              const isUntrustworthy =
                !isMissing &&
                (reading.is_trustworthy === false ||
                  reading.quality === 'bad' ||
                  reading.quality === 'missing' ||
                  reading.quality === 'suspect')

              return (
                <button
                  key={reading.channel_id}
                  type="button"
                  onClick={() => setSelectedChannelKey(reading.channel_key)}
                  data-testid={`kpi-card-${reading.channel_key}`}
                  className={`flex flex-col justify-between rounded-md border p-3 text-start transition ${
                    isSelected
                      ? 'border-signal bg-signal/5 dark:border-signal-light dark:bg-signal/10'
                      : 'border-graphite-100 hover:border-graphite-300 dark:border-graphite-800 dark:hover:border-graphite-700'
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <span className="block text-xs font-semibold text-graphite-700 dark:text-graphite-200">
                        {reading.label}
                      </span>
                      <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
                        {reading.channel_key} · {reading.dimension}
                      </span>
                    </div>
                    <Badge tone={freshnessTone(reading.freshness)}>
                      {reading.freshness === 'live'
                        ? t('liveMonitor.freshnessLive')
                        : reading.freshness === 'stale'
                          ? t('liveMonitor.freshnessStale')
                          : t('liveMonitor.freshnessMissing')}
                    </Badge>
                  </div>

                  <div className="my-2">
                    {isMissing ? (
                      <span
                        className="text-sm italic text-graphite-400"
                        data-testid={`kpi-missing-${reading.channel_key}`}
                      >
                        {t('liveMonitor.noValueRecorded')}
                      </span>
                    ) : isUntrustworthy ? (
                      <div data-testid={`kpi-untrustworthy-${reading.channel_key}`}>
                        <span className="block text-xs font-semibold text-danger">
                          {t('liveMonitor.untrustworthyReading')} ({reading.quality})
                        </span>
                        <span
                          className="font-mono text-xs text-graphite-500 line-through"
                          dir="ltr"
                        >
                          {formatNumber(reading.value, locale, 2)} {reading.unit}
                        </span>
                      </div>
                    ) : (
                      <span
                        className="font-mono text-lg font-semibold text-graphite-900 dark:text-graphite-100"
                        dir="ltr"
                        data-testid={`kpi-value-${reading.channel_key}`}
                      >
                        {formatNumber(reading.value, locale, 2)}{' '}
                        <span className="text-xs font-normal text-graphite-500">
                          {reading.unit}
                        </span>
                      </span>
                    )}
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-1 text-[11px] text-graphite-500">
                    <span className="inline-flex items-center gap-1">
                      <Badge tone={qualityTone(reading.quality)}>{reading.quality}</Badge>
                      {reading.age_seconds !== null && (
                        <span dir="ltr">
                          {t('liveMonitor.ageSeconds', {
                            seconds: Math.round(reading.age_seconds),
                          })}
                        </span>
                      )}
                    </span>
                    {renderTrendBadge(trend)}
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </Card>

      {/* Channel History Chart */}
      {effectiveChannel && (
        <Card
          title={`${t('liveMonitor.historyChartTitle')} · ${effectiveChannel.label}`}
          subtitle={t('liveMonitor.historyChartSubtitle')}
          actions={
            <label className="flex items-center gap-2 text-xs">
              <span className="text-graphite-500">{t('liveMonitor.selectChannel')}:</span>
              <select
                value={effectiveChannel.channel_key}
                onChange={(e) => setSelectedChannelKey(e.target.value)}
                className="rounded border border-graphite-200 bg-white px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-800"
                data-testid="channel-history-select"
              >
                {readings.map((r) => (
                  <option key={r.channel_id} value={r.channel_key}>
                    {r.label} ({r.channel_key})
                  </option>
                ))}
              </select>
            </label>
          }
        >
          {windowQuery.isLoading ? (
            <p className="text-sm text-graphite-500">{t('common.loading')}</p>
          ) : windowQuery.isError ? (
            <ErrorState error={windowQuery.error} onRetry={() => windowQuery.refetch()} />
          ) : windowQuery.data ? (
            <ChannelHistoryChart
              points={windowQuery.data.items}
              unit={
                windowQuery.data.unit ??
                windowQuery.data.channel?.unit ??
                effectiveChannel.unit ??
                ''
              }
              downsampled={Boolean(windowQuery.data.downsampled)}
              truncated={Boolean(windowQuery.data.truncated)}
              totalInWindow={
                windowQuery.data.total ??
                windowQuery.data.total_in_window ??
                windowQuery.data.items.length
              }
            />
          ) : null}
        </Card>
      )}

      {/* Operational Alerts & Lifecycle */}
      <Card
        title={t('liveMonitor.alertsTitle')}
        subtitle={t('liveMonitor.alertsSubtitle')}
        actions={
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <label className="flex items-center gap-1">
              <span className="text-graphite-500">{t('liveMonitor.filterStatus')}:</span>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="rounded border border-graphite-200 bg-white px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-800"
                data-testid="alerts-filter-status"
              >
                <option value="all">{t('common.all')}</option>
                <option value="raised">raised</option>
                <option value="acknowledged">acknowledged</option>
                <option value="cleared">cleared</option>
                <option value="cancelled">cancelled</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <span className="text-graphite-500">{t('liveMonitor.filterSeverity')}:</span>
              <select
                value={severityFilter}
                onChange={(e) => setSeverityFilter(e.target.value)}
                className="rounded border border-graphite-200 bg-white px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-800"
                data-testid="alerts-filter-severity"
              >
                <option value="all">{t('common.all')}</option>
                <option value="critical">critical</option>
                <option value="high">high</option>
                <option value="medium">medium</option>
                <option value="low">low</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <span className="text-graphite-500">{t('liveMonitor.filterChannel')}:</span>
              <select
                value={channelFilter}
                onChange={(e) => setChannelFilter(e.target.value)}
                className="rounded border border-graphite-200 bg-white px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-800"
                data-testid="alerts-filter-channel"
              >
                <option value="all">{t('common.all')}</option>
                {readings.map((r) => (
                  <option key={r.channel_id} value={r.channel_key}>
                    {r.channel_key}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1">
              <span className="text-graphite-500">{t('liveMonitor.sortLabel')}:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as 'severity' | 'newest')}
                className="rounded border border-graphite-200 bg-white px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-800"
                data-testid="alerts-sort-select"
              >
                <option value="severity">{t('liveMonitor.sortSeverity')}</option>
                <option value="newest">{t('liveMonitor.sortNewest')}</option>
              </select>
            </label>
          </div>
        }
      >
        {conflictBanner && (
          <div
            role="alert"
            className="mb-3 rounded-md border border-warning bg-warning/10 px-3 py-2 text-xs text-warning"
            data-testid="alert-conflict-banner"
          >
            {conflictBanner}
          </div>
        )}

        {alertsQuery.isLoading ? (
          <p className="text-sm text-graphite-500">{t('common.loading')}</p>
        ) : alertsQuery.isError ? (
          <ErrorState error={alertsQuery.error} onRetry={() => alertsQuery.refetch()} />
        ) : filteredAlerts.length === 0 ? (
          <EmptyState message={t('liveMonitor.noAlerts')} />
        ) : (
          <ul className="space-y-2.5" data-testid="operational-alerts-list">
            {filteredAlerts.map((alert) => {
              const allowed = alert.allowed_transitions ?? []
              const isEditingThis = activeTransition?.alertId === alert.id
              return (
                <li
                  key={alert.id}
                  data-testid={`alert-row-${alert.id}`}
                  className="rounded-md border border-graphite-100 p-3 dark:border-graphite-800"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone={severityTone(alert.severity)}>
                          {humanise(alert.severity)}
                        </Badge>
                        <Badge
                          tone={
                            alert.status === 'cleared'
                              ? 'ok'
                              : alert.status === 'cancelled'
                                ? 'neutral'
                                : 'warning'
                          }
                        >
                          <span data-testid={`alert-status-${alert.id}`}>
                            {formatStatus(alert.status)}
                          </span>
                        </Badge>
                        {alert.rule_ref && (
                          <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
                            {alert.rule_ref}
                          </span>
                        )}
                        <span className="font-mono text-[11px] text-graphite-400" dir="ltr">
                          {formatDateTime(alert.raised_at, locale)}
                        </span>
                      </div>
                      <p className="text-sm font-medium">{alert.title}</p>
                      {alert.description && (
                        <p className="text-xs text-graphite-600 dark:text-graphite-300">
                          {alert.description}
                        </p>
                      )}
                      <p className="font-mono text-[11px] text-graphite-500" dir="ltr">
                        observed: {formatNumber(alert.observed.value, locale, 2)}{' '}
                        {alert.observed.unit ?? ''} · threshold:{' '}
                        {alert.observed.declared_threshold !== null
                          ? `${formatNumber(alert.observed.declared_threshold, locale, 2)} ${alert.observed.declared_unit ?? ''}`
                          : `${formatNumber(alert.observed.threshold, locale, 2)} ${alert.observed.unit ?? ''}`}
                      </p>
                    </div>

                    <div className="flex flex-wrap items-center gap-1.5">
                      {allowed.includes('acknowledged') && (
                        <Button
                          size="sm"
                          variant="default"
                          onClick={() => {
                            setActiveTransition({ alertId: alert.id, target: 'acknowledged' })
                            setTransitionReason('')
                            setTransitionError(null)
                          }}
                          data-testid={`alert-ack-btn-${alert.id}`}
                        >
                          {t('liveMonitor.actionAcknowledge')}
                        </Button>
                      )}
                      {allowed.includes('cleared') && (
                        <Button
                          size="sm"
                          variant="default"
                          onClick={() => {
                            setActiveTransition({ alertId: alert.id, target: 'cleared' })
                            setTransitionReason('')
                            setTransitionError(null)
                          }}
                          data-testid={`alert-clear-btn-${alert.id}`}
                        >
                          {t('liveMonitor.actionClear')}
                        </Button>
                      )}
                      {allowed.includes('cancelled') && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setActiveTransition({ alertId: alert.id, target: 'cancelled' })
                            setTransitionReason('')
                            setTransitionError(null)
                          }}
                          data-testid={`alert-cancel-btn-${alert.id}`}
                        >
                          {t('liveMonitor.actionCancel')}
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="default"
                        onClick={() => setSelectedAlertId(alert.id)}
                        data-testid={`alert-evidence-btn-${alert.id}`}
                      >
                        {t('liveMonitor.inspectEvidence')}
                      </Button>
                    </div>
                  </div>

                  {isEditingThis && activeTransition && (
                    <form
                      className="mt-3 space-y-2 rounded border border-graphite-200 bg-graphite-50 p-2.5 dark:border-graphite-700 dark:bg-graphite-900"
                      onSubmit={(e) => {
                        e.preventDefault()
                        if (
                          (activeTransition.target === 'cleared' ||
                            activeTransition.target === 'cancelled') &&
                          !transitionReason.trim()
                        ) {
                          setTransitionError(t('liveMonitor.reasonRequiredError'))
                          return
                        }
                        transitionMutation.mutate({
                          alert,
                          target: activeTransition.target,
                          reason: transitionReason,
                        })
                      }}
                      data-testid={`alert-transition-form-${alert.id}`}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <input
                          type="text"
                          value={transitionReason}
                          onChange={(e) => setTransitionReason(e.target.value)}
                          placeholder={
                            activeTransition.target === 'acknowledged'
                              ? t('liveMonitor.ackReasonPlaceholder')
                              : t('liveMonitor.reasonPlaceholder')
                          }
                          className="min-w-[240px] flex-1 rounded border border-graphite-200 bg-white px-2.5 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-800"
                          data-testid={`alert-reason-input-${alert.id}`}
                        />
                        <Button
                          type="submit"
                          size="sm"
                          variant="primary"
                          disabled={transitionMutation.isPending}
                          data-testid={`alert-confirm-btn-${alert.id}`}
                        >
                          {t('liveMonitor.confirmAction')}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setActiveTransition(null)
                            setTransitionError(null)
                          }}
                        >
                          {t('common.cancel')}
                        </Button>
                      </div>
                      {transitionError && (
                        <p
                          role="alert"
                          className="text-xs text-danger"
                          data-testid="alert-transition-error"
                        >
                          {transitionError}
                        </p>
                      )}
                    </form>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </Card>

      <AlertEvidenceDrawer
        alertId={selectedAlertId}
        wellId={wellId}
        onClose={() => setSelectedAlertId(null)}
      />
    </div>
  )
}
