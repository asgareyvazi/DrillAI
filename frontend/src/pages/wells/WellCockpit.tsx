/**
 * Well Cockpit — the landing workspace for a well.
 *
 * It answers, in order: where are we, what is happening now, what went wrong, what is at risk, what
 * is missing, and where did every statement come from. Each block is fed by one API call so the
 * page degrades block by block instead of failing whole, and every number can open its evidence.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type {
  DrillingState,
  NptSummary,
  Well,
  TimelineEntry,
  TwinState,
  AuditTrail,
  DocumentRow,
} from '../../api/types'
import { EvidencePanel, EvidenceSummaryStrip, WhyButton } from '../../components/evidence/EvidencePanel'
import { PageHeader } from '../../components/layout/AppShell'
import {
  Async,
  Badge,
  BarList,
  Button,
  Card,
  EmptyState,
  Json,
  ProgressBar,
  Table,
  Tabs,
  Value,
  toneForSeverity,
} from '../../components/common'
import { useI18n } from '../../i18n'
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  formatStatus,
  humanise,
} from '../../lib/format'
import { useSession } from '../../stores/session'

// --------------------------------------------------------------------------- overview

function ProgressCard({ state }: { state: DrillingState }) {
  const { t } = useI18n()
  const { unitSystem } = useSession()
  // unitSystem is passed to each Value so the display-unit switch is honoured per value.
  const progress = state.progress
  return (
    <Card
      title={t('cockpit.progress')}
      subtitle={`generated ${formatDateTime(state.generated_at ?? null)}`}
      actions={<Badge tone="neutral">{state.counts.operations ?? 0} operations</Badge>}
    >
      <div className="space-y-3">
        <ProgressBar
          percent={progress.percent_planned_depth}
          label={`${t('cockpit.currentDepth')} / ${t('cockpit.plannedDepth')}`}
          basis={progress.percent_basis}
        />
        <dl className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          <Value
            label={t('cockpit.currentDepth')}
            value={progress.current_md_si}
            unit="m"
            unitSystem={unitSystem}
            source={progress.current_md_source}
            reason="no measured depth is recorded for this well"
          />
          <Value
            label={t('cockpit.plannedDepth')}
            value={progress.planned_td_md_si}
            unit="m"
            unitSystem={unitSystem}
            source="wellbore / section planned depth"
            reason="no planned depth is on file"
          />
          <Value
            label={t('cockpit.variance')}
            value={progress.depth_variance_si}
            unit="m"
            unitSystem={unitSystem}
            source="measured minus planned at the active section"
            reason="variance needs both a measured and a planned depth"
          />
          <Value
            label={t('cockpit.currentSection')}
            value={progress.current_section?.name ?? null}
            source={progress.current_section?.hole_diameter_nominal ?? 'well_sections'}
            reason="no section spans the recorded depth"
          />
        </dl>
        <p className="text-[11px] text-graphite-500">
          sections started: {progress.sections_started} of {progress.section_count} · depth source:{' '}
          {progress.current_md_source ?? 'none — no measured depth is recorded'}
          {progress.note && <> · {progress.note}</>}
        </p>
      </div>
    </Card>
  )
}

function OperationCard({ state }: { state: DrillingState }) {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  const { operation } = state
  const rows: Array<{ role: string; op: typeof operation.current; basis: string }> = [
    { role: t('operation.current'), op: operation.current, basis: 'recorded operation covering now' },
    { role: t('operation.previous'), op: operation.previous, basis: 'predecessor of the current operation' },
    { role: t('operation.planned'), op: operation.next, basis: operation.next_basis },
  ]
  return (
    <Card title={t('cockpit.currentOperation')}>
      <div className="space-y-2">
        {rows.map(({ role, op, basis }) => (
          <div
            key={role}
            className="rounded-md border border-graphite-100 p-2.5 dark:border-graphite-800"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] font-semibold tracking-wide text-graphite-500 uppercase">{role}</span>
              {op ? (
                <Badge
                  tone={
                    op.operation_class === 'actual' || op.status === 'completed'
                      ? 'ok'
                      : op.operation_class === 'planned'
                        ? 'info'
                        : 'neutral'
                  }
                >
                  {formatStatus(op.status)}
                </Badge>
              ) : (
                <Badge tone="neutral">{t('common.unknown')}</Badge>
              )}
            </div>
            {op ? (
              <>
                <p className="mt-1 text-sm font-medium">{op.name}</p>
                <p className="text-[11px] text-graphite-500">
                  {op.actual_start
                    ? `${formatDateTime(op.actual_start, locale)} → ${op.actual_end ? formatDateTime(op.actual_end, locale) : 'in progress'}`
                    : op.planned_start
                      ? `planned ${formatDateTime(op.planned_start, locale)} → ${formatDateTime(op.planned_end, locale)}`
                      : 'no time recorded for this operation'}
                  {op.actual_duration_hours !== null && <> · {formatDuration(op.actual_duration_hours)}</>}
                  {op.planned_duration_hours !== null && op.actual_duration_hours === null && (
                    <> · planned {formatDuration(op.planned_duration_hours)}</>
                  )}
                  {op.depth_to_md_si !== null && (
                    <>
                      {' '}· to depth {formatNumber(op.depth_to_md_si, locale, 0)} {unitSystem === 'oilfield' ? 'ft' : 'm'}
                    </>
                  )}
                </p>
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <Badge tone="neutral">{humanise(op.operation_class ?? 'unclassified')}</Badge>
                  {op.data_quality && <Badge tone="warning">{op.data_quality}</Badge>}
                  {(op.npt_hours ?? 0) > 0 && (
                    <Badge tone="danger">NPT {formatDuration(op.npt_hours)}</Badge>
                  )}
                  <Link
                    to={`/wells/${state.well.id}/documents`}
                    className="text-[11px] text-signal-deep underline-offset-2 hover:underline dark:text-signal-light"
                  >
                    {t('common.provenance')}
                  </Link>
                </div>
              </>
            ) : (
              <p className="mt-1 text-sm text-graphite-500">
                none recorded — <span className="italic">{basis}</span>
              </p>
            )}
            {!op && <p className="mt-1 text-[11px] text-graphite-500">basis: {basis}</p>}
          </div>
        ))}
        <p className="text-[11px] text-graphite-500">
          basis for the next operation: {operation.next_basis} — planned, inferred and unknown are never
          presented as the same thing.
        </p>
      </div>
    </Card>
  )
}

function MeasuredCard({
  measured,
  onEvidence,
}: {
  measured: DrillingState['measured']
  onEvidence: (subjectKind: string, subjectId: string) => void
}) {
  const { t } = useI18n()
  const { unitSystem } = useSession()
  if (measured.length === 0) return <EmptyState message={t('empty.noEngineRuns')} />
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
      {measured.map((item) => (
        <Value
          key={item.key}
          label={item.label}
          value={item.value}
          unit={item.unit}
          unitSystem={unitSystem}
          source={item.source}
          quality={item.quality}
          evidence={
            item.evidence_ref ? (
              <WhyButton
                onClick={() => onEvidence('drilling_state', item.key)}
                label={t('common.showEvidence')}
              />
            ) : undefined
          }
        />
      ))}
    </div>
  )
}

/** Header context: what the API knows about this well, and nothing it does not. */
function wellDescription(well: Well | null): string {
  if (!well) return 'Where the well is, what is happening, what went wrong, and where each statement comes from.'
  const parts = [well.well_type, well.operator, well.uwi ? `UWI ${well.uwi}` : null].filter(Boolean)
  return `${parts.join(' · ')} — where the well is, what is happening, what went wrong, and where each statement comes from.`
}

/**
 * Controllability is tri-state: an event that was recorded without a code has `operator_controllable`
 * of `null`. Labelling it as either controllable or not would invent a judgement nobody made.
 */
function controllabilitySuffix(controllable: boolean | null): string {
  if (controllable === null) return ' (controllability not established)'
  return controllable ? ' (operator-controllable)' : ' (not controllable)'
}

function NptCard({ npt, onSelectCause }: { npt: NptSummary; onSelectCause: (key: string) => void }) {
  const { t, locale } = useI18n()
  if (npt.total_hours <= 0) return <EmptyState message={t('empty.noNpt')} />
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Value label={t('cockpit.npt')} value={npt.total_hours} unit="h" source={npt.basis} />
        <Value
          label="% of well time"
          value={npt.percent_of_well_time}
          source="NPT hours / recorded operation hours"
          reason="no operation hours are recorded, so no share can be computed"
        />
        <Value
          label="operator-controllable"
          value={npt.controllable_hours}
          unit="h"
          source="NPT classification"
        />
        <Value label="occurrences" value={npt.event_count} source="npt_events" />
      </dl>
      <dl className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Value label="not controllable" value={npt.uncontrollable_hours} unit="h" source="NPT classification" />
        <Value
          label="controllability unknown"
          value={npt.unknown_controllability_hours}
          unit="h"
          source="events recorded without a code"
          reason="every recorded NPT event carries a controllability classification"
        />
        <Value label="from operations" value={npt.hours_from_operations} unit="h" source="operation.npt_hours" />
        <Value label="recorded well time" value={npt.measured_hours_total} unit="h" source="measured operations" />
      </dl>
      <div>
        <p className="mb-1 text-xs font-medium text-graphite-600 dark:text-graphite-300">
          {t('cockpit.nptPareto')} · {formatPercent(npt.percent_of_well_time, locale)} of well time
        </p>
        <BarList
          rows={npt.by_category
            .filter((row) => row.occurrences > 0 || row.hours > 0)
            .map((row) => ({
              key: row.key,
              label: `${row.label}${controllabilitySuffix(row.operator_controllable)}`,
              value: row.hours,
              display: `${formatDuration(row.hours)} · ${row.occurrences}× · ${formatPercent(row.percent_of_total, locale)}`,
              tone:
                row.operator_controllable === true
                  ? 'bg-danger/70'
                  : row.operator_controllable === false
                    ? 'bg-warning/70'
                    : 'bg-graphite-400/70',
            }))}
          onSelect={onSelectCause}
        />
      </div>
      {npt.offset_comparison && (
        <div>
          <p className="mb-1 text-xs font-medium">Offset comparison</p>
          <Json value={npt.offset_comparison} max={120} />
        </div>
      )}
      {npt.notes.length > 0 && (
        <ul className="list-disc ps-4 text-[11px] text-graphite-500">
          {npt.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      <p className="text-[11px] text-graphite-500">{npt.basis}</p>
    </div>
  )
}

function RisksCard({ state }: { state: DrillingState }) {
  const { t } = useI18n()
  if (state.risks.length === 0) return <EmptyState message="No risk flag was raised from the recorded data." />
  return (
    <ul className="space-y-2">
      {state.risks.map((risk) => (
        <li key={risk.key} className="rounded-md border border-graphite-100 p-2.5 dark:border-graphite-800">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={toneForSeverity(risk.severity)}>{risk.severity}</Badge>
            <span className="text-sm font-medium">{risk.label}</span>
          </div>
          <p className="mt-1 text-xs text-graphite-600 dark:text-graphite-300">{risk.detail}</p>
          <p className="mt-1 text-[11px] text-graphite-500">
            {t('common.source')}: {risk.basis ?? 'recorded data'}
            {risk.evidence_ref ? ' · evidence linked' : ''}
          </p>
        </li>
      ))}
    </ul>
  )
}

function MissingCard({ state }: { state: DrillingState }) {
  const { t } = useI18n()
  if (state.missing.length === 0) return <EmptyState message={t('empty.noMissing')} />
  return (
    <Table
      rows={state.missing}
      rowKey={(row) => row.key}
      columns={[
        { key: 'label', header: t('common.detail'), render: (row) => row.description },
        { key: 'why', header: 'why it matters', render: (row) => row.why_it_matters },
        { key: 'how', header: 'how to supply', render: (row) => row.how_to_supply },
      ]}
    />
  )
}

function TwinCard({ twin }: { twin: TwinState }) {
  const { t } = useI18n()
  if (twin.aspects.length === 0)
    return <EmptyState message="The twin has no aspects for this well yet." hint="Process a DDR to populate it." />
  return (
    <Table
      rows={twin.aspects}
      rowKey={(row) => row.id}
      columns={[
        {
          key: 'aspect',
          header: 'Aspect',
          render: (row) => (
            <span>
              <span className="font-medium">{humanise(row.aspect)}</span>
              <span className="ms-2 text-[11px] text-graphite-500">{row.schema_key} v{row.schema_version}</span>
            </span>
          ),
        },
        { key: 'kind', header: 'State kind', render: (row) => <Badge tone="info">{row.state_kind}</Badge> },
        { key: 'summary', header: 'Summary', render: (row) => row.summary ?? '—' },
        { key: 'quality', header: t('common.quality'), render: (row) => row.data_quality ?? '—' },
        { key: 'computed', header: 'Computed', render: (row) => formatDateTime(row.computed_at) },
        {
          key: 'valid',
          header: 'Validity',
          render: (row) => (
            <span className="text-[11px]">
              {formatDateTime(row.valid_from)} → {row.valid_to ? formatDateTime(row.valid_to) : 'open'}
              {row.supersedes_id && <span className="block text-graphite-500">supersedes {row.supersedes_id}</span>}
            </span>
          ),
        },
      ]}
      empty={<EmptyState message="No twin aspects." />}
    />
  )
}

function AuditCard({ audit }: { audit: AuditTrail }) {
  const { t } = useI18n()
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {Object.entries(audit.counts).map(([key, value]) => (
          <Badge key={key} tone={value > 0 ? 'info' : 'neutral'}>
            {humanise(key)}: {value}
          </Badge>
        ))}
      </div>
      <Table
        rows={audit.engine_runs}
        rowKey={(row) => row.id}
        empty={<EmptyState message={t('empty.noEngineRuns')} />}
        columns={[
          { key: 'engine', header: t('common.engine'), render: (row) => <span className="font-mono text-xs">{row.engine_key}@{row.engine_version}</span> },
          { key: 'status', header: t('common.status'), render: (row) => <Badge tone={row.status === 'succeeded' ? 'ok' : 'warning'}>{row.status}</Badge> },
          {
            key: 'feasible',
            header: 'Feasible',
            render: (row) =>
              row.is_feasible === null ? '—' : <Badge tone={row.is_feasible ? 'ok' : 'danger'}>{row.is_feasible ? 'yes' : 'no'}</Badge>,
          },
          { key: 'hash', header: t('audit.inputsHash'), render: (row) => <span className="font-mono text-[10px]">{row.inputs_hash.slice(0, 12)}…</span> },
          { key: 'outputs', header: t('audit.outputsHash'), render: (row) => <span className="font-mono text-[10px]">{row.outputs_hash.slice(0, 12)}…</span> },
          { key: 'by', header: t('audit.triggeredBy'), render: (row) => row.triggered_by },
          { key: 'at', header: 'When', render: (row) => formatDateTime(row.created_at) },
        ]}
      />
    </div>
  )
}

// --------------------------------------------------------------------------- page

export default function WellCockpit() {
  const { t } = useI18n()
  const { locale } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const [tab, setTab] = useState('overview')
  const [evidenceSubject, setEvidenceSubject] = useState<{ kind: string; id: string } | null>(null)
  const [nptCause, setNptCause] = useState<string | null>(null)

  const state = useQuery({ queryKey: ['well-state', id], queryFn: ({ signal }) => drillingApi.wellState(id, signal) })
  const timeline = useQuery({
    queryKey: ['well-timeline', id],
    queryFn: ({ signal }) => drillingApi.wellTimeline(id, { limit: 500 }, signal),
    enabled: tab === 'timeline' || tab === 'overview',
  })
  const well = state.data?.state.well ?? null
  const npt = useQuery({
    queryKey: ['well-npt', id],
    queryFn: ({ signal }) => drillingApi.wellNpt(id, {}, signal),
    // The endpoint answers with an envelope; unwrap it once here so every consumer sees a summary.
    select: (data) => data.npt,
    enabled: tab === 'overview' || tab === 'npt',
  })
  const twin = useQuery({
    queryKey: ['well-twin', id],
    queryFn: ({ signal }) => drillingApi.wellTwin(id, signal),
    enabled: tab === 'twin',
  })
  const audit = useQuery({
    queryKey: ['well-audit', id],
    queryFn: ({ signal }) => drillingApi.wellAudit(id, signal),
    enabled: tab === 'audit',
  })
  const documents = useQuery({
    queryKey: ['documents', id],
    queryFn: ({ signal }) => drillingApi.listDocuments({ well_id: id }, signal),
    enabled: tab === 'documents',
  })

  const tabs = [
    { key: 'overview', label: t('nav.cockpit') },
    { key: 'timeline', label: t('cockpit.timeline'), badge: timeline.data && <Badge tone="neutral">{timeline.data.count}</Badge> },
    {
      key: 'npt',
      label: t('cockpit.npt'),
      badge: state.data && state.data.state.npt.total_hours > 0 && (
        <Badge tone="danger">{state.data.state.npt.total_hours} h</Badge>
      ),
    },
    { key: 'documents', label: t('nav.documents') },
    { key: 'twin', label: t('cockpit.twin') },
    { key: 'audit', label: t('cockpit.audit') },
    { key: 'missing', label: t('cockpit.missing'), badge: state.data && state.data.state.missing.length > 0 && (
      <Badge tone="warning">{state.data.state.missing.length}</Badge>
    ) },
  ]

  return (
    <div className="space-y-4">
      {/*
        The header names the well on purpose. A cockpit that shows a depth without saying which well
        it belongs to is a context bug waiting to happen: every statement on this page has to be
        attributable to the well the user believes they are looking at.
      */}
      <PageHeader
        title={well?.name ?? t('cockpit.title')}
        description={wellDescription(well)}
        breadcrumb={
          <>
            <Link to="/wells">{t('nav.wells')}</Link>
            {well && <> / {well.name}</>}
          </>
        }
        actions={
          <>
            <EvidenceSummaryStrip
              wellId={id}
              onOpen={() => setEvidenceSubject({ kind: 'well', id })}
            />
            <Link to={`/wells/${id}/engineering`}>
              <Button variant="primary" size="sm">
                {t('engineering.title')}
              </Button>
            </Link>
          </>
        }
      />

      <Tabs tabs={tabs} active={tab} onChange={setTab} />

      {tab === 'overview' && (
        <Async query={state}>
          {(data) => (
            <div className="grid gap-3 xl:grid-cols-2">
              <ProgressCard state={data.state} />
              <OperationCard state={data.state} />
              <Card
                title={t('cockpit.kpis')}
                subtitle={`${data.state.measured.length} values with a recorded source`}
              >
                <MeasuredCard
                  measured={data.state.measured}
                  onEvidence={(kind, subjectId) => setEvidenceSubject({ kind, id: subjectId })}
                />
              </Card>
              <Card title={t('cockpit.risks')}>
                <RisksCard state={data.state} />
              </Card>
              <Card title={t('cockpit.npt')} className="xl:col-span-2">
                <Async query={npt}>
                  {(summary) => <NptCard npt={summary} onSelectCause={setNptCause} />}
                </Async>
              </Card>
            </div>
          )}
        </Async>
      )}

      {tab === 'timeline' && (
        <Card
          title={t('cockpit.timeline')}
          subtitle="operations, NPT events, engine runs, documents and twin changes on one axis"
          actions={
            timeline.data && (
              <Badge tone="neutral">
                kinds: {timeline.data.kinds_available.join(', ')}
              </Badge>
            )
          }
        >
          <Async query={timeline}>
            {(data) =>
              data.entries.length === 0 ? (
                <EmptyState message={t('empty.noTimeline')} />
              ) : (
                <ul className="space-y-1.5">
                  {data.entries.map((entry: TimelineEntry, index) => (
                    <li
                      key={`${entry.kind}-${entry.at}-${index}`}
                      className="flex flex-wrap items-start gap-3 rounded-md border border-graphite-100 px-3 py-2 dark:border-graphite-800"
                    >
                      <span className="w-36 shrink-0 font-mono text-[11px] text-graphite-500">
                        {formatDateTime(entry.at, locale)}
                      </span>
                      <span className="w-28 shrink-0">
                        <Badge tone={entry.kind.includes('npt') ? 'danger' : entry.kind.includes('engine') ? 'info' : 'neutral'}>
                          {humanise(entry.kind)}
                        </Badge>
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm">{entry.title}</span>
                        {entry.summary && <span className="block text-[11px] text-graphite-500">{entry.summary}</span>}
                        {(entry.depth_md_si !== null || entry.duration_hours !== null || entry.npt_hours !== null) && (
                          <span className="block text-[11px] text-graphite-500">
                            {entry.depth_md_si !== null && <>at {formatNumber(entry.depth_md_si, locale, 0)} m</>}
                            {entry.duration_hours !== null && <> · {formatDuration(entry.duration_hours)}</>}
                            {entry.npt_hours !== null && <> · NPT {formatDuration(entry.npt_hours)}</>}
                          </span>
                        )}
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        {entry.severity && (
                          <Badge tone={toneForSeverity(entry.severity)}>{humanise(entry.severity)}</Badge>
                        )}
                        {entry.status && <Badge tone="neutral">{formatStatus(entry.status)}</Badge>}
                        {entry.npt_hours !== null && <Badge tone="danger">NPT</Badge>}
                        <WhyButton
                          onClick={() =>
                            setEvidenceSubject({
                              kind: entry.kind,
                              id: entry.evidence_refs[0] ?? entry.document_id ?? entry.id,
                            })
                          }
                        />
                      </span>
                    </li>
                  ))}
                </ul>
              )
            }
          </Async>
        </Card>
      )}

      {tab === 'npt' && (
        <Async query={npt}>
          {(data) => (
            <div className="grid gap-3 xl:grid-cols-[2fr_1fr]">
              <Card title={t('cockpit.npt')} subtitle={`basis: ${data.basis}`}>
                <NptCard npt={data} onSelectCause={setNptCause} />
              </Card>
              <Card title="Cases" subtitle="each case keeps its code, hours and whether it was controllable">
                {data.cases.length === 0 ? (
                  <EmptyState message={t('empty.noNpt')} />
                ) : (
                  <Table
                    rows={
                      nptCause
                        ? data.cases.filter(
                            (row) => row.category === nptCause || row.code === nptCause || row.subcategory === nptCause,
                          )
                        : data.cases
                    }
                    rowKey={(row) => row.id}
                    empty={<EmptyState message="No case matches the selected cause." />}
                    columns={[
                      { key: 'title', header: 'Case', render: (row) => row.title },
                      { key: 'code', header: 'Code', render: (row) => row.code ?? '—' },
                      { key: 'hours', header: 'Hours', render: (row) => `${formatNumber(row.hours, locale, 1)} h`, align: 'end' },
                      {
                        key: 'classification',
                        header: 'Classification',
                        render: (row) => (
                          <span className="text-[11px]">
                            {row.classification_source}
                            {row.confidence !== null && <> · {formatPercent(row.confidence * 100, locale, 0)}</>}
                          </span>
                        ),
                      },
                      {
                        key: 'control',
                        header: 'Operator-controllable',
                        render: (row) => (
                          <Badge
                            tone={
                              row.operator_controllable === true
                                ? 'danger'
                                : row.operator_controllable === false
                                  ? 'neutral'
                                  : 'warning'
                            }
                          >
                            {row.operator_controllable === null
                              ? 'unknown'
                              : row.operator_controllable
                                ? 'yes'
                                : 'no'}
                          </Badge>
                        ),
                      },
                      {
                        key: 'window',
                        header: 'Window',
                        render: (row) => (
                          <span className="text-[11px]">
                            {formatDateTime(row.started_at, locale)} → {formatDateTime(row.ended_at, locale)}
                          </span>
                        ),
                      },
                    ]}
                  />
                )}
              </Card>
            </div>
          )}
        </Async>
      )}

      {tab === 'documents' && (
        <Card
          title={t('cockpit.documents')}
          actions={
            <Link to={`/wells/${id}/documents`}>
              <Button size="sm">{t('documents.title')}</Button>
            </Link>
          }
        >
          <Async query={documents} empty={(data) => (data.items.length === 0 ? <EmptyState message={t('empty.noDocuments')} /> : false)}>
            {(data) => (
              <Table
                rows={data.items}
                rowKey={(row: DocumentRow) => row.id}
                columns={[
                  {
                    key: 'title',
                    header: 'Document',
                    render: (row) => (
                      <Link
                        to={`/wells/${id}/documents?document=${row.id}`}
                        className="text-signal-deep underline-offset-2 hover:underline dark:text-signal-light"
                      >
                        {row.title}
                      </Link>
                    ),
                  },
                  { key: 'type', header: 'Type', render: (row) => <Badge tone="neutral">{row.doc_type}</Badge> },
                  { key: 'pages', header: 'Pages', render: (row) => row.page_count, align: 'end' },
                  { key: 'records', header: 'Records', render: (row) => row.extraction_summary.records, align: 'end' },
                  { key: 'evidence', header: 'Evidence', render: (row) => row.extraction_summary.evidence_links, align: 'end' },
                  {
                    key: 'flags',
                    header: 'Flags',
                    render: (row) => (
                      <span className="flex gap-1">
                        {row.has_figures && <Badge tone="warning">figures not extracted</Badge>}
                        {row.is_demo_fixture && <Badge tone="info">synthetic</Badge>}
                      </span>
                    ),
                  },
                ]}
              />
            )}
          </Async>
        </Card>
      )}

      {tab === 'twin' && (
        <Card title={t('cockpit.twin')} subtitle="state kinds are explicit: planned, actual, current, predicted, recommended">
          <Async query={twin}>{(data) => <TwinCard twin={data} />}</Async>
        </Card>
      )}

      {tab === 'audit' && (
        <Card title={t('cockpit.audit')} subtitle="every calculation keeps its inputs and outputs hash">
          <Async query={audit}>{(data) => <AuditCard audit={data} />}</Async>
        </Card>
      )}

      {tab === 'missing' && (
        <Async query={state}>
          {(data) => (
            <Card title={t('cockpit.missing')} subtitle="what the platform knows it does not know">
              <MissingCard state={data.state} />
            </Card>
          )}
        </Async>
      )}

      <EvidencePanel
        open={evidenceSubject !== null}
        onClose={() => setEvidenceSubject(null)}
        subjectKind={evidenceSubject?.kind}
        subjectId={evidenceSubject?.id}
        wellId={id}
        title={`${t('common.evidence')} · ${evidenceSubject?.kind ?? ''}`}
      />
    </div>
  )
}
