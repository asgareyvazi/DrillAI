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
import type { FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { EventCreatePayload, EventRow, OperationCreatePayload, OperationRow, WellStructure } from '../../api/types'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Button, Card, Drawer, EmptyState, ErrorState, Field, Table, Tabs } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber, humanise } from '../../lib/format'

const OPERATION_STATUS_FILTERS = ['planned', 'ready', 'in_progress', 'completed', 'suspended', 'cancelled']
const OPERATION_CLASS_FILTERS = ['plan', 'actual', 'forecast']
const EVENT_STATUS_FILTERS = ['open', 'acknowledged', 'investigating', 'closed', 'cancelled']
const MANUAL_EVENT_SEVERITIES = ['low', 'medium', 'high', 'critical']
const MANUAL_ACTUAL_OPERATION_STATUSES = ['in_progress', 'completed']
const MANUAL_OPERATION_KINDS = [
  'spud',
  'drilling',
  'slide_drilling',
  'rotary_drilling',
  'reaming',
  'back_reaming',
  'connection',
  'trip_in',
  'trip_out',
  'wiper_trip',
  'casing',
  'cementing',
  'curing_woc',
  'nipple_up',
  'pressure_test',
  'mud_conditioning',
  'circulating',
  'logging',
  'coring',
  'fishing',
  'milling',
  'jarring',
  'blow_out_preventer_test',
  'well_control',
  'completion',
  'perforating',
  'stimulation',
  'well_testing',
  'well_cleanup',
  'suspension',
  'abandonment',
  'p_and_a',
  'rig_up',
  'rig_down',
  'move',
  'maintenance',
  'waiting',
  'other',
]
const MANUAL_EVENT_KINDS = [
  'incident',
  'near_miss',
  'hazard',
  'observation',
  'equipment_failure',
  'tool_failure',
  'kick',
  'influx',
  'lost_circulation',
  'stuck_pipe',
  'twist_off',
  'washout',
  'dysfunction',
  'vibration',
  'hole_instability',
  'tight_hole',
  'poor_hole_cleaning',
  'differential_sticking',
  'wellhead_issue',
  'weather_downtime',
  'third_party_downtime',
  'rig_repair',
  'waiting_on_orders',
  'change',
  'milestone',
  'alert',
  'performance',
  'data_quality',
]
const MANUAL_NPT_CATEGORIES = [
  'equipment_failure',
  'surface_equipment',
  'downhole_tools',
  'hole_problems',
  'lost_circulation',
  'stuck_pipe',
  'well_control',
  'third_party',
  'weather',
  'waiting',
  'unclassified',
]

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

type ManualOperationFields = {
  name: string
  kind: string
  status: string
  wellboreId: string
  sectionId: string
  actualStart: string
  actualEnd: string
  duration: string
  depthFrom: string
  depthTo: string
  remarks: string
}

const operationInputClass =
  'w-full rounded border border-graphite-300 bg-white px-2 py-1 text-sm dark:border-graphite-700 dark:bg-graphite-900'

function optionalNumber(value: string): number | undefined {
  if (!value.trim()) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function localDateTimeToUtc(value: string): string | undefined {
  if (!value) return undefined
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : undefined
}

function ManualOperationForm({
  wellId,
  structure,
  onSubmit,
  onCancel,
  onChanged,
  pending,
  error,
}: {
  wellId: string
  structure: WellStructure
  onSubmit: (payload: OperationCreatePayload, key: string) => void
  onCancel: () => void
  onChanged: () => void
  pending: boolean
  error: unknown
}) {
  const { t } = useI18n()
  const firstWellbore = structure.well.active_wellbore_id ?? structure.wellbores[0]?.id ?? ''
  const [fields, setFields] = useState<ManualOperationFields>({
    name: '',
    kind: 'drilling',
    status: 'completed',
    wellboreId: firstWellbore,
    sectionId: '',
    actualStart: '',
    actualEnd: '',
    duration: '',
    depthFrom: '',
    depthTo: '',
    remarks: '',
  })
  const [idempotencyKey, setIdempotencyKey] = useState(
    () => `ui-operation-create-${wellId}-${crypto.randomUUID()}`,
  )
  const [validationError, setValidationError] = useState<string | null>(null)
  const wellbore = structure.wellbores.find((item) => item.id === fields.wellboreId) ?? null

  const set = <K extends keyof ManualOperationFields>(key: K, value: ManualOperationFields[K]) => {
    setFields((current) => ({ ...current, [key]: value }))
    setIdempotencyKey(`ui-operation-create-${wellId}-${crypto.randomUUID()}`)
    setValidationError(null)
    onChanged()
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const start = localDateTimeToUtc(fields.actualStart)
    const end = localDateTimeToUtc(fields.actualEnd)
    const depthFrom = optionalNumber(fields.depthFrom)
    const depthTo = optionalNumber(fields.depthTo)
    if (start && end && Date.parse(end) < Date.parse(start)) {
      setValidationError(t('operations.manual.timeOrder'))
      return
    }
    if (depthFrom !== undefined && depthTo !== undefined && depthTo < depthFrom) {
      setValidationError(t('operations.manual.depthOrder'))
      return
    }
    setValidationError(null)
    const payload: OperationCreatePayload = {
      well_id: wellId,
      wellbore_id: fields.wellboreId,
      name: fields.name.trim(),
      operation_class: 'actual',
      kind: fields.kind,
      status: fields.status,
      source_kind: 'manual',
    }
    if (structure.well.project_id) payload.project_id = structure.well.project_id
    if (fields.sectionId) payload.section_id = fields.sectionId
    if (start) payload.actual_start = start
    if (end) payload.actual_end = end
    const duration = optionalNumber(fields.duration)
    if (duration !== undefined) payload.actual_duration_hours = duration
    if (depthFrom !== undefined) payload.depth_from_md_si = depthFrom
    if (depthTo !== undefined) payload.depth_to_md_si = depthTo
    if (fields.remarks.trim()) payload.remarks = fields.remarks.trim()
    onSubmit(payload, idempotencyKey)
  }

  return (
    <form className="space-y-3" data-testid="manual-operation-form" onSubmit={submit}>
      <p className="text-xs text-graphite-500">{t('operations.manual.operationHint')}</p>
      {structure.wellbores.length === 0 ? (
        <EmptyState message={t('operations.manual.noWellbore')} hint={t('operations.manual.noWellboreHint')} />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('operations.manual.name')}>
              <input
                className={operationInputClass}
                value={fields.name}
                maxLength={300}
                required
                disabled={pending}
                onChange={(event) => set('name', event.target.value)}
                data-testid="manual-operation-name"
              />
            </Field>
            <Field label={t('operations.manual.kind')}>
              <select
                className={operationInputClass}
                value={fields.kind}
                disabled={pending}
                onChange={(event) => set('kind', event.target.value)}
                data-testid="manual-operation-kind"
              >
                {MANUAL_OPERATION_KINDS.map((kind) => <option key={kind} value={kind}>{humanise(kind)}</option>)}
              </select>
            </Field>
            <Field label={t('operations.manual.status')}>
              <select
                className={operationInputClass}
                value={fields.status}
                disabled={pending}
                onChange={(event) => set('status', event.target.value)}
                data-testid="manual-operation-status"
              >
                {MANUAL_ACTUAL_OPERATION_STATUSES.map((status) => <option key={status} value={status}>{humanise(status)}</option>)}
              </select>
            </Field>
            <Field label={t('operations.manual.wellbore')}>
              <select
                className={operationInputClass}
                value={fields.wellboreId}
                required
                disabled={pending}
                onChange={(event) => {
                  setFields((current) => ({ ...current, wellboreId: event.target.value, sectionId: '' }))
                  setIdempotencyKey(`ui-operation-create-${wellId}-${crypto.randomUUID()}`)
                  onChanged()
                }}
                data-testid="manual-operation-wellbore"
              >
                <option value="">{t('operations.manual.selectWellbore')}</option>
                {structure.wellbores.map((item) => (
                  <option key={item.id} value={item.id}>{item.name} · {humanise(item.status)}</option>
                ))}
              </select>
            </Field>
            <Field label={t('operations.manual.section')}>
              <select
                className={operationInputClass}
                value={fields.sectionId}
                disabled={pending || !wellbore}
                onChange={(event) => set('sectionId', event.target.value)}
                data-testid="manual-operation-section"
              >
                <option value="">{t('common.none')}</option>
                {(wellbore?.sections ?? []).map((section) => (
                  <option key={section.id} value={section.id}>{section.sequence}. {section.name}</option>
                ))}
              </select>
            </Field>
            <Field label={t('operations.manual.actualStart')} hint={t('operations.manual.timezoneHint')}>
              <input
                className={operationInputClass}
                type="datetime-local"
                value={fields.actualStart}
                disabled={pending}
                onChange={(event) => set('actualStart', event.target.value)}
                data-testid="manual-operation-start"
              />
            </Field>
            <Field label={t('operations.manual.actualEnd')}>
              <input
                className={operationInputClass}
                type="datetime-local"
                value={fields.actualEnd}
                disabled={pending}
                onChange={(event) => set('actualEnd', event.target.value)}
                data-testid="manual-operation-end"
              />
            </Field>
            <Field label={t('operations.manual.duration')}>
              <input
                className={operationInputClass}
                type="number"
                min="0"
                step="any"
                value={fields.duration}
                disabled={pending}
                onChange={(event) => set('duration', event.target.value)}
                data-testid="manual-operation-duration"
              />
            </Field>
            <Field
              label={t('operations.manual.depthFrom')}
              hint={t('operations.manual.depthHint', { datum: humanise(wellbore?.datum ?? 'unknown') })}
            >
              <input
                className={operationInputClass}
                type="number"
                min="0"
                step="any"
                value={fields.depthFrom}
                disabled={pending}
                onChange={(event) => set('depthFrom', event.target.value)}
                data-testid="manual-operation-depth-from"
              />
            </Field>
            <Field
              label={t('operations.manual.depthTo')}
              hint={t('operations.manual.depthHint', { datum: humanise(wellbore?.datum ?? 'unknown') })}
            >
              <input
                className={operationInputClass}
                type="number"
                min="0"
                step="any"
                value={fields.depthTo}
                disabled={pending}
                onChange={(event) => set('depthTo', event.target.value)}
                data-testid="manual-operation-depth-to"
              />
            </Field>
          </div>
          <Field label={t('operations.manual.remarks')}>
            <textarea
              className={operationInputClass}
              value={fields.remarks}
              maxLength={4000}
              rows={3}
              disabled={pending}
              onChange={(event) => set('remarks', event.target.value)}
              data-testid="manual-operation-remarks"
            />
          </Field>
        </>
      )}
      {validationError ? <p role="alert" className="text-sm text-danger">{validationError}</p> : null}
      {error ? <ErrorState error={error} /> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={pending || structure.wellbores.length === 0 || !fields.wellboreId} data-testid="manual-operation-submit">
          {t('operations.manual.saveOperation')}
        </Button>
        <Button onClick={onCancel} disabled={pending}>{t('common.cancel')}</Button>
      </div>
    </form>
  )
}

type ManualEventFields = {
  title: string
  kind: string
  severity: string
  occurredAt: string
  operationId: string
  description: string
  isNpt: boolean
  nptCategory: string
  nptHours: string
  rootCause: string
}

function ManualEventForm({
  wellId,
  operations,
  onSubmit,
  onCancel,
  onChanged,
  pending,
  error,
}: {
  wellId: string
  operations: OperationRow[]
  onSubmit: (payload: EventCreatePayload, key: string) => void
  onCancel: () => void
  onChanged: () => void
  pending: boolean
  error: unknown
}) {
  const { t } = useI18n()
  const [fields, setFields] = useState<ManualEventFields>({
    title: '',
    kind: 'observation',
    severity: 'low',
    occurredAt: '',
    operationId: '',
    description: '',
    isNpt: false,
    nptCategory: '',
    nptHours: '',
    rootCause: '',
  })
  const [idempotencyKey, setIdempotencyKey] = useState(
    () => `ui-event-create-${wellId}-${crypto.randomUUID()}`,
  )
  const set = <K extends keyof ManualEventFields>(key: K, value: ManualEventFields[K]) => {
    setFields((current) => ({ ...current, [key]: value }))
    setIdempotencyKey(`ui-event-create-${wellId}-${crypto.randomUUID()}`)
    onChanged()
  }
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const occurredAt = localDateTimeToUtc(fields.occurredAt)
    const nptHours = optionalNumber(fields.nptHours)
    if (fields.isNpt && (!fields.nptCategory || nptHours === undefined || nptHours <= 0)) return
    const payload: EventCreatePayload = {
      well_id: wellId,
      title: fields.title.trim(),
      kind: fields.kind,
      severity: fields.severity,
      is_npt: fields.isNpt,
      cause_basis: fields.rootCause.trim() ? 'recorded' : 'unknown',
      classification_source: 'recorded',
      source_kind: 'manual',
    }
    if (occurredAt) payload.occurred_at = occurredAt
    if (fields.operationId) payload.operation_id = fields.operationId
    if (fields.description.trim()) payload.description = fields.description.trim()
    if (fields.isNpt) {
      payload.npt_category = fields.nptCategory
      if (nptHours !== undefined) payload.npt_hours = nptHours
    }
    if (fields.rootCause.trim()) payload.root_cause = fields.rootCause.trim()
    onSubmit(payload, idempotencyKey)
  }

  return (
    <form className="space-y-3" data-testid="manual-event-form" onSubmit={submit}>
      <p className="text-xs text-graphite-500">{t('operations.manual.eventHint')}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('operations.manual.eventTitle')}>
          <input
            className={operationInputClass}
            value={fields.title}
            maxLength={400}
            required
            disabled={pending}
            onChange={(event) => set('title', event.target.value)}
            data-testid="manual-event-title"
          />
        </Field>
        <Field label={t('operations.manual.kind')}>
          <select
            className={operationInputClass}
            value={fields.kind}
            disabled={pending}
            onChange={(event) => set('kind', event.target.value)}
            data-testid="manual-event-kind"
          >
            {MANUAL_EVENT_KINDS.map((kind) => <option key={kind} value={kind}>{humanise(kind)}</option>)}
          </select>
        </Field>
        <Field label={t('operations.manual.severity')}>
          <select
            className={operationInputClass}
            value={fields.severity}
            disabled={pending}
            onChange={(event) => set('severity', event.target.value)}
            data-testid="manual-event-severity"
          >
            {MANUAL_EVENT_SEVERITIES.map((severity) => <option key={severity} value={severity}>{humanise(severity)}</option>)}
          </select>
        </Field>
        <Field label={t('operations.manual.occurredAt')} hint={t('operations.manual.eventTimeHint')}>
          <input
            className={operationInputClass}
            type="datetime-local"
            value={fields.occurredAt}
            disabled={pending}
            onChange={(event) => set('occurredAt', event.target.value)}
            data-testid="manual-event-occurred-at"
          />
        </Field>
        <Field label={t('operations.manual.linkedOperation')}>
          <select
            className={operationInputClass}
            value={fields.operationId}
            disabled={pending}
            onChange={(event) => set('operationId', event.target.value)}
            data-testid="manual-event-operation"
          >
            <option value="">{t('common.none')}</option>
            {operations.map((operation) => <option key={operation.id} value={operation.id}>{operation.sequence}. {operation.name}</option>)}
          </select>
        </Field>
      </div>
      <Field label={t('operations.manual.description')}>
        <textarea
          className={operationInputClass}
          value={fields.description}
          maxLength={8000}
          rows={3}
          disabled={pending}
          onChange={(event) => set('description', event.target.value)}
          data-testid="manual-event-description"
        />
      </Field>
      <Field label={t('operations.manual.rootCause')}>
        <input
          className={operationInputClass}
          value={fields.rootCause}
          maxLength={1000}
          disabled={pending}
          onChange={(event) => set('rootCause', event.target.value)}
          data-testid="manual-event-root-cause"
        />
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={fields.isNpt}
          disabled={pending}
          onChange={(event) => set('isNpt', event.target.checked)}
          data-testid="manual-event-is-npt"
        />
        <span>{t('operations.manual.chargeAsNpt')}</span>
      </label>
      {fields.isNpt ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('operations.manual.nptCategory')}>
            <select
              className={operationInputClass}
              value={fields.nptCategory}
              required
              disabled={pending}
              onChange={(event) => set('nptCategory', event.target.value)}
              data-testid="manual-event-npt-category"
            >
              <option value="">{t('operations.manual.selectNptCategory')}</option>
              {MANUAL_NPT_CATEGORIES.map((category) => <option key={category} value={category}>{humanise(category)}</option>)}
            </select>
          </Field>
          <Field label={t('operations.manual.nptHours')} hint="h">
            <input
              className={operationInputClass}
              type="number"
              min="0.000001"
              step="any"
              value={fields.nptHours}
              required
              disabled={pending}
              onChange={(event) => set('nptHours', event.target.value)}
              data-testid="manual-event-npt-hours"
            />
          </Field>
        </div>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={pending || !fields.title.trim() || (fields.isNpt && (!fields.nptCategory || !(optionalNumber(fields.nptHours) ?? 0)))} data-testid="manual-event-submit">
          {t('operations.manual.saveEvent')}
        </Button>
        <Button onClick={onCancel} disabled={pending}>{t('common.cancel')}</Button>
      </div>
    </form>
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
  const [creatingOperation, setCreatingOperation] = useState(false)
  const [creatingEvent, setCreatingEvent] = useState(false)

  const wellStructure = useQuery({
    queryKey: ['well-structure', id],
    queryFn: ({ signal }) => drillingApi.wellStructure(id, signal),
    enabled: creatingOperation,
  })

  const operationsForEvent = useQuery({
    queryKey: ['operations', id, 'manual-event-options'],
    queryFn: ({ signal }) => drillingApi.listOperations({ well_id: id, limit: 200 }, signal),
    enabled: tab === 'events' && creatingEvent,
  })

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

  const operationCreate = useMutation({
    mutationFn: (input: { payload: OperationCreatePayload; key: string }) =>
      drillingApi.createOperation(input.payload, input.key),
    onSuccess: () => {
      invalidate()
      setCreatingOperation(false)
    },
  })

  const eventCreate = useMutation({
    mutationFn: (input: { payload: EventCreatePayload; key: string }) =>
      drillingApi.createEvent(input.payload, input.key),
    onSuccess: () => {
      invalidate()
      setCreatingEvent(false)
    },
  })

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
            {creatingOperation ? (
              <div className="rounded-md border border-graphite-200 p-3 dark:border-graphite-800">
                <Async query={wellStructure}>
                  {(structure) => (
                    <ManualOperationForm
                      wellId={id}
                      structure={structure}
                      onSubmit={(payload, key) => operationCreate.mutate({ payload, key })}
                      onCancel={() => setCreatingOperation(false)}
                      onChanged={() => operationCreate.reset()}
                      pending={operationCreate.isPending}
                      error={operationCreate.error}
                    />
                  )}
                </Async>
              </div>
            ) : null}
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
              <Button
                variant="primary"
                onClick={() => {
                  operationCreate.reset()
                  setCreatingOperation((current) => !current)
                }}
                data-testid="manual-operation-toggle"
              >
                {creatingOperation ? t('common.cancel') : t('operations.manual.createOperation')}
              </Button>
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
            {creatingEvent ? (
              <div className="rounded-md border border-graphite-200 p-3 dark:border-graphite-800">
                <ManualEventForm
                  wellId={id}
                  operations={operationsForEvent.data?.items ?? []}
                  onSubmit={(payload, key) => eventCreate.mutate({ payload, key })}
                  onCancel={() => setCreatingEvent(false)}
                  onChanged={() => eventCreate.reset()}
                  pending={eventCreate.isPending}
                  error={eventCreate.error}
                />
                {operationsForEvent.isPending ? (
                  <p className="mt-2 text-xs text-graphite-500">{t('operations.manual.loadingOperations')}</p>
                ) : null}
                {operationsForEvent.error ? <ErrorState error={operationsForEvent.error} /> : null}
              </div>
            ) : null}
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
              <Button
                variant="primary"
                onClick={() => {
                  eventCreate.reset()
                  setCreatingEvent((current) => !current)
                }}
                data-testid="manual-event-toggle"
              >
                {creatingEvent ? t('common.cancel') : t('operations.manual.createEvent')}
              </Button>
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
