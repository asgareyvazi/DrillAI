/**
 * Operations Workspace — the well's operational record, read and corrected.
 *
 * Three subjects, one page, because they answer one question between them: *what happened on this
 * well, and how well do we know it?* Operations and events are the two first-class records; the
 * timeline is the merged view of both with the documents, engine runs and twin changes that surround
 * them.
 *
 * What the page refuses to do:
 *
 * * render a **planned** operation as though it were history — `operation_class` decides, and the
 *   badge says `plan` for a plan;
 * * show an event's **kind** as its NPT category, or its **hours** as a charge when none was booked:
 *   the kind, the category and the hours are three separate claims and are displayed separately;
 * * present an **inferred** cause as a **recorded** one: the basis travels with the cause text;
 * * turn a **failed read** into an empty list: an error state and an empty state are different
 *   screens;
 * * hide **provenance**: every promoted row links back to the document it was read from.
 *
 * Corrections and transitions go through the audited endpoints with the version the reader was
 * looking at, so two people editing the same operation cannot silently overwrite each other.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { EventRow, OperationRow } from '../../api/types'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Button, Card, Drawer, EmptyState, ErrorState, Field, Table, Tabs } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber, humanise } from '../../lib/format'

const OPERATION_STATUS_FILTERS = ['planned', 'ready', 'in_progress', 'completed', 'suspended', 'cancelled']
const OPERATION_CLASS_FILTERS = ['plan', 'actual', 'forecast']
const EVENT_STATUS_FILTERS = ['open', 'acknowledged', 'investigating', 'closed', 'cancelled']

function QualityBadge({ quality }: { quality: string | null }) {
  if (!quality) return null
  const tone = quality === 'extracted' || quality === 'rule_validated' ? 'info' : quality === 'human_validated' ? 'ok' : 'neutral'
  return <Badge tone={tone}>{humanise(quality)}</Badge>
}

function CauseBadge({ basis }: { basis: string }) {
  const { t } = useI18n()
  const tone = basis === 'recorded' ? 'ok' : basis === 'inferred' ? 'warning' : 'neutral'
  return <Badge tone={tone}>{t(`operations.cause.${basis === 'recorded' || basis === 'inferred' ? basis : 'unknown'}`)}</Badge>
}

/** The provenance strip: where this row came from, and how to get back to the source. */
function Provenance({ row, wellId }: { row: { source_kind: string | null; source_document_id: string | null; source_record_id: string | null; promotion_fingerprint?: string | null }; wellId: string }) {
  const { t } = useI18n()
  if (!row.source_kind && !row.source_document_id) return <p className="text-[11px] text-graphite-500">{t('operations.detail.noProvenance')}</p>
  return (
    <dl className="grid grid-cols-1 gap-1 text-[11px] sm:grid-cols-2">
      <div>
        <dt className="text-graphite-500">{t('operations.detail.sourceKind')}</dt>
        <dd>{row.source_kind ? humanise(row.source_kind) : t('operations.detail.notStated')}</dd>
      </div>
      <div>
        <dt className="text-graphite-500">{t('operations.detail.sourceRecord')}</dt>
        <dd className="font-mono break-all" dir="ltr">
          {row.source_record_id ?? t('operations.detail.notStated')}
        </dd>
      </div>
      {row.source_document_id && (
        <div className="sm:col-span-2">
          <dt className="text-graphite-500">{t('operations.detail.sourceDocument')}</dt>
          <dd>
            <Link className="underline" to={`/wells/${wellId}/documents?document=${row.source_document_id}`}>
              <span className="font-mono" dir="ltr">
                {row.source_document_id}
              </span>
            </Link>
          </dd>
        </div>
      )}
      {row.promotion_fingerprint && (
        <div className="sm:col-span-2">
          <dt className="text-graphite-500">{t('operations.detail.fingerprint')}</dt>
          <dd className="font-mono break-all" dir="ltr">
            {row.promotion_fingerprint}
          </dd>
        </div>
      )}
    </dl>
  )
}

function TransitionControls({
  row,
  onTransition,
  pending,
}: {
  row: OperationRow | EventRow
  onTransition: (status: string, reason: string) => void
  pending: boolean
}) {
  const { t } = useI18n()
  const [reason, setReason] = useState('')
  if (row.allowed_transitions.length === 0) {
    return <p className="text-[11px] text-graphite-500">{t('operations.detail.terminal')}</p>
  }
  return (
    <div className="space-y-2">
      <Field label={t('operations.detail.transitionReason')} hint={t('operations.detail.transitionReasonHint')}>
        <input
          className="w-full rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          data-testid="transition-reason"
        />
      </Field>
      <div className="flex flex-wrap gap-2">
        {row.allowed_transitions.map((status) => (
          <Button
            key={status}
            disabled={pending}
            onClick={() => onTransition(status, reason)}
            data-testid={`transition-${status}`}
          >
            {t('operations.detail.moveTo', { status: humanise(status) })}
          </Button>
        ))}
      </div>
    </div>
  )
}

function CorrectionForm({
  row,
  fields,
  onSubmit,
  pending,
  error,
}: {
  row: OperationRow | EventRow
  fields: string[]
  onSubmit: (field: string, value: string) => void
  pending: boolean
  error: unknown
}) {
  const { t } = useI18n()
  const [field, setField] = useState(fields[0] ?? '')
  const [value, setValue] = useState('')
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Field label={t('operations.detail.field')}>
          <select
            className="w-full rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            value={field}
            onChange={(event) => setField(event.target.value)}
            data-testid="correction-field"
          >
            {fields.map((name) => (
              <option key={name} value={name}>
                {humanise(name)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('operations.detail.newValue')}>
          <input
            className="w-full rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            data-testid="correction-value"
          />
        </Field>
      </div>
      <Button
        disabled={pending || !field || !row.updated_at}
        onClick={() => onSubmit(field, value)}
        data-testid="correction-submit"
      >
        {t('operations.detail.correct')}
      </Button>
      {error ? <ErrorState error={error} /> : null}
    </div>
  )
}

export default function OperationsWorkspace() {
  const { t } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') ?? 'operations'
  const selectedOperation = searchParams.get('operation')
  const selectedEvent = searchParams.get('event')
  const kindFilter = searchParams.get('kind')
  const [statusFilter, setStatusFilter] = useState('')
  const [classFilter, setClassFilter] = useState('')
  const [nptOnly, setNptOnly] = useState(false)

  const operations = useQuery({
    queryKey: ['operations', id, statusFilter, classFilter],
    queryFn: ({ signal }) =>
      drillingApi.listOperations(
        {
          well_id: id,
          limit: 200,
          ...(statusFilter ? { status: statusFilter } : {}),
          ...(classFilter ? { operation_class: classFilter } : {}),
        },
        signal,
      ),
    enabled: tab === 'operations',
  })

  const events = useQuery({
    queryKey: ['events', id, statusFilter, nptOnly],
    queryFn: ({ signal }) =>
      drillingApi.listEvents(
        { well_id: id, limit: 200, ...(statusFilter ? { status: statusFilter } : {}), ...(nptOnly ? { is_npt: true } : {}) },
        signal,
      ),
    enabled: tab === 'events',
  })

  const timeline = useQuery({
    queryKey: ['timeline', id, kindFilter],
    queryFn: ({ signal }) =>
      drillingApi.wellTimeline(id, { limit: 100, ...(kindFilter ? { kinds: [kindFilter] } : {}) }, signal),
    enabled: tab === 'timeline',
  })

  const operation = useMemo<OperationRow | null>(
    () => operations.data?.items.find((row) => row.id === selectedOperation) ?? null,
    [operations.data, selectedOperation],
  )
  const event = useMemo<EventRow | null>(
    () => events.data?.items.find((row) => row.id === selectedEvent) ?? null,
    [events.data, selectedEvent],
  )

  /**
   * Targeted invalidation.
   *
   * A mutation on one record invalidates the queries that record appears in — its own list and the
   * merged timeline — and nothing else. Invalidating the whole cache would refetch every screen the
   * reader is not looking at, and the well state is refetched only because NPT and productivity
   * numbers live there.
   */
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['operations', id] })
    queryClient.invalidateQueries({ queryKey: ['events', id] })
    queryClient.invalidateQueries({ queryKey: ['timeline', id] })
    queryClient.invalidateQueries({ queryKey: ['well-state', id] })
  }

  const operationTransition = useMutation({
    mutationFn: (input: { row: OperationRow; status: string; reason: string; key: string }) =>
      drillingApi.transitionOperation(
        input.row.id,
        {
          status: input.status,
          expected_updated_at: input.row.updated_at ?? undefined,
          ...(input.reason ? { reason: input.reason } : {}),
        },
        input.key,
      ),
    onSuccess: invalidate,
  })

  const operationCorrection = useMutation({
    mutationFn: (input: { row: OperationRow; field: string; value: string; key: string }) =>
      drillingApi.updateOperation(
        input.row.id,
        {
          expected_updated_at: input.row.updated_at as string,
          reason: t('operations.detail.correctionReason'),
          changes: { [input.field]: coerce(input.value) },
        },
        input.key,
      ),
    onSuccess: invalidate,
  })

  const eventTransition = useMutation({
    mutationFn: (input: { row: EventRow; status: string; reason: string; key: string }) =>
      drillingApi.transitionEvent(
        input.row.id,
        {
          status: input.status,
          expected_updated_at: input.row.updated_at ?? undefined,
          ...(input.reason ? { reason: input.reason } : {}),
        },
        input.key,
      ),
    onSuccess: invalidate,
  })

  const selectTab = (key: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', key)
    next.delete('operation')
    next.delete('event')
    setSearchParams(next)
  }

  const openOperation = (row: OperationRow) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', 'operations')
    next.set('operation', row.id)
    setSearchParams(next)
  }

  const openEvent = (row: EventRow) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', 'events')
    next.set('event', row.id)
    setSearchParams(next)
  }

  const closeDrawer = () => {
    const next = new URLSearchParams(searchParams)
    next.delete('operation')
    next.delete('event')
    setSearchParams(next)
  }

  const drawerOpen = Boolean(operation || event)

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('operations.title')}
        description={t('operations.subtitle')}
        actions={
          <Link className="text-xs underline" to={`/wells/${id}/cockpit`}>
            {t('operations.backToCockpit')}
          </Link>
        }
      />

      <Tabs
        tabs={[
          { key: 'operations', label: t('operations.tabs.operations'), badge: operations.data?.total },
          { key: 'events', label: t('operations.tabs.events'), badge: events.data?.total },
          { key: 'timeline', label: t('operations.tabs.timeline'), badge: timeline.data?.count },
        ]}
        active={tab}
        onChange={selectTab}
      >
        {tab === 'operations' && (
          <Card className="space-y-3">
            <div className="flex flex-wrap items-end gap-3">
              <Field label={t('operations.filters.status')}>
                <select
                  className="rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900"
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value)}
                  data-testid="operation-status-filter"
                >
                  <option value="">{t('operations.filters.any')}</option>
                  {OPERATION_STATUS_FILTERS.map((status) => (
                    <option key={status} value={status}>
                      {humanise(status)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('operations.filters.class')}>
                <select
                  className="rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900"
                  value={classFilter}
                  onChange={(event) => setClassFilter(event.target.value)}
                  data-testid="operation-class-filter"
                >
                  <option value="">{t('operations.filters.any')}</option>
                  {OPERATION_CLASS_FILTERS.map((klass) => (
                    <option key={klass} value={klass}>
                      {humanise(klass)}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <Async query={operations}>
              {(data) => (
                <Table
                  rows={data.items}
                  rowKey={(row) => row.id}
                  onRowClick={openOperation}
                  isRowActive={(row) => row.id === selectedOperation}
                  empty={<EmptyState message={t('operations.empty.operations')} hint={t('operations.empty.operationsHint')} />}
                  columns={[
                    { key: 'sequence', header: '#', render: (row) => row.sequence, align: 'end' },
                    { key: 'name', header: t('operations.columns.name'), render: (row) => row.name },
                    { key: 'kind', header: t('operations.columns.kind'), render: (row) => humanise(row.kind) },
                    {
                      key: 'class',
                      header: t('operations.columns.class'),
                      render: (row) => <Badge tone={row.is_planned ? 'info' : 'neutral'}>{humanise(row.operation_class)}</Badge>,
                    },
                    { key: 'status', header: t('operations.columns.status'), render: (row) => humanise(row.status) },
                    {
                      key: 'hours',
                      header: t('operations.columns.hours'),
                      align: 'end',
                      // A plan has planned hours; an actual has actual hours. Showing the plan's number
                      // in the actual column is how a programme becomes a record.
                      render: (row) =>
                        row.is_planned
                          ? formatNumber(row.planned_duration_hours)
                          : formatNumber(row.actual_duration_hours),
                    },
                    {
                      key: 'quality',
                      header: t('operations.columns.quality'),
                      render: (row) => <QualityBadge quality={row.data_quality} />,
                    },
                  ]}
                />
              )}
            </Async>
            <p className="text-[11px] text-graphite-500">{t('operations.tableCount', { count: operations.data?.total ?? 0 })}</p>
          </Card>
        )}

        {tab === 'events' && (
          <Card className="space-y-3">
            <div className="flex flex-wrap items-end gap-3">
              <Field label={t('operations.filters.status')}>
                <select
                  className="rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900"
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value)}
                  data-testid="event-status-filter"
                >
                  <option value="">{t('operations.filters.any')}</option>
                  {EVENT_STATUS_FILTERS.map((status) => (
                    <option key={status} value={status}>
                      {humanise(status)}
                    </option>
                  ))}
                </select>
              </Field>
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={nptOnly}
                  onChange={(event) => setNptOnly(event.target.checked)}
                  data-testid="event-npt-filter"
                />
                {t('operations.filters.nptOnly')}
              </label>
            </div>

            <Async query={events}>
              {(data) => (
                <Table
                  rows={data.items}
                  rowKey={(row) => row.id}
                  onRowClick={openEvent}
                  isRowActive={(row) => row.id === selectedEvent}
                  empty={<EmptyState message={t('operations.empty.events')} hint={t('operations.empty.eventsHint')} />}
                  columns={[
                    {
                      key: 'occurred_at',
                      header: t('operations.columns.occurred'),
                      render: (row) => <span dir="ltr">{formatDateTime(row.occurred_at)}</span>,
                    },
                    { key: 'title', header: t('operations.columns.title'), render: (row) => row.title },
                    // The event's kind, its NPT category and its hours are three different claims.
                    { key: 'kind', header: t('operations.columns.kind'), render: (row) => humanise(row.kind) },
                    {
                      key: 'npt',
                      header: t('operations.columns.npt'),
                      render: (row) =>
                        row.is_npt ? (
                          <Badge tone="warning">
                            {humanise(row.npt_category ?? 'unclassified')} · {formatNumber(row.npt_hours)} h
                          </Badge>
                        ) : (
                          <span className="text-graphite-500">{t('operations.npt.notCharged')}</span>
                        ),
                    },
                    {
                      key: 'cause',
                      header: t('operations.columns.cause'),
                      render: (row) => <CauseBadge basis={row.cause_basis} />,
                    },
                    { key: 'severity', header: t('operations.columns.severity'), render: (row) => humanise(row.severity) },
                    { key: 'status', header: t('operations.columns.status'), render: (row) => humanise(row.status) },
                  ]}
                />
              )}
            </Async>
            <p className="text-[11px] text-graphite-500">{t('operations.tableCount', { count: events.data?.total ?? 0 })}</p>
          </Card>
        )}

        {tab === 'timeline' && (
          <Card className="space-y-3">
            <div className="flex flex-wrap gap-1">
              <Button onClick={() => setSearchParams({ tab: 'timeline' })} data-testid="timeline-kind-all">
                {t('operations.filters.any')}
              </Button>
              {(timeline.data?.kinds_available ?? []).map((kind) => (
                <Button
                  key={kind}
                  onClick={() => setSearchParams({ tab: 'timeline', kind })}
                  data-testid={`timeline-kind-${kind}`}
                >
                  {humanise(kind)}
                </Button>
              ))}
            </div>
            <Async query={timeline}>
              {(data) =>
                data.entries.length === 0 ? (
                  <EmptyState message={t('operations.empty.timeline')} hint={t('operations.empty.timelineHint')} />
                ) : (
                  <ol className="space-y-2" data-testid="timeline-entries">
                    {data.entries.map((entry) => (
                      <li
                        key={`${entry.kind}-${entry.id}`}
                        className="rounded-md border border-graphite-100 p-2 dark:border-graphite-800"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
                            {formatDateTime(entry.at)}
                          </span>
                          <Badge tone="neutral">{humanise(entry.kind)}</Badge>
                          <span className="text-sm font-medium">{entry.title}</span>
                          {entry.npt && entry.npt_hours ? (
                            <Badge tone="warning">{formatNumber(entry.npt_hours)} h NPT</Badge>
                          ) : null}
                          {entry.status ? <span className="text-[11px] text-graphite-500">{humanise(entry.status)}</span> : null}
                        </div>
                        {entry.summary ? (
                          <p className="mt-1 text-[11px] text-graphite-600 dark:text-graphite-300">{entry.summary}</p>
                        ) : null}
                        {entry.document_id ? (
                          <Link
                            className="mt-1 inline-block text-[11px] underline"
                            to={`/wells/${id}/documents?document=${entry.document_id}`}
                          >
                            {t('operations.detail.sourceDocument')}
                          </Link>
                        ) : null}
                      </li>
                    ))}
                  </ol>
                )
              }
            </Async>
            <p className="text-[11px] text-graphite-500">
              {timeline.data?.next_cursor ? t('operations.timeline.more') : t('operations.timeline.end')}
            </p>
          </Card>
        )}
      </Tabs>

      <Drawer
        open={drawerOpen}
        wide
        title={operation ? operation.name : event ? event.title : ''}
        onClose={closeDrawer}
      >
        {operation && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <Badge tone={operation.is_planned ? 'info' : 'neutral'}>{humanise(operation.operation_class)}</Badge>
              <Badge tone="neutral">{humanise(operation.status)}</Badge>
              <Badge tone="neutral">{humanise(operation.kind)}</Badge>
              <QualityBadge quality={operation.data_quality} />
            </div>
            <dl className="grid grid-cols-2 gap-2 text-xs">
              {[
                [t('operations.detail.sequence'), String(operation.sequence)],
                [t('operations.detail.phase'), humanise(operation.phase)],
                [t('operations.detail.actualStart'), formatDateTime(operation.actual_start)],
                [t('operations.detail.actualEnd'), formatDateTime(operation.actual_end)],
                [t('operations.detail.plannedStart'), formatDateTime(operation.planned_start)],
                [t('operations.detail.plannedEnd'), formatDateTime(operation.planned_end)],
                [t('operations.detail.actualHours'), formatNumber(operation.actual_duration_hours)],
                [t('operations.detail.plannedHours'), formatNumber(operation.planned_duration_hours)],
                [t('operations.detail.nptHours'), formatNumber(operation.npt_hours)],
                [t('operations.detail.productive'), operation.is_productive === null ? t('operations.detail.notStated') : operation.is_productive ? t('common.yes') : t('common.no')],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-graphite-500">{label}</dt>
                  <dd dir="auto">{value}</dd>
                </div>
              ))}
            </dl>
            <Provenance row={operation} wellId={id} />
            <TransitionControls
              row={operation}
              pending={operationTransition.isPending}
              onTransition={(status, reason) =>
                operationTransition.mutate({
                  row: operation,
                  status,
                  reason,
                  // Minted once per user action: a retry of the same action reuses it, so the server
                  // recognises the second attempt instead of recording a second transition.
                  key: `ui-operation-transition-${operation.id}-${crypto.randomUUID()}`,
                })
              }
            />
            {operationTransition.error ? <ErrorState error={operationTransition.error} /> : null}
            <CorrectionForm
              row={operation}
              fields={['remarks', 'npt_hours', 'actual_duration_hours', 'depth_to_md_si']}
              pending={operationCorrection.isPending}
              error={operationCorrection.error}
              onSubmit={(field, value) =>
                operationCorrection.mutate({
                  row: operation,
                  field,
                  value,
                  key: `ui-operation-correct-${operation.id}-${crypto.randomUUID()}`,
                })
              }
            />
          </div>
        )}

        {event && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2">
              <Badge tone="neutral">{humanise(event.kind)}</Badge>
              <Badge tone="neutral">{humanise(event.severity)}</Badge>
              <Badge tone="neutral">{humanise(event.status)}</Badge>
              <CauseBadge basis={event.cause_basis} />
              {event.classification_source !== 'recorded' ? (
                <Badge tone="info">{humanise(event.classification_source)}</Badge>
              ) : null}
            </div>
            <dl className="grid grid-cols-2 gap-2 text-xs">
              {[
                [t('operations.detail.occurred'), formatDateTime(event.occurred_at)],
                [t('operations.detail.ended'), formatDateTime(event.ended_at)],
                [t('operations.detail.nptCategory'), event.npt_category ? humanise(event.npt_category) : t('operations.npt.notCharged')],
                [t('operations.detail.nptHours'), event.is_npt ? formatNumber(event.npt_hours) : t('operations.npt.notCharged')],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-graphite-500">{label}</dt>
                  <dd dir="auto">{value}</dd>
                </div>
              ))}
            </dl>
            {event.description ? (
              <div>
                <p className="text-[11px] text-graphite-500">{t('operations.detail.description')}</p>
                <p className="text-xs" dir="auto">
                  {event.description}
                </p>
              </div>
            ) : null}
            <div>
              <p className="text-[11px] text-graphite-500">{t('operations.detail.rootCause')}</p>
              <p className="text-xs" dir="auto">
                {event.root_cause ?? t('operations.detail.noCause')}
              </p>
            </div>
            <Provenance row={event} wellId={id} />
            <TransitionControls
              row={event}
              pending={eventTransition.isPending}
              onTransition={(status, reason) =>
                eventTransition.mutate({
                  row: event,
                  status,
                  reason,
                  key: `ui-event-transition-${event.id}-${crypto.randomUUID()}`,
                })
              }
            />
            {eventTransition.error ? <ErrorState error={eventTransition.error} /> : null}
          </div>
        )}
      </Drawer>
    </div>
  )
}

/**
 * A form field arrives as text; the API validates it.
 *
 * Coercion is limited to the two shapes the editable numeric fields have, and anything else is sent
 * as written so the backend's `ValidationFailed` names the field — the client is not the authority on
 * what a value means.
 */
function coerce(value: string): unknown {
  const trimmed = value.trim()
  if (trimmed === '') return null
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed)
  return trimmed
}
