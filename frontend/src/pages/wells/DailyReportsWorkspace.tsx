import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState, type Dispatch, type FormEvent, type ReactNode, type SetStateAction } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  type DailyReport,
  type DailyReportActivityInput,
  type DailyReportCatalogue,
  type DailyReportContentInput,
  type DailyReportCreatePayload,
  type DailyReportDecisionPayload,
  type DailyReportEditPayload,
  type DailyReportMeasurementInput,
  type DailyReportRevision,
  type DailyReportStatus,
  type DailyReportTimeBasis,
  type Wellbore,
} from '../../api/types'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import { Badge, Button, Card, EmptyState, ErrorState, Loading } from '../../components/common'
import { PageHeader } from '../../components/layout/AppShell'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber } from '../../lib/format'
import { useActionGate } from '../../hooks/useActionGate'
import { useIdentity } from '../../hooks/useIdentity'

let localRowSequence = 0
const localRowKey = () => `daily-report-row-${++localRowSequence}`
const EXPLICIT_OFFSET = /(?:Z|[+-](?:0\d|1\d|2[0-3]):?[0-5]\d)$/i
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

type ActivityDraft = {
  key: string
  activity_kind: string
  operation_kind: string
  npt_category: string
  title: string
  description: string
  start_at: string
  start_time_basis: string
  end_at: string
  end_time_basis: string
  duration_value: string
  duration_unit: string
  duration_basis: string
  depth_from_value: string
  depth_from_unit: string
  depth_to_value: string
  depth_to_unit: string
  depth_reference: string
  depth_basis: string
  cause_basis: string
}

type MeasurementDraft = {
  key: string
  parameter_key: string
  value: string
  unit: string
  value_basis: string
  observed_at: string
  time_basis: string
  depth_value: string
  depth_unit: string
  depth_reference: string
  depth_basis: string
}

type ReportFormState = {
  wellbore_id: string
  report_date: string
  report_timezone: string
  shift_code: string
  shift_label: string
  report_number: string
  period_start_at: string
  period_end_at: string
  summary: string
  operations_summary: string
  safety_notes: string
  next_period_plan: string
  environmental_notes: string
  reason: string
  activities: ActivityDraft[]
  measurements: MeasurementDraft[]
}

type FormMode = 'create' | 'edit'
type ActionMode = 'submit' | 'approve' | 'reject' | 'withdraw' | 'correct'

type ReportActionVariables = {
  action: Exclude<ActionMode, 'correct'>
  reportId: string
  revisionNumber: number
  body: DailyReportDecisionPayload
  idempotencyKey: string
}

type CorrectionVariables = {
  reportId: string
  reason: string
  idempotencyKey: string
}

type CreateVariables = { payload: DailyReportCreatePayload; idempotencyKey: string }
type EditVariables = {
  reportId: string
  revisionNumber: number
  payload: DailyReportEditPayload
  idempotencyKey: string
}

function emptyActivity(): ActivityDraft {
  return {
    key: localRowKey(),
    activity_kind: '',
    operation_kind: '',
    npt_category: '',
    title: '',
    description: '',
    start_at: '',
    start_time_basis: 'unknown',
    end_at: '',
    end_time_basis: 'unknown',
    duration_value: '',
    duration_unit: '',
    duration_basis: 'unknown',
    depth_from_value: '',
    depth_from_unit: '',
    depth_to_value: '',
    depth_to_unit: '',
    depth_reference: 'unknown',
    depth_basis: 'unknown',
    cause_basis: 'unknown',
  }
}

function emptyMeasurement(): MeasurementDraft {
  return {
    key: localRowKey(),
    parameter_key: '',
    value: '',
    unit: '',
    value_basis: 'reported',
    observed_at: '',
    time_basis: 'unknown',
    depth_value: '',
    depth_unit: '',
    depth_reference: 'unknown',
    depth_basis: 'unknown',
  }
}

function emptyForm(): ReportFormState {
  return {
    wellbore_id: '',
    report_date: '',
    report_timezone: '',
    shift_code: '',
    shift_label: '',
    report_number: '',
    period_start_at: '',
    period_end_at: '',
    summary: '',
    operations_summary: '',
    safety_notes: '',
    next_period_plan: '',
    environmental_notes: '',
    reason: '',
    activities: [],
    measurements: [],
  }
}

function revisionForm(report: DailyReport, revision: DailyReportRevision): ReportFormState {
  return {
    wellbore_id: report.wellbore_id,
    report_date: report.report_date,
    report_timezone: report.report_timezone,
    shift_code: report.shift_code,
    shift_label: report.shift_label ?? '',
    report_number: report.report_number ?? '',
    period_start_at: revision.period_start_at ?? '',
    period_end_at: revision.period_end_at ?? '',
    summary: revision.summary ?? '',
    operations_summary: revision.operations_summary ?? '',
    safety_notes: revision.safety_notes ?? '',
    next_period_plan: revision.next_period_plan ?? '',
    environmental_notes: revision.environmental_notes ?? '',
    reason: '',
    activities: revision.activities.map((activity) => ({
      key: localRowKey(),
      activity_kind: activity.activity_kind,
      operation_kind: activity.operation_kind ?? '',
      npt_category: activity.npt_category ?? '',
      title: activity.title,
      description: activity.description ?? '',
      start_at: activity.start_at ?? '',
      start_time_basis: activity.start_time_basis,
      end_at: activity.end_at ?? '',
      end_time_basis: activity.end_time_basis,
      duration_value: activity.duration_source_value === null ? '' : String(activity.duration_source_value),
      duration_unit: activity.duration_source_unit ?? '',
      duration_basis: activity.duration_basis,
      depth_from_value: activity.depth_from_source_value === null ? '' : String(activity.depth_from_source_value),
      depth_from_unit: activity.depth_from_source_unit ?? '',
      depth_to_value: activity.depth_to_source_value === null ? '' : String(activity.depth_to_source_value),
      depth_to_unit: activity.depth_to_source_unit ?? '',
      depth_reference: activity.depth_reference,
      depth_basis: activity.depth_basis,
      cause_basis: activity.cause_basis,
    })),
    measurements: revision.measurements.map((measurement) => ({
      key: localRowKey(),
      parameter_key: measurement.parameter_key,
      value: String(measurement.source_value),
      unit: measurement.source_unit,
      value_basis: measurement.value_basis,
      observed_at: measurement.observed_at ?? '',
      time_basis: measurement.time_basis,
      depth_value: measurement.depth_source_value === null ? '' : String(measurement.depth_source_value),
      depth_unit: measurement.depth_source_unit ?? '',
      depth_reference: measurement.depth_reference,
      depth_basis: measurement.depth_basis,
    })),
  }
}

function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `daily-report-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function hasExplicitOffset(value: string): boolean {
  return EXPLICIT_OFFSET.test(value.trim()) && Number.isFinite(Date.parse(value.trim()))
}

function validDate(value: string): boolean {
  if (!DATE_ONLY.test(value)) return false
  const parts = value.split('-').map(Number)
  if (parts.length !== 3 || parts.some((part) => !Number.isInteger(part))) return false
  const [year, month, day] = parts
  if (year === undefined || month === undefined || day === undefined) return false
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
}

function finiteNumber(value: string): number | null {
  if (!value.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function nonEmpty(value: string): string | null {
  return value.trim() || null
}

function activityHasInput(activity: ActivityDraft): boolean {
  return Boolean(
    activity.activity_kind ||
      activity.operation_kind ||
      activity.npt_category ||
      activity.title.trim() ||
      activity.description.trim() ||
      activity.start_at.trim() ||
      activity.end_at.trim() ||
      activity.duration_value.trim() ||
      activity.duration_unit ||
      activity.depth_from_value.trim() ||
      activity.depth_from_unit ||
      activity.depth_to_value.trim() ||
      activity.depth_to_unit,
  )
}

function measurementHasInput(measurement: MeasurementDraft): boolean {
  return Boolean(
    measurement.parameter_key ||
      measurement.value.trim() ||
      measurement.unit ||
      measurement.observed_at.trim() ||
      measurement.depth_value.trim() ||
      measurement.depth_unit ||
      measurement.depth_reference !== 'unknown',
  )
}

function validateForm(form: ReportFormState, catalogue: DailyReportCatalogue, mode: FormMode): string | null {
  if (!form.wellbore_id) return 'wellbore'
  if (!validDate(form.report_date)) return 'date'
  if (!form.report_timezone.trim()) return 'timezone'
  try {
    new Intl.DateTimeFormat('en', { timeZone: form.report_timezone.trim() })
  } catch {
    return 'timezone'
  }
  if (!catalogue.shift_codes.includes(form.shift_code)) return 'shift'
  if (form.shift_code === 'custom' && !form.shift_label.trim()) return 'customShift'
  if (form.period_start_at.trim() && !hasExplicitOffset(form.period_start_at)) return 'timestamp'
  if (form.period_end_at.trim() && !hasExplicitOffset(form.period_end_at)) return 'timestamp'
  if (
    form.period_start_at.trim() &&
    form.period_end_at.trim() &&
    Date.parse(form.period_end_at) < Date.parse(form.period_start_at)
  ) {
    return 'period'
  }
  if (mode === 'edit' && !form.reason.trim()) return 'reason'

  for (const activity of form.activities.filter(activityHasInput)) {
    if (!activity.title.trim()) return 'activityTitle'
    if (!catalogue.activity_kinds.includes(activity.activity_kind)) return 'activityKind'
    if (activity.activity_kind === 'operation' && !catalogue.operation_kinds.includes(activity.operation_kind)) {
      return 'operationKind'
    }
    if (activity.activity_kind === 'npt' && !catalogue.npt_categories.includes(activity.npt_category)) {
      return 'nptCategory'
    }
    if (activity.start_at.trim() && (!hasExplicitOffset(activity.start_at) || activity.start_time_basis === 'unknown')) {
      return 'timestamp'
    }
    if (activity.end_at.trim() && (!hasExplicitOffset(activity.end_at) || activity.end_time_basis === 'unknown')) {
      return 'timestamp'
    }
    if (activity.duration_value.trim() || activity.duration_unit) {
      const duration = finiteNumber(activity.duration_value)
      if (duration === null || duration < 0 || !activity.duration_unit || activity.duration_basis === 'unknown') {
        return 'duration'
      }
    }
    const hasDepth = Boolean(
      activity.depth_from_value.trim() ||
        activity.depth_from_unit ||
        activity.depth_to_value.trim() ||
        activity.depth_to_unit,
    )
    if (hasDepth) {
      const from = finiteNumber(activity.depth_from_value)
      const to = finiteNumber(activity.depth_to_value)
      if (
        ((activity.depth_from_value.trim() || activity.depth_from_unit) && (from === null || from < 0 || !activity.depth_from_unit)) ||
        ((activity.depth_to_value.trim() || activity.depth_to_unit) && (to === null || to < 0 || !activity.depth_to_unit)) ||
        activity.depth_reference === 'unknown' ||
        activity.depth_basis === 'unknown'
      ) {
        return 'depth'
      }
    }
  }

  for (const measurement of form.measurements.filter(measurementHasInput)) {
    const parameter = catalogue.parameters.find((item) => item.key === measurement.parameter_key)
    const value = finiteNumber(measurement.value)
    if (!parameter || value === null || !measurement.unit || !parameter.accepted_units.includes(measurement.unit)) {
      return 'value'
    }
    if (measurement.value_basis === 'unknown') return 'basis'
    if (measurement.observed_at.trim() && (!hasExplicitOffset(measurement.observed_at) || measurement.time_basis === 'unknown')) {
      return 'timestamp'
    }
    const hasDepth = Boolean(measurement.depth_value.trim() || measurement.depth_unit)
    if (hasDepth) {
      const depth = finiteNumber(measurement.depth_value)
      if (
        depth === null ||
        depth < 0 ||
        !measurement.depth_unit ||
        measurement.depth_reference === 'unknown' ||
        measurement.depth_basis === 'unknown'
      ) {
        return 'depth'
      }
    }
    if (parameter.required_depth_reference && measurement.depth_reference !== parameter.required_depth_reference) {
      return 'depth'
    }
  }
  return null
}

function toContent(form: ReportFormState): DailyReportContentInput {
  const activities: DailyReportActivityInput[] = form.activities.filter(activityHasInput).map((activity) => {
    const duration = finiteNumber(activity.duration_value)
    const from = finiteNumber(activity.depth_from_value)
    const to = finiteNumber(activity.depth_to_value)
    const hasDepth = from !== null || to !== null
    return {
      activity_kind: activity.activity_kind,
      ...(activity.activity_kind === 'operation' ? { operation_kind: activity.operation_kind } : {}),
      ...(activity.activity_kind === 'npt' ? { npt_category: activity.npt_category } : {}),
      title: activity.title.trim(),
      description: nonEmpty(activity.description),
      start_at: nonEmpty(activity.start_at),
      start_time_basis: (activity.start_at.trim() ? activity.start_time_basis : 'unknown') as DailyReportTimeBasis,
      end_at: nonEmpty(activity.end_at),
      end_time_basis: (activity.end_at.trim() ? activity.end_time_basis : 'unknown') as DailyReportTimeBasis,
      duration_value: duration,
      duration_unit: duration === null ? null : activity.duration_unit,
      duration_basis: (duration === null ? 'unknown' : activity.duration_basis) as DailyReportTimeBasis,
      depth_from_value: from,
      depth_from_unit: from === null ? null : activity.depth_from_unit,
      depth_to_value: to,
      depth_to_unit: to === null ? null : activity.depth_to_unit,
      depth_reference: hasDepth ? activity.depth_reference : 'unknown',
      depth_basis: (hasDepth ? activity.depth_basis : 'unknown') as DailyReportTimeBasis,
      cause_basis: activity.cause_basis || 'unknown',
    }
  })
  const measurements: DailyReportMeasurementInput[] = form.measurements
    .filter(measurementHasInput)
    .map((measurement) => {
      const depth = finiteNumber(measurement.depth_value)
      return {
        parameter_key: measurement.parameter_key,
        value: finiteNumber(measurement.value) ?? 0,
        unit: measurement.unit,
        value_basis: measurement.value_basis as DailyReportTimeBasis,
        observed_at: nonEmpty(measurement.observed_at),
        time_basis: (measurement.observed_at.trim() ? measurement.time_basis : 'unknown') as DailyReportTimeBasis,
        depth_value: depth,
        depth_unit: depth === null ? null : measurement.depth_unit,
        depth_reference: measurement.depth_reference,
        depth_basis: (depth === null ? 'unknown' : measurement.depth_basis) as DailyReportTimeBasis,
      }
    })
  return {
    period_start_at: nonEmpty(form.period_start_at),
    period_end_at: nonEmpty(form.period_end_at),
    summary: nonEmpty(form.summary),
    operations_summary: nonEmpty(form.operations_summary),
    safety_notes: nonEmpty(form.safety_notes),
    next_period_plan: nonEmpty(form.next_period_plan),
    environmental_notes: nonEmpty(form.environmental_notes),
    activities,
    measurements,
  }
}

function labelEnum(t: (key: string) => string, group: string, value: string | null | undefined): string {
  if (!value) return '—'
  const translated = t(`dailyReports.enums.${group}.${value}`)
  return translated.startsWith('⟦') ? value.replaceAll('_', ' ') : translated
}

function statusTone(status: DailyReportStatus | null | undefined): 'neutral' | 'ok' | 'warning' | 'danger' | 'info' {
  if (status === 'approved') return 'ok'
  if (status === 'submitted') return 'info'
  if (status === 'rejected') return 'danger'
  if (status === 'withdrawn') return 'warning'
  return 'neutral'
}

function FieldLabel({ children, required }: { children: ReactNode; required?: boolean }) {
  return (
    <span className="block text-xs font-medium text-graphite-700 dark:text-graphite-200">
      {children}
      {required && <span aria-hidden="true" className="ms-1 text-danger">*</span>}
    </span>
  )
}

function TextField({
  id,
  label,
  value,
  onChange,
  required,
  help,
  placeholder,
  dir,
  inputMode,
  autoComplete,
  testId,
}: {
  id: string
  label: ReactNode
  value: string
  onChange: (value: string) => void
  required?: boolean
  help?: string
  placeholder?: string
  dir?: 'ltr' | 'rtl' | 'auto'
  inputMode?: 'text' | 'decimal' | 'numeric' | 'url'
  autoComplete?: string
  testId?: string
}) {
  const helpId = help ? `${id}-hint` : undefined
  return (
    <label htmlFor={id} className="block min-w-0 space-y-1.5">
      <FieldLabel required={required}>{label}</FieldLabel>
      <input
        id={id}
        data-testid={testId}
        type="text"
        dir={dir}
        inputMode={inputMode}
        autoComplete={autoComplete}
        required={required}
        aria-required={required || undefined}
        aria-describedby={helpId}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-graphite-300 bg-white px-2.5 py-2 text-sm text-graphite-900 shadow-sm outline-none placeholder:text-graphite-400 focus:border-signal focus:ring-2 focus:ring-signal/30 dark:border-graphite-700 dark:bg-graphite-950 dark:text-graphite-100"
      />
      {help && <span id={helpId} className="block text-[11px] leading-4 text-graphite-500">{help}</span>}
    </label>
  )
}

function TextAreaField({
  id,
  label,
  value,
  onChange,
  help,
  rows = 3,
  required,
  testId,
}: {
  id: string
  label: ReactNode
  value: string
  onChange: (value: string) => void
  help?: string
  rows?: number
  required?: boolean
  testId?: string
}) {
  const helpId = help ? `${id}-hint` : undefined
  return (
    <label htmlFor={id} className="block min-w-0 space-y-1.5">
      <FieldLabel required={required}>{label}</FieldLabel>
      <textarea
        id={id}
        data-testid={testId}
        required={required}
        aria-required={required || undefined}
        aria-describedby={helpId}
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full resize-y rounded-md border border-graphite-300 bg-white px-2.5 py-2 text-sm text-graphite-900 shadow-sm outline-none placeholder:text-graphite-400 focus:border-signal focus:ring-2 focus:ring-signal/30 dark:border-graphite-700 dark:bg-graphite-950 dark:text-graphite-100"
      />
      {help && <span id={helpId} className="block text-[11px] leading-4 text-graphite-500">{help}</span>}
    </label>
  )
}

function SelectField({
  id,
  label,
  value,
  options,
  onChange,
  placeholder,
  required,
  disabled,
  help,
  testId,
}: {
  id: string
  label: ReactNode
  value: string
  options: Array<{ value: string; label: string }>
  onChange: (value: string) => void
  placeholder: string
  required?: boolean
  disabled?: boolean
  help?: string
  testId?: string
}) {
  const helpId = help ? `${id}-hint` : undefined
  return (
    <label htmlFor={id} className="block min-w-0 space-y-1.5">
      <FieldLabel required={required}>{label}</FieldLabel>
      <select
        id={id}
        data-testid={testId}
        required={required}
        aria-required={required || undefined}
        aria-describedby={helpId}
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-graphite-300 bg-white px-2.5 py-2 text-sm text-graphite-900 shadow-sm outline-none focus:border-signal focus:ring-2 focus:ring-signal/30 disabled:cursor-not-allowed disabled:opacity-60 dark:border-graphite-700 dark:bg-graphite-950 dark:text-graphite-100"
      >
        <option value="">{placeholder}</option>
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      {help && <span id={helpId} className="block text-[11px] leading-4 text-graphite-500">{help}</span>}
    </label>
  )
}

function formErrorText(error: string | null, t: (key: string) => string): string | null {
  if (!error) return null
  const messages: Record<string, string> = {
    wellbore: 'dailyReports.validationWellbore',
    date: 'dailyReports.validationDate',
    timezone: 'dailyReports.validationTimezone',
    shift: 'dailyReports.validationShift',
    customShift: 'dailyReports.validationCustomShift',
    timestamp: 'dailyReports.validationTimestamp',
    period: 'dailyReports.validationPeriod',
    reason: 'dailyReports.validationReason',
    activityTitle: 'dailyReports.validationActivityTitle',
    activityKind: 'dailyReports.validationActivityKind',
    operationKind: 'dailyReports.validationOperationKind',
    nptCategory: 'dailyReports.validationNptCategory',
    duration: 'dailyReports.validationDuration',
    depth: 'dailyReports.validationDepth',
    value: 'dailyReports.validationValue',
    basis: 'dailyReports.validationBasis',
  }
  return t(messages[error] ?? 'dailyReports.validationValue')
}

function DailyReportEditor({
  mode,
  form,
  setForm,
  catalogue,
  wellbores,
  wellboresLoading,
  wellboresError,
  onRetryWellbores,
  createGateReason,
  submitDisabled,
  isSaving,
  onCancel,
  onSubmit,
  error,
  onRetry,
}: {
  mode: FormMode
  form: ReportFormState
  setForm: Dispatch<SetStateAction<ReportFormState>>
  catalogue: DailyReportCatalogue
  wellbores: Wellbore[]
  wellboresLoading: boolean
  wellboresError: unknown
  onRetryWellbores: () => void
  createGateReason: string
  submitDisabled: boolean
  isSaving: boolean
  onCancel: () => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  error: unknown
  onRetry: () => void
}) {
  const { t } = useI18n()
  const [validationError, setValidationError] = useState<string | null>(null)
  const isCreate = mode === 'create'
  const timeOptions = catalogue.time_bases.map((value) => ({ value, label: labelEnum(t, 'timeBases', value) }))
  const depthOptions = catalogue.depth_references.map((value) => ({ value, label: labelEnum(t, 'depthReferences', value) }))
  const causeOptions = catalogue.cause_bases.map((value) => ({ value, label: labelEnum(t, 'causeBases', value) }))
  const activityOptions = catalogue.activity_kinds.map((value) => ({ value, label: labelEnum(t, 'activities', value) }))
  const shiftOptions = catalogue.shift_codes.map((value) => ({ value, label: labelEnum(t, 'shifts', value) }))
  const wellboreOptions = wellbores.map((wellbore) => ({
    value: wellbore.id,
    label: `${wellbore.name} · ${wellbore.sequence}${wellbore.is_active ? ' · ●' : ''}`,
  }))
  const activityUnitOptions = [...new Set(catalogue.parameters.flatMap((parameter) => parameter.accepted_units))]
  const durationUnitOptions = activityUnitOptions.filter((unit) => ['s', 'min', 'h', 'day'].includes(unit))
  const lengthUnitOptions = catalogue.parameters.find((parameter) => parameter.key === 'depth_md')?.accepted_units ?? []

  const patchForm = (patch: Partial<ReportFormState>) => setForm((current) => ({ ...current, ...patch }))
  const patchActivity = (index: number, patch: Partial<ActivityDraft>) => {
    setForm((current) => ({
      ...current,
      activities: current.activities.map((activity, row) => row === index ? { ...activity, ...patch } : activity),
    }))
  }
  const patchMeasurement = (index: number, patch: Partial<MeasurementDraft>) => {
    setForm((current) => ({
      ...current,
      measurements: current.measurements.map((measurement, row) => row === index ? { ...measurement, ...patch } : measurement),
    }))
  }
  const submitForm = (event: FormEvent<HTMLFormElement>) => {
    const messageKey = validateForm(form, catalogue, mode)
    if (messageKey) {
      event.preventDefault()
      setValidationError(formErrorText(messageKey, t))
      return
    }
    setValidationError(null)
    onSubmit(event)
  }

  return (
    <Card
      title={isCreate ? t('dailyReports.newReport') : t('dailyReports.editDraft')}
      subtitle={t('dailyReports.explicitProvenance')}
      className="mb-4"
      data-testid="daily-report-editor"
    >
      <form aria-label={isCreate ? t('dailyReports.createReport') : t('dailyReports.editDraft')} onSubmit={submitForm} noValidate>
        {isCreate ? (
          <fieldset className="mb-6 space-y-4">
            <legend className="mb-3 text-sm font-semibold">{t('dailyReports.identity')}</legend>
            {wellboresLoading && <Loading label={t('dailyReports.loadingWellbores')} />}
            {Boolean(wellboresError) && <ErrorState error={wellboresError} onRetry={onRetryWellbores} />}
            {!wellboresLoading && (wellboresError === null || wellboresError === undefined) && wellbores.length === 0 && (
              <p className="rounded-md border border-warning/40 bg-amber-50 p-3 text-xs text-warning dark:bg-amber-950/30">
                {t('dailyReports.unavailableWellbores')}
              </p>
            )}
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <SelectField
                id="daily-report-wellbore"
                label={t('dailyReports.wellbore')}
                value={form.wellbore_id}
                options={wellboreOptions}
                onChange={(wellbore_id) => patchForm({ wellbore_id })}
                placeholder={t('dailyReports.selectWellbore')}
                required
                disabled={wellboresLoading || Boolean(wellboresError) || wellbores.length === 0}
                testId="daily-report-wellbore"
              />
              <TextField
                id="daily-report-date"
                label={t('dailyReports.reportDate')}
                value={form.report_date}
                onChange={(report_date) => patchForm({ report_date })}
                placeholder="YYYY-MM-DD"
                required
                dir="ltr"
                inputMode="numeric"
                testId="daily-report-date"
              />
              <TextField
                id="daily-report-timezone"
                label={t('dailyReports.reportTimezone')}
                value={form.report_timezone}
                onChange={(report_timezone) => patchForm({ report_timezone })}
                placeholder="Europe/Amsterdam"
                required
                dir="ltr"
                help={t('dailyReports.timezoneHint')}
                testId="daily-report-timezone"
              />
              <SelectField
                id="daily-report-shift"
                label={t('dailyReports.shift')}
                value={form.shift_code}
                options={shiftOptions}
                onChange={(shift_code) => patchForm({ shift_code })}
                placeholder={t('dailyReports.chooseOption')}
                required
                testId="daily-report-shift"
              />
              {form.shift_code === 'custom' && (
                <TextField
                  id="daily-report-shift-label"
                  label={t('dailyReports.shiftLabel')}
                  value={form.shift_label}
                  onChange={(shift_label) => patchForm({ shift_label })}
                  required
                  help={t('dailyReports.shiftLabelHint')}
                />
              )}
              <TextField
                id="daily-report-number"
                label={t('dailyReports.reportNumber')}
                value={form.report_number}
                onChange={(report_number) => patchForm({ report_number })}
                help={t('dailyReports.reportNumberHint')}
                dir="auto"
              />
            </div>
          </fieldset>
        ) : (
          <div className="mb-4 rounded-md bg-graphite-50 p-3 text-xs text-graphite-600 dark:bg-graphite-950 dark:text-graphite-300">
            <bdi dir="ltr">{form.report_date}</bdi> · {labelEnum(t, 'shifts', form.shift_code)} · <bdi dir="ltr">{form.report_timezone}</bdi>
          </div>
        )}

        <fieldset className="mb-6 space-y-4">
          <legend className="mb-3 text-sm font-semibold">{t('dailyReports.reportingPeriod')}</legend>
          <p className="text-xs text-graphite-500">{t('dailyReports.timestampHint')}</p>
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField
              id="daily-report-period-start"
              label={t('dailyReports.periodStart')}
              value={form.period_start_at}
              onChange={(period_start_at) => patchForm({ period_start_at })}
              placeholder="2026-03-15T08:00:00+01:00"
              dir="ltr"
              testId="daily-report-period-start"
            />
            <TextField
              id="daily-report-period-end"
              label={t('dailyReports.periodEnd')}
              value={form.period_end_at}
              onChange={(period_end_at) => patchForm({ period_end_at })}
              placeholder="2026-03-15T20:00:00+01:00"
              dir="ltr"
              testId="daily-report-period-end"
            />
          </div>
        </fieldset>

        <fieldset className="mb-6 space-y-4">
          <legend className="mb-3 text-sm font-semibold">{t('dailyReports.content')}</legend>
          <div className="grid gap-4 lg:grid-cols-2">
            <TextAreaField id="daily-report-summary" label={t('dailyReports.summary')} value={form.summary} onChange={(summary) => patchForm({ summary })} rows={4} />
            <TextAreaField id="daily-report-operations-summary" label={t('dailyReports.operationsSummary')} value={form.operations_summary} onChange={(operations_summary) => patchForm({ operations_summary })} rows={4} />
            <TextAreaField id="daily-report-safety-notes" label={t('dailyReports.safetyNotes')} value={form.safety_notes} onChange={(safety_notes) => patchForm({ safety_notes })} rows={3} />
            <TextAreaField id="daily-report-next-plan" label={t('dailyReports.nextPeriodPlan')} value={form.next_period_plan} onChange={(next_period_plan) => patchForm({ next_period_plan })} rows={3} />
            <TextAreaField id="daily-report-environmental-notes" label={t('dailyReports.environmentalNotes')} value={form.environmental_notes} onChange={(environmental_notes) => patchForm({ environmental_notes })} rows={3} />
          </div>
        </fieldset>

        <fieldset className="mb-6 space-y-4">
          <legend className="mb-3 text-sm font-semibold">{t('dailyReports.activities')}</legend>
          <div className="flex justify-end">
            <Button size="sm" onClick={() => patchForm({ activities: [...form.activities, emptyActivity()] })} data-testid="daily-report-add-activity">
              {t('dailyReports.addActivity')}
            </Button>
          </div>
          {form.activities.length === 0 ? (
            <p className="text-xs text-graphite-500">{t('dailyReports.emptyActivities')}</p>
          ) : form.activities.map((activity, index) => (
            <fieldset key={activity.key} className="rounded-md border border-graphite-200 p-3 dark:border-graphite-700" data-testid="daily-report-activity-row">
              <legend className="px-1 text-xs font-semibold">{t('dailyReports.activity', { number: index + 1 })}</legend>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <SelectField id={`activity-${index}-kind`} label={t('dailyReports.activityKind')} value={activity.activity_kind} options={activityOptions} onChange={(activity_kind) => patchActivity(index, { activity_kind })} placeholder={t('dailyReports.chooseOption')} required />
                {activity.activity_kind === 'operation' && (
                  <SelectField id={`activity-${index}-operation`} label={t('dailyReports.operationKind')} value={activity.operation_kind} options={catalogue.operation_kinds.map((value) => ({ value, label: labelEnum(t, 'operationKinds', value) }))} onChange={(operation_kind) => patchActivity(index, { operation_kind })} placeholder={t('dailyReports.chooseOption')} required />
                )}
                {activity.activity_kind === 'npt' && (
                  <SelectField id={`activity-${index}-npt`} label={t('dailyReports.nptCategory')} value={activity.npt_category} options={catalogue.npt_categories.map((value) => ({ value, label: labelEnum(t, 'nptCategories', value) }))} onChange={(npt_category) => patchActivity(index, { npt_category })} placeholder={t('dailyReports.chooseOption')} required />
                )}
                <TextField id={`activity-${index}-title`} label={t('dailyReports.activityTitle')} value={activity.title} onChange={(title) => patchActivity(index, { title })} required />
                <div className="sm:col-span-2 lg:col-span-3">
                  <TextAreaField id={`activity-${index}-description`} label={t('dailyReports.activityDescription')} value={activity.description} onChange={(description) => patchActivity(index, { description })} rows={2} />
                </div>
                <TextField id={`activity-${index}-start`} label={t('dailyReports.startAt')} value={activity.start_at} onChange={(start_at) => patchActivity(index, { start_at })} placeholder="2026-03-15T08:00:00+01:00" dir="ltr" />
                <SelectField id={`activity-${index}-start-basis`} label={t('dailyReports.startTimeBasis')} value={activity.start_time_basis} options={timeOptions} onChange={(start_time_basis) => patchActivity(index, { start_time_basis })} placeholder={t('dailyReports.chooseOption')} />
                <TextField id={`activity-${index}-end`} label={t('dailyReports.endAt')} value={activity.end_at} onChange={(end_at) => patchActivity(index, { end_at })} placeholder="2026-03-15T20:00:00+01:00" dir="ltr" />
                <SelectField id={`activity-${index}-end-basis`} label={t('dailyReports.endTimeBasis')} value={activity.end_time_basis} options={timeOptions} onChange={(end_time_basis) => patchActivity(index, { end_time_basis })} placeholder={t('dailyReports.chooseOption')} />
                <TextField id={`activity-${index}-duration`} label={t('dailyReports.durationValue')} value={activity.duration_value} onChange={(duration_value) => patchActivity(index, { duration_value })} inputMode="decimal" dir="ltr" />
                <SelectField id={`activity-${index}-duration-unit`} label={t('dailyReports.durationUnit')} value={activity.duration_unit} options={durationUnitOptions.map((value) => ({ value, label: value }))} onChange={(duration_unit) => patchActivity(index, { duration_unit })} placeholder={t('dailyReports.selectUnit')} />
                <SelectField id={`activity-${index}-duration-basis`} label={t('dailyReports.durationBasis')} value={activity.duration_basis} options={timeOptions} onChange={(duration_basis) => patchActivity(index, { duration_basis })} placeholder={t('dailyReports.chooseOption')} />
                <TextField id={`activity-${index}-depth-from`} label={t('dailyReports.depthFrom')} value={activity.depth_from_value} onChange={(depth_from_value) => patchActivity(index, { depth_from_value })} inputMode="decimal" dir="ltr" />
                <SelectField id={`activity-${index}-depth-from-unit`} label={t('dailyReports.unit')} value={activity.depth_from_unit} options={lengthUnitOptions.map((value) => ({ value, label: value }))} onChange={(depth_from_unit) => patchActivity(index, { depth_from_unit })} placeholder={t('dailyReports.selectUnit')} />
                <TextField id={`activity-${index}-depth-to`} label={t('dailyReports.depthTo')} value={activity.depth_to_value} onChange={(depth_to_value) => patchActivity(index, { depth_to_value })} inputMode="decimal" dir="ltr" />
                <SelectField id={`activity-${index}-depth-to-unit`} label={t('dailyReports.unit')} value={activity.depth_to_unit} options={lengthUnitOptions.map((value) => ({ value, label: value }))} onChange={(depth_to_unit) => patchActivity(index, { depth_to_unit })} placeholder={t('dailyReports.selectUnit')} />
                <SelectField id={`activity-${index}-depth-reference`} label={t('dailyReports.depthReference')} value={activity.depth_reference} options={depthOptions} onChange={(depth_reference) => patchActivity(index, { depth_reference })} placeholder={t('dailyReports.chooseOption')} />
                <SelectField id={`activity-${index}-depth-basis`} label={t('dailyReports.depthBasis')} value={activity.depth_basis} options={timeOptions} onChange={(depth_basis) => patchActivity(index, { depth_basis })} placeholder={t('dailyReports.chooseOption')} />
                <SelectField id={`activity-${index}-cause-basis`} label={t('dailyReports.causeBasis')} value={activity.cause_basis} options={causeOptions} onChange={(cause_basis) => patchActivity(index, { cause_basis })} placeholder={t('dailyReports.chooseOption')} />
              </div>
              <div className="mt-3 flex justify-end">
                <Button size="sm" variant="ghost" onClick={() => patchForm({ activities: form.activities.filter((_, row) => row !== index) })} aria-label={`${t('dailyReports.removeActivity')} ${index + 1}`}>
                  {t('dailyReports.removeActivity')}
                </Button>
              </div>
            </fieldset>
          ))}
        </fieldset>

        <fieldset className="mb-6 space-y-4">
          <legend className="mb-3 text-sm font-semibold">{t('dailyReports.measurements')}</legend>
          <div className="flex justify-end">
            <Button size="sm" onClick={() => patchForm({ measurements: [...form.measurements, emptyMeasurement()] })} data-testid="daily-report-add-measurement">
              {t('dailyReports.addMeasurement')}
            </Button>
          </div>
          {form.measurements.length === 0 ? (
            <p className="text-xs text-graphite-500">{t('dailyReports.emptyMeasurements')}</p>
          ) : form.measurements.map((measurement, index) => {
            const parameter = catalogue.parameters.find((item) => item.key === measurement.parameter_key)
            const acceptedUnits = parameter?.accepted_units ?? []
            const requiredReference = parameter?.required_depth_reference ?? null
            return (
              <fieldset key={measurement.key} className="rounded-md border border-graphite-200 p-3 dark:border-graphite-700" data-testid="daily-report-measurement-row">
                <legend className="px-1 text-xs font-semibold">{t('dailyReports.measurement', { number: index + 1 })}</legend>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  <SelectField
                    id={`measurement-${index}-parameter`}
                    label={t('dailyReports.parameter')}
                    value={measurement.parameter_key}
                    options={catalogue.parameters.map((item) => ({ value: item.key, label: t(`dailyReports.parameterLabels.${item.key}`) }))}
                    onChange={(parameter_key) => {
                      const selected = catalogue.parameters.find((item) => item.key === parameter_key)
                      patchMeasurement(index, {
                        parameter_key,
                        unit: selected?.canonical_unit ?? '',
                        depth_reference: selected?.required_depth_reference ?? 'unknown',
                      })
                    }}
                    placeholder={t('dailyReports.selectParameter')}
                    required
                    testId="daily-report-measurement-parameter"
                  />
                  <TextField id={`measurement-${index}-value`} label={t('dailyReports.value')} value={measurement.value} onChange={(value) => patchMeasurement(index, { value })} inputMode="decimal" dir="ltr" required testId="daily-report-measurement-value" />
                  <SelectField id={`measurement-${index}-unit`} label={t('dailyReports.unit')} value={measurement.unit} options={acceptedUnits.map((value) => ({ value, label: value }))} onChange={(unit) => patchMeasurement(index, { unit })} placeholder={t('dailyReports.selectUnit')} required testId="daily-report-measurement-unit" />
                  <SelectField id={`measurement-${index}-basis`} label={t('dailyReports.valueBasis')} value={measurement.value_basis} options={timeOptions} onChange={(value_basis) => patchMeasurement(index, { value_basis })} placeholder={t('dailyReports.chooseOption')} required />
                  <TextField id={`measurement-${index}-observed`} label={t('dailyReports.observedAt')} value={measurement.observed_at} onChange={(observed_at) => patchMeasurement(index, { observed_at })} placeholder="2026-03-15T08:00:00+01:00" dir="ltr" />
                  <SelectField id={`measurement-${index}-time-basis`} label={t('dailyReports.timeBasis')} value={measurement.time_basis} options={timeOptions} onChange={(time_basis) => patchMeasurement(index, { time_basis })} placeholder={t('dailyReports.chooseOption')} />
                  <TextField id={`measurement-${index}-depth`} label={t('dailyReports.depthValue')} value={measurement.depth_value} onChange={(depth_value) => patchMeasurement(index, { depth_value })} inputMode="decimal" dir="ltr" />
                  <SelectField id={`measurement-${index}-depth-unit`} label={t('dailyReports.unit')} value={measurement.depth_unit} options={lengthUnitOptions.map((value) => ({ value, label: value }))} onChange={(depth_unit) => patchMeasurement(index, { depth_unit })} placeholder={t('dailyReports.selectUnit')} />
                  <SelectField
                    id={`measurement-${index}-depth-reference`}
                    label={t('dailyReports.depthReference')}
                    value={measurement.depth_reference}
                    options={depthOptions}
                    onChange={(depth_reference) => patchMeasurement(index, { depth_reference })}
                    placeholder={t('dailyReports.chooseOption')}
                    disabled={Boolean(requiredReference)}
                    help={requiredReference ? labelEnum(t, 'depthReferences', requiredReference) : undefined}
                  />
                  <SelectField id={`measurement-${index}-depth-basis`} label={t('dailyReports.depthBasis')} value={measurement.depth_basis} options={timeOptions} onChange={(depth_basis) => patchMeasurement(index, { depth_basis })} placeholder={t('dailyReports.chooseOption')} />
                </div>
                <div className="mt-3 flex justify-end">
                  <Button size="sm" variant="ghost" onClick={() => patchForm({ measurements: form.measurements.filter((_, row) => row !== index) })} aria-label={`${t('dailyReports.removeMeasurement')} ${index + 1}`}>
                    {t('dailyReports.removeMeasurement')}
                  </Button>
                </div>
              </fieldset>
            )
          })}
        </fieldset>

        {!isCreate && (
          <div className="mb-5">
            <TextAreaField id="daily-report-edit-reason" label={t('dailyReports.reason')} value={form.reason} onChange={(reason) => patchForm({ reason })} rows={2} required help={t('dailyReports.reasonHint')} testId="daily-report-edit-reason" />
          </div>
        )}
        {validationError && <p className="mb-3 rounded-md bg-red-50 p-3 text-sm text-danger dark:bg-red-950/30" role="alert">{validationError}</p>}
        {createGateReason && <p className="mb-3 text-xs text-graphite-500">{createGateReason}</p>}
        {Boolean(error) && <div className="mb-3"><ErrorState error={error} onRetry={onRetry} /></div>}
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-graphite-100 pt-4 dark:border-graphite-800">
          <Button onClick={onCancel}>{t('dailyReports.cancel')}</Button>
          <Button type="submit" variant="primary" disabled={submitDisabled || isSaving || (isCreate && (wellboresLoading || Boolean(wellboresError) || wellbores.length === 0))} data-testid={isCreate ? 'daily-report-create-submit' : 'daily-report-save-submit'}>
            {isSaving ? t(isCreate ? 'dailyReports.creating' : 'dailyReports.saving') : t(isCreate ? 'dailyReports.createReport' : 'dailyReports.saveDraft')}
          </Button>
        </div>
      </form>
    </Card>
  )
}

function SourceProvenance({ revision }: { revision: DailyReportRevision }) {
  const { t } = useI18n()
  const hasSource = revision.source_document_id || revision.source_record_id || revision.source_region_id || revision.source_page_number
  return (
    <Card title={t('dailyReports.sourceEvidence')}>
      {hasSource ? (
        <dl className="grid gap-3 text-xs sm:grid-cols-2">
          {revision.source_document_id && <ProvenanceItem label={t('dailyReports.sourceDocument')} value={revision.source_document_id} />}
          {revision.source_record_id && <ProvenanceItem label={t('dailyReports.sourceRecord')} value={revision.source_record_id} />}
          {revision.source_region_id && <ProvenanceItem label={t('dailyReports.sourceRegion')} value={revision.source_region_id} />}
          {revision.source_page_number !== null && <ProvenanceItem label={t('dailyReports.sourcePage')} value={String(revision.source_page_number)} />}
        </dl>
      ) : <p className="text-xs text-graphite-500">{t('dailyReports.noSource')}</p>}
    </Card>
  )
}

function ProvenanceItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-graphite-500">{label}</dt>
      <dd className="mt-1 break-all font-mono text-graphite-800 dark:text-graphite-200"><bdi dir="ltr">{value}</bdi></dd>
    </div>
  )
}

function RevisionContent({ revision }: { revision: DailyReportRevision }) {
  const { t, locale } = useI18n()
  const verified = revision.content_hash_verified
  return (
    <div className="space-y-4">
      <Card title={t('dailyReports.contentIntegrity')}>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={verified ? 'ok' : 'danger'}>{verified ? t('dailyReports.fingerprintVerified') : t('dailyReports.fingerprintMismatch')}</Badge>
          {revision.content_hash && <code className="max-w-full break-all text-[11px] text-graphite-500">{revision.content_hash}</code>}
        </div>
        {!verified && <p className="mt-3 text-sm font-medium text-danger">{t('dailyReports.fingerprintMismatch')}</p>}
      </Card>

      <Card title={t('dailyReports.content')}>
        <dl className="grid gap-4 sm:grid-cols-2">
          <ContentItem label={t('dailyReports.summary')} value={revision.summary} />
          <ContentItem label={t('dailyReports.operationsSummary')} value={revision.operations_summary} />
          <ContentItem label={t('dailyReports.safetyNotes')} value={revision.safety_notes} />
          <ContentItem label={t('dailyReports.nextPeriodPlan')} value={revision.next_period_plan} />
          <ContentItem label={t('dailyReports.environmentalNotes')} value={revision.environmental_notes} />
          <ContentItem label={t('dailyReports.periodStart')} value={revision.period_start_at} ltr />
          <ContentItem label={t('dailyReports.periodEnd')} value={revision.period_end_at} ltr />
          <ContentItem label={t('dailyReports.reason')} value={revision.decision_reason} />
        </dl>
        {revision.activities.length === 0 ? (
          <p className="mt-4 text-xs text-graphite-500">{t('dailyReports.emptyActivities')}</p>
        ) : (
          <div className="mt-5 space-y-3">
            <h3 className="text-xs font-semibold">{t('dailyReports.activities')}</h3>
            {revision.activities.map((activity) => (
              <article key={activity.id} className="rounded-md border border-graphite-200 p-3 dark:border-graphite-700" data-testid="daily-report-activity-detail">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge>{labelEnum(t, 'activities', activity.activity_kind)}</Badge>
                  {activity.operation_kind && <Badge tone="info">{labelEnum(t, 'operationKinds', activity.operation_kind)}</Badge>}
                  {activity.npt_category && <Badge tone="warning">{labelEnum(t, 'nptCategories', activity.npt_category)}</Badge>}
                  <Badge>{labelEnum(t, 'resolutions', activity.resolution_state)}</Badge>
                </div>
                <h4 className="mt-2 text-sm font-semibold">{activity.title}</h4>
                {activity.description && <p className="mt-1 whitespace-pre-wrap text-xs text-graphite-600 dark:text-graphite-300">{activity.description}</p>}
                <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
                  <ProvenanceItem label={t('dailyReports.startAt')} value={activity.start_at ?? t('dailyReports.noValue')} />
                  <ProvenanceItem label={t('dailyReports.endAt')} value={activity.end_at ?? t('dailyReports.noValue')} />
                  <ProvenanceItem label={t('dailyReports.startTimeBasis')} value={labelEnum(t, 'timeBases', activity.start_time_basis)} />
                  <ProvenanceItem label={t('dailyReports.endTimeBasis')} value={labelEnum(t, 'timeBases', activity.end_time_basis)} />
                  <ProvenanceItem label={t('dailyReports.sourceDuration')} value={activity.duration_source_value === null ? t('dailyReports.noValue') : `${formatNumber(activity.duration_source_value, locale)} ${activity.duration_source_unit ?? ''}`} />
                  <ProvenanceItem label={t('dailyReports.canonicalDuration')} value={activity.duration_seconds === null ? t('dailyReports.noValue') : `${formatNumber(activity.duration_seconds, locale)} s`} />
                  <ProvenanceItem label={t('dailyReports.durationBasis')} value={labelEnum(t, 'timeBases', activity.duration_basis)} />
                  <ProvenanceItem label={t('dailyReports.sourceDepthFrom')} value={activity.depth_from_source_value === null ? t('dailyReports.noValue') : `${formatNumber(activity.depth_from_source_value, locale)} ${activity.depth_from_source_unit ?? ''}`} />
                  <ProvenanceItem label={t('dailyReports.canonicalDepthFrom')} value={activity.depth_from_si === null ? t('dailyReports.noValue') : `${formatNumber(activity.depth_from_si, locale)} m`} />
                  <ProvenanceItem label={t('dailyReports.sourceDepthTo')} value={activity.depth_to_source_value === null ? t('dailyReports.noValue') : `${formatNumber(activity.depth_to_source_value, locale)} ${activity.depth_to_source_unit ?? ''}`} />
                  <ProvenanceItem label={t('dailyReports.canonicalDepthTo')} value={activity.depth_to_si === null ? t('dailyReports.noValue') : `${formatNumber(activity.depth_to_si, locale)} m`} />
                  <ProvenanceItem label={t('dailyReports.depthReference')} value={labelEnum(t, 'depthReferences', activity.depth_reference)} />
                  <ProvenanceItem label={t('dailyReports.depthBasis')} value={labelEnum(t, 'timeBases', activity.depth_basis)} />
                  <ProvenanceItem label={t('dailyReports.causeBasis')} value={labelEnum(t, 'causeBases', activity.cause_basis)} />
                  {activity.source_document_id && <ProvenanceItem label={t('dailyReports.sourceDocument')} value={activity.source_document_id} />}
                  {activity.source_record_id && <ProvenanceItem label={t('dailyReports.sourceRecord')} value={activity.source_record_id} />}
                  {activity.source_region_id && <ProvenanceItem label={t('dailyReports.sourceRegion')} value={activity.source_region_id} />}
                  {activity.source_page_number !== null && <ProvenanceItem label={t('dailyReports.sourcePage')} value={String(activity.source_page_number)} />}
                  {activity.operation_id && <ProvenanceItem label={t('dailyReports.linkedOperation')} value={activity.operation_id} />}
                  {activity.event_id && <ProvenanceItem label={t('dailyReports.linkedEvent')} value={activity.event_id} />}
                </dl>
              </article>
            ))}
          </div>
        )}
        {revision.measurements.length === 0 ? (
          <p className="mt-4 text-xs text-graphite-500">{t('dailyReports.emptyMeasurements')}</p>
        ) : (
          <div className="mt-5 space-y-3">
            <h3 className="text-xs font-semibold">{t('dailyReports.measurements')}</h3>
            {revision.measurements.map((measurement) => (
              <article key={measurement.id} className="rounded-md border border-graphite-200 p-3 dark:border-graphite-700" data-testid="daily-report-measurement-detail">
                <div className="flex flex-wrap items-center gap-2">
                  <h4 className="text-sm font-semibold">{t(`dailyReports.parameterLabels.${measurement.parameter_key}`)}</h4>
                  <Badge>{labelEnum(t, 'resolutions', measurement.resolution_state)}</Badge>
                </div>
                <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
                  <ProvenanceItem label={t('dailyReports.enteredValue')} value={`${formatNumber(measurement.source_value, locale)} ${measurement.source_unit}`} />
                  <ProvenanceItem label={t('dailyReports.acceptedValue')} value={`${formatNumber(measurement.accepted_input_value, locale)} ${measurement.accepted_input_unit}`} />
                  <ProvenanceItem label={t('dailyReports.canonicalValue')} value={`${formatNumber(measurement.value_si, locale)} ${measurement.canonical_unit}`} />
                  <ProvenanceItem label={t('dailyReports.valueBasis')} value={labelEnum(t, 'timeBases', measurement.value_basis)} />
                  <ProvenanceItem label={t('dailyReports.observedAt')} value={measurement.observed_at ?? t('dailyReports.noValue')} />
                  <ProvenanceItem label={t('dailyReports.timeBasis')} value={labelEnum(t, 'timeBases', measurement.time_basis)} />
                  <ProvenanceItem label={t('dailyReports.depthValue')} value={measurement.depth_source_value === null ? t('dailyReports.noValue') : `${formatNumber(measurement.depth_source_value, locale)} ${measurement.depth_source_unit ?? ''}`} />
                  <ProvenanceItem label={t('dailyReports.depthReference')} value={labelEnum(t, 'depthReferences', measurement.depth_reference)} />
                  <ProvenanceItem label={t('dailyReports.depthBasis')} value={labelEnum(t, 'timeBases', measurement.depth_basis)} />
                  {measurement.source_document_id && <ProvenanceItem label={t('dailyReports.sourceDocument')} value={measurement.source_document_id} />}
                  {measurement.source_record_id && <ProvenanceItem label={t('dailyReports.sourceRecord')} value={measurement.source_record_id} />}
                  {measurement.source_region_id && <ProvenanceItem label={t('dailyReports.sourceRegion')} value={measurement.source_region_id} />}
                  {measurement.source_page_number !== null && <ProvenanceItem label={t('dailyReports.sourcePage')} value={String(measurement.source_page_number)} />}
                </dl>
              </article>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function ContentItem({ label, value, ltr = false }: { label: string; value: string | null; ltr?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-graphite-500">{label}</dt>
      <dd className={`mt-1 whitespace-pre-wrap break-words text-sm text-graphite-800 dark:text-graphite-200 ${ltr ? 'font-mono' : ''}`}>
        {value ? (ltr ? <bdi dir="ltr">{value}</bdi> : value) : <span className="text-graphite-400">—</span>}
      </dd>
    </div>
  )
}

function revisionStatusLabel(status: DailyReportStatus, t: (key: string) => string): string {
  const label = t(`dailyReports.enums.statuses.${status}`)
  return label.startsWith('⟦') ? status : label
}

function asDecisionBody(revision: DailyReportRevision, reason: string): DailyReportDecisionPayload {
  return { expected_edit_version: revision.edit_version, reason: reason.trim() }
}

export default function DailyReportsWorkspace() {
  const { wellId = '' } = useParams()
  const { t, locale } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const identity = useIdentity()
  const selectedReportId = searchParams.get('report')
  const rawRevision = searchParams.get('revision')
  const selectedRevisionNumber = rawRevision && /^\d+$/.test(rawRevision) ? Number(rawRevision) : null
  const [statusFilter, setStatusFilter] = useState('')
  const [shiftFilter, setShiftFilter] = useState('')
  const [reportDateFrom, setReportDateFrom] = useState('')
  const [reportDateTo, setReportDateTo] = useState('')
  const [listOffset, setListOffset] = useState(0)
  const [formMode, setFormMode] = useState<FormMode | null>(null)
  const [form, setForm] = useState<ReportFormState>(emptyForm)
  const [actionMode, setActionMode] = useState<ActionMode | null>(null)
  const [actionReason, setActionReason] = useState('')
  const [notice, setNotice] = useState('')
  const [localActionError, setLocalActionError] = useState<string | null>(null)

  const createGate = useActionGate('daily_report.create')
  const editGate = useActionGate('daily_report.edit')
  const reviseGate = useActionGate('daily_report.revise')
  const submitGate = useActionGate('daily_report.submit')
  const approveGate = useActionGate('daily_report.approve')
  const rejectGate = useActionGate('daily_report.reject')
  const withdrawGate = useActionGate('daily_report.withdraw')

  const catalogueQuery = useQuery({
    queryKey: ['daily-report-catalogue'],
    queryFn: ({ signal }) => drillingApi.dailyReportCatalogue(signal),
  })
  const wellboresQuery = useQuery({
    queryKey: ['wellbores', wellId],
    queryFn: ({ signal }) => drillingApi.listWellbores(wellId, signal),
    enabled: Boolean(wellId),
  })
  const dateRangeInvalid = Boolean(reportDateFrom && reportDateTo && reportDateFrom > reportDateTo)
  const reportListQuery = useQuery({
    queryKey: ['daily-reports', wellId, statusFilter, shiftFilter, reportDateFrom, reportDateTo, listOffset],
    queryFn: ({ signal }) => drillingApi.listDailyReports(
      wellId,
      {
        status: statusFilter || undefined,
        shift_code: shiftFilter || undefined,
        report_date_from: reportDateFrom || undefined,
        report_date_to: reportDateTo || undefined,
        limit: 50,
        offset: listOffset,
      },
      signal,
    ),
    enabled: Boolean(wellId && !dateRangeInvalid),
  })
  const detailQuery = useQuery({
    queryKey: ['daily-report-detail', wellId, selectedReportId],
    queryFn: ({ signal }) => drillingApi.getDailyReport(wellId, selectedReportId as string, signal),
    enabled: Boolean(wellId && selectedReportId),
  })
  const historyQuery = useQuery({
    queryKey: ['daily-report-history', wellId, selectedReportId],
    queryFn: ({ signal }) => drillingApi.getDailyReportHistory(wellId, selectedReportId as string, { limit: 50, offset: 0 }, signal),
    enabled: Boolean(wellId && selectedReportId),
  })
  const catalogue = catalogueQuery.data
  const wellbores = wellboresQuery.data?.items ?? []
  const reportDetail = detailQuery.data
  const report = reportDetail?.report
  const latestRevision = reportDetail?.latest_revision
  const selectedWellbore = report ? wellbores.find((candidate) => candidate.id === report.wellbore_id) : undefined
  const oldRevisionQuery = useQuery({
    queryKey: ['daily-report-revision', wellId, selectedReportId, selectedRevisionNumber],
    queryFn: ({ signal }) => drillingApi.getDailyReportRevision(wellId, selectedReportId as string, selectedRevisionNumber as number, signal),
    enabled: Boolean(wellId && selectedReportId && selectedRevisionNumber && report && selectedRevisionNumber !== report.latest_revision_number),
  })
  const activeRevision = selectedRevisionNumber && selectedRevisionNumber !== report?.latest_revision_number
    ? oldRevisionQuery.data?.revision
    : latestRevision
  const isViewingLatest = !selectedRevisionNumber || selectedRevisionNumber === report?.latest_revision_number
  const revisionFingerprintValid = activeRevision?.content_hash_verified === true

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['daily-reports', wellId] })
    if (selectedReportId) {
      void queryClient.invalidateQueries({ queryKey: ['daily-report-detail', wellId, selectedReportId] })
      void queryClient.invalidateQueries({ queryKey: ['daily-report-history', wellId, selectedReportId] })
    }
  }
  const selectReport = (reportId: string, revisionNumber?: number) => {
    const next = new URLSearchParams(searchParams)
    next.set('report', reportId)
    if (revisionNumber === undefined) next.delete('revision')
    else next.set('revision', String(revisionNumber))
    setSearchParams(next)
    setFormMode(null)
    setActionMode(null)
    setActionReason('')
    setNotice('')
  }
  const clearSelection = () => {
    const next = new URLSearchParams(searchParams)
    next.delete('report')
    next.delete('revision')
    setSearchParams(next)
    setFormMode(null)
    setActionMode(null)
  }

  const createMutation = useMutation({
    mutationFn: ({ payload, idempotencyKey }: CreateVariables) => drillingApi.createDailyReport(wellId, payload, idempotencyKey),
    onSuccess: async (result) => {
      setFormMode(null)
      await queryClient.invalidateQueries({ queryKey: ['daily-reports', wellId] })
      selectReport(result.report.id)
      setNotice('dailyReports.reportCreated')
    },
  })
  const editMutation = useMutation({
    mutationFn: ({ reportId, revisionNumber, payload, idempotencyKey }: EditVariables) =>
      drillingApi.updateDailyReportDraft(wellId, reportId, revisionNumber, payload, idempotencyKey),
    onSuccess: async (result) => {
      setNotice('dailyReports.draftSaved')
      setFormMode(null)
      await queryClient.invalidateQueries({ queryKey: ['daily-reports', wellId] })
      await queryClient.invalidateQueries({ queryKey: ['daily-report-detail', wellId, result.report.id] })
      await queryClient.invalidateQueries({ queryKey: ['daily-report-history', wellId, result.report.id] })
      const next = new URLSearchParams(searchParams)
      next.set('report', result.report.id)
      next.delete('revision')
      setSearchParams(next)
    },
  })
  const actionMutation = useMutation({
    mutationFn: ({ action, reportId, revisionNumber, body, idempotencyKey }: ReportActionVariables) => {
      if (action === 'submit') return drillingApi.submitDailyReport(wellId, reportId, revisionNumber, body, idempotencyKey)
      if (action === 'approve') return drillingApi.approveDailyReport(wellId, reportId, revisionNumber, body, idempotencyKey)
      if (action === 'reject') return drillingApi.rejectDailyReport(wellId, reportId, revisionNumber, body, idempotencyKey)
      return drillingApi.withdrawDailyReport(wellId, reportId, revisionNumber, body, idempotencyKey)
    },
    onSuccess: (_result, variables) => {
      if (variables.action === 'withdraw') setNotice('dailyReports.reportWithdrawn')
      if (variables.action === 'submit') setNotice('dailyReports.reportSubmitted')
      if (variables.action === 'approve') setNotice('dailyReports.reportApproved')
      if (variables.action === 'reject') setNotice('dailyReports.reportRejected')
      setActionMode(null)
      setActionReason('')
      setLocalActionError(null)
      refreshAll()
    },
  })
  const correctionMutation = useMutation({
    mutationFn: ({ reportId, reason, idempotencyKey }: CorrectionVariables) =>
      drillingApi.createDailyReportCorrection(wellId, reportId, reason, idempotencyKey),
    onSuccess: async (result) => {
      setNotice('dailyReports.correctionCreated')
      setActionMode(null)
      setActionReason('')
      await queryClient.invalidateQueries({ queryKey: ['daily-reports', wellId] })
      await queryClient.invalidateQueries({ queryKey: ['daily-report-detail', wellId, result.report.id] })
      await queryClient.invalidateQueries({ queryKey: ['daily-report-history', wellId, result.report.id] })
      const next = new URLSearchParams(searchParams)
      next.set('report', result.report.id)
      next.delete('revision')
      setSearchParams(next)
    },
  })

  const mutationError = formMode === 'create' ? createMutation.error : formMode === 'edit' ? editMutation.error : null
  const mutationRetry = () => {
    if (formMode === 'create' && createMutation.error) {
      if (createMutation.error instanceof ApiError && createMutation.error.kind === 'conflict') refreshAll()
      else if (createMutation.variables) createMutation.mutate(createMutation.variables)
      return
    }
    if (formMode === 'edit' && editMutation.error) {
      if (editMutation.error instanceof ApiError && editMutation.error.kind === 'conflict') {
        void detailQuery.refetch()
        void historyQuery.refetch()
      } else if (editMutation.variables) editMutation.mutate(editMutation.variables)
    }
  }
  const actionRetry = () => {
    if (actionMutation.error instanceof ApiError && actionMutation.error.kind === 'conflict') refreshAll()
    else if (actionMutation.variables) actionMutation.mutate(actionMutation.variables)
  }
  const correctionRetry = () => {
    if (correctionMutation.error instanceof ApiError && correctionMutation.error.kind === 'conflict') refreshAll()
    else if (correctionMutation.variables) correctionMutation.mutate(correctionMutation.variables)
  }

  const openCreate = () => {
    setForm(emptyForm())
    setListOffset(0)
    setFormMode('create')
    setActionMode(null)
    setNotice('')
    createMutation.reset()
    editMutation.reset()
  }
  const openEdit = () => {
    if (!report || !latestRevision || !isViewingLatest) return
    setForm(revisionForm(report, latestRevision))
    setFormMode('edit')
    setActionMode(null)
    setNotice('')
    createMutation.reset()
    editMutation.reset()
  }
  const submitEditor = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!catalogue) return
    const content = toContent(form)
    if (formMode === 'create') {
      createMutation.mutate({
        idempotencyKey: newIdempotencyKey(),
        payload: {
          wellbore_id: form.wellbore_id,
          report_date: form.report_date,
          report_timezone: form.report_timezone.trim(),
          shift_code: form.shift_code,
          shift_label: form.shift_code === 'custom' ? nonEmpty(form.shift_label) : null,
          report_number: nonEmpty(form.report_number),
          content,
        },
      })
      return
    }
    if (!report || !latestRevision) return
    editMutation.mutate({
      reportId: report.id,
      revisionNumber: latestRevision.revision_number,
      idempotencyKey: newIdempotencyKey(),
      payload: {
        expected_edit_version: latestRevision.edit_version,
        reason: form.reason.trim(),
        content,
      },
    })
  }
  const performAction = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!activeRevision || !report || !actionMode) return
    if (
      actionMode === 'submit' &&
      !activeRevision.activities.length &&
      !activeRevision.measurements.length &&
      ![
        activeRevision.summary,
        activeRevision.operations_summary,
        activeRevision.safety_notes,
        activeRevision.next_period_plan,
        activeRevision.environmental_notes,
      ].some((value) => value?.trim())
    ) {
      setLocalActionError('dailyReports.validationContent')
      return
    }
    if (!actionReason.trim()) {
      setLocalActionError('dailyReports.validationReason')
      return
    }
    setLocalActionError(null)
    if (actionMode === 'correct') {
      correctionMutation.mutate({ reportId: report.id, reason: actionReason.trim(), idempotencyKey: newIdempotencyKey() })
      return
    }
    actionMutation.mutate({
      action: actionMode,
      reportId: report.id,
      revisionNumber: activeRevision.revision_number,
      body: asDecisionBody(activeRevision, actionReason),
      idempotencyKey: newIdempotencyKey(),
    })
  }

  const filterOptions = useMemo(() => [
    { value: '', label: t('dailyReports.allStatuses') },
    ...(catalogue?.revision_statuses ?? []).map((status) => ({ value: status, label: revisionStatusLabel(status, t) })),
  ], [catalogue?.revision_statuses, t])
  const shiftFilterOptions = useMemo(() => [
    { value: '', label: t('dailyReports.allShifts') },
    ...(catalogue?.shift_codes ?? []).map((shift) => ({ value: shift, label: labelEnum(t, 'shifts', shift) })),
  ], [catalogue?.shift_codes, t])

  const listContent = () => {
    if (dateRangeInvalid) return <p role="alert" className="rounded-md bg-amber-50 p-3 text-xs text-warning dark:bg-amber-950/30">{t('dailyReports.invalidDateRange')}</p>
    if (reportListQuery.isPending) return <Loading />
    if (reportListQuery.error) return <ErrorState error={reportListQuery.error} onRetry={() => { void reportListQuery.refetch() }} />
    const reports = reportListQuery.data?.items ?? []
    if (!reports.length) return <EmptyState message={t('dailyReports.noReports')} hint={t('dailyReports.noReportsHint')} />
    return (
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-start text-xs" data-testid="daily-report-list">
          <caption className="sr-only">{t('dailyReports.reportList')}</caption>
          <thead className="text-graphite-500">
            <tr className="border-b border-graphite-200 dark:border-graphite-700">
              <th scope="col" className="px-2 py-2 font-medium">{t('dailyReports.reportDate')}</th>
              <th scope="col" className="px-2 py-2 font-medium">{t('dailyReports.wellbore')}</th>
              <th scope="col" className="px-2 py-2 font-medium">{t('dailyReports.shift')}</th>
              <th scope="col" className="px-2 py-2 font-medium">{t('dailyReports.reportNumber')}</th>
              <th scope="col" className="px-2 py-2 font-medium">{t('dailyReports.statusFilter')}</th>
            </tr>
          </thead>
          <tbody>
            {reports.map((row) => {
              const status = row.latest_revision?.status ?? 'draft'
              const selected = row.id === selectedReportId
              const bore = wellbores.find((candidate) => candidate.id === row.wellbore_id)
              return (
                <tr key={row.id} className={`border-b border-graphite-100 dark:border-graphite-800 ${selected ? 'bg-signal/5' : ''}`}>
                  <td className="px-2 py-2">
                    <button type="button" onClick={() => selectReport(row.id)} className="rounded text-start font-medium text-signal-deep underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-signal dark:text-signal-light" aria-current={selected ? 'true' : undefined} data-testid={`daily-report-select-${row.id}`}>
                      <bdi dir="ltr">{row.report_date}</bdi>
                    </button>
                  </td>
                  <td className="px-2 py-2">{bore ? `${bore.name} · ${bore.sequence}` : <bdi dir="ltr">{row.wellbore_id}</bdi>}</td>
                  <td className="px-2 py-2">{labelEnum(t, 'shifts', row.shift_code)}{row.shift_label ? ` · ${row.shift_label}` : ''}</td>
                  <td className="px-2 py-2"><bdi dir="auto">{row.report_number ?? '—'}</bdi></td>
                  <td className="px-2 py-2"><Badge tone={statusTone(status)}>{revisionStatusLabel(status, t)}</Badge> <span className="ms-1 text-graphite-500">v{row.latest_revision_number}</span></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    )
  }

  const revisionCreatedLabel = activeRevision?.created_at ? formatDateTime(activeRevision.created_at, locale) : '—'
  const actionGate = actionMode === 'submit' ? submitGate : actionMode === 'approve' ? approveGate : actionMode === 'reject' ? rejectGate : actionMode === 'withdraw' ? withdrawGate : reviseGate
  const actionMutationBusy = actionMutation.isPending || correctionMutation.isPending
  const actionError = actionMutation.error || correctionMutation.error
  const submitterIsCurrentUser = Boolean(activeRevision && identity.data?.principal_id === activeRevision.submitted_by)
  const currentUserOwnsWithdrawal = Boolean(
    activeRevision && identity.data?.principal_id === (activeRevision.status === 'submitted' ? activeRevision.submitted_by : activeRevision.created_by),
  )
  const canWithdrawState = activeRevision?.status === 'draft' || activeRevision?.status === 'submitted'
  const canCorrectState = Boolean(latestRevision && ['approved', 'rejected', 'withdrawn'].includes(latestRevision.status))
  const selectedRevisionLoading = Boolean(selectedRevisionNumber && selectedRevisionNumber !== report?.latest_revision_number && oldRevisionQuery.isPending)
  const selectedRevisionError = Boolean(selectedRevisionNumber && selectedRevisionNumber !== report?.latest_revision_number && oldRevisionQuery.error)

  return (
    <div className="space-y-4" data-testid="daily-reports-workspace">
      <PageHeader
        title={t('dailyReports.title')}
        description={t('dailyReports.description')}
        breadcrumb={<span>{t('nav.wells')} <span aria-hidden="true">/</span> <bdi dir="ltr">{wellId}</bdi></span>}
        actions={(
          <>
            <Button onClick={refreshAll} aria-label={t('dailyReports.refresh')}>
              {t('dailyReports.refresh')}
            </Button>
            <Button variant="primary" onClick={openCreate} disabled={createGate.suggestDisabled} title={createGate.reason || undefined} data-testid="daily-report-new">
              {t('dailyReports.newReport')}
            </Button>
          </>
        )}
      />

      {notice && <p role="status" aria-live="polite" className="rounded-md border border-ok/30 bg-emerald-50 p-3 text-sm text-ok dark:bg-emerald-950/30">{t(notice)}</p>}
      {createGate.suggestDisabled && <p className="text-xs text-graphite-500" data-testid="daily-report-create-gate">{createGate.reason}</p>}
      {catalogueQuery.isPending && <Card><Loading /></Card>}
      {catalogueQuery.error && <ErrorState error={catalogueQuery.error} onRetry={() => { void catalogueQuery.refetch() }} />}

      {formMode && catalogue && (
        <DailyReportEditor
          mode={formMode}
          form={form}
          setForm={setForm}
          catalogue={catalogue}
          wellbores={wellbores}
          wellboresLoading={wellboresQuery.isPending}
          wellboresError={wellboresQuery.error}
          onRetryWellbores={() => { void wellboresQuery.refetch() }}
          createGateReason={formMode === 'create' ? createGate.reason : editGate.reason}
          submitDisabled={formMode === 'create' ? createGate.suggestDisabled : editGate.suggestDisabled}
          isSaving={createMutation.isPending || editMutation.isPending}
          onCancel={() => { setFormMode(null); createMutation.reset(); editMutation.reset() }}
          onSubmit={submitEditor}
          error={mutationError}
          onRetry={mutationRetry}
        />
      )}

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(24rem,0.85fr)_minmax(0,1.6fr)]">
        <Card
          title={t('dailyReports.reportList')}
          actions={(
            <div className="flex flex-wrap gap-2">
              <label className="flex items-center gap-2 text-xs">
                <span className="sr-only">{t('dailyReports.statusFilter')}</span>
                <select aria-label={t('dailyReports.statusFilter')} value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setListOffset(0) }} className="max-w-44 rounded-md border border-graphite-300 bg-white px-2 py-1.5 text-xs dark:border-graphite-700 dark:bg-graphite-950">
                  {filterOptions.map((option) => <option key={option.value || 'all'} value={option.value}>{option.label}</option>)}
                </select>
              </label>
              <label className="flex items-center gap-2 text-xs">
                <span className="sr-only">{t('dailyReports.shiftFilter')}</span>
                <select aria-label={t('dailyReports.shiftFilter')} value={shiftFilter} onChange={(event) => { setShiftFilter(event.target.value); setListOffset(0) }} className="max-w-36 rounded-md border border-graphite-300 bg-white px-2 py-1.5 text-xs dark:border-graphite-700 dark:bg-graphite-950">
                  {shiftFilterOptions.map((option) => <option key={option.value || 'all'} value={option.value}>{option.label}</option>)}
                </select>
              </label>
            </div>
          )}
          data-testid="daily-report-list-card"
        >
          <div className="mb-4 grid gap-3 sm:grid-cols-2">
            <label htmlFor="daily-report-filter-date-from" className="block space-y-1.5">
              <span className="block text-xs font-medium text-graphite-700 dark:text-graphite-200">{t('dailyReports.reportDateFrom')}</span>
              <input id="daily-report-filter-date-from" type="date" value={reportDateFrom} onChange={(event) => { setReportDateFrom(event.target.value); setListOffset(0) }} className="w-full rounded-md border border-graphite-300 bg-white px-2.5 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-950" />
            </label>
            <label htmlFor="daily-report-filter-date-to" className="block space-y-1.5">
              <span className="block text-xs font-medium text-graphite-700 dark:text-graphite-200">{t('dailyReports.reportDateTo')}</span>
              <input id="daily-report-filter-date-to" type="date" value={reportDateTo} onChange={(event) => { setReportDateTo(event.target.value); setListOffset(0) }} className="w-full rounded-md border border-graphite-300 bg-white px-2.5 py-2 text-sm dark:border-graphite-700 dark:bg-graphite-950" />
            </label>
          </div>
          {listContent()}
          {!dateRangeInvalid && reportListQuery.data && reportListQuery.data.total > 0 && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-graphite-100 pt-3 dark:border-graphite-800">
              <p className="text-xs text-graphite-500" role="status" aria-live="polite">
                {t('dailyReports.pageRange', {
                  from: formatNumber(reportListQuery.data.offset + 1, locale, 0),
                  to: formatNumber(Math.min(reportListQuery.data.offset + reportListQuery.data.items.length, reportListQuery.data.total), locale, 0),
                  total: formatNumber(reportListQuery.data.total, locale, 0),
                })}
              </p>
              <div className="flex gap-2">
                <Button size="sm" disabled={reportListQuery.data.offset <= 0} onClick={() => setListOffset(Math.max(0, listOffset - reportListQuery.data!.limit))} aria-label={t('dailyReports.previousPage')}>
                  {t('dailyReports.previousPage')}
                </Button>
                <Button size="sm" disabled={reportListQuery.data.offset + reportListQuery.data.limit >= reportListQuery.data.total} onClick={() => setListOffset(listOffset + reportListQuery.data!.limit)} aria-label={t('dailyReports.nextPage')}>
                  {t('dailyReports.nextPage')}
                </Button>
              </div>
            </div>
          )}
        </Card>

        <div className="space-y-4">
          {!selectedReportId ? (
            <Card title={t('dailyReports.selectedReport')}>
              <EmptyState message={t('dailyReports.selectReport')} />
            </Card>
          ) : detailQuery.isPending ? (
            <Card title={t('dailyReports.selectedReport')}><Loading /></Card>
          ) : detailQuery.error ? (
            <ErrorState error={detailQuery.error} onRetry={() => { void detailQuery.refetch() }} />
          ) : reportDetail && report ? (
            <>
              <Card
                title={t('dailyReports.detail')}
                subtitle={(
                  <>
                    {report.report_number && <><span>{t('dailyReports.reportNumber')}:</span> <bdi dir="auto">{report.report_number}</bdi> · </>}
                    <span>{t('dailyReports.wellbore')}:</span> {selectedWellbore ? `${selectedWellbore.name} · ${selectedWellbore.sequence}` : <bdi dir="ltr">{report.wellbore_id}</bdi>} ·
                    <bdi dir="ltr">{report.report_date}</bdi> · {labelEnum(t, 'shifts', report.shift_code)}{report.shift_label ? ` · ${report.shift_label}` : ''} · <bdi dir="ltr">{report.report_timezone}</bdi>
                  </>
                )}
                actions={(
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    {latestRevision && isViewingLatest && latestRevision.status === 'draft' && (
                      <Button onClick={openEdit} disabled={editGate.suggestDisabled} title={editGate.reason || undefined} data-testid="daily-report-edit">
                        {t('dailyReports.editDraft')}
                      </Button>
                    )}
                    {latestRevision && isViewingLatest && latestRevision.status === 'draft' && (
                      <Button variant="primary" onClick={() => { setActionMode('submit'); setActionReason(''); setActionMutationErrorReset() }} disabled={submitGate.suggestDisabled || !revisionFingerprintValid} title={submitGate.reason || (!revisionFingerprintValid ? t('dailyReports.fingerprintMismatch') : undefined)} data-testid="daily-report-submit">
                        {t('dailyReports.submitForReview')}
                      </Button>
                    )}
                    {latestRevision && isViewingLatest && latestRevision.status === 'submitted' && (
                      <>
                        <Button variant="primary" onClick={() => { setActionMode('approve'); setActionReason(''); setActionMutationErrorReset() }} disabled={approveGate.suggestDisabled || submitterIsCurrentUser || !revisionFingerprintValid} title={submitterIsCurrentUser ? t('dailyReports.authorCannotReview') : approveGate.reason || undefined} data-testid="daily-report-approve">
                          {t('dailyReports.approve')}
                        </Button>
                        <Button variant="danger" onClick={() => { setActionMode('reject'); setActionReason(''); setActionMutationErrorReset() }} disabled={rejectGate.suggestDisabled || submitterIsCurrentUser || !revisionFingerprintValid} title={submitterIsCurrentUser ? t('dailyReports.authorCannotReview') : rejectGate.reason || undefined} data-testid="daily-report-reject">
                          {t('dailyReports.reject')}
                        </Button>
                      </>
                    )}
                    {latestRevision && isViewingLatest && canWithdrawState && (
                      <Button variant="ghost" onClick={() => { setActionMode('withdraw'); setActionReason(''); setActionMutationErrorReset() }} disabled={withdrawGate.suggestDisabled || !currentUserOwnsWithdrawal} title={!currentUserOwnsWithdrawal ? t('dailyReports.withdrawOwnerOnly') : withdrawGate.reason || undefined} data-testid="daily-report-withdraw">
                        {t('dailyReports.withdraw')}
                      </Button>
                    )}
                    {canCorrectState && isViewingLatest && (
                      <Button onClick={() => { setActionMode('correct'); setActionReason(''); setActionMutationErrorReset() }} disabled={reviseGate.suggestDisabled || !revisionFingerprintValid} title={reviseGate.reason || (!revisionFingerprintValid ? t('dailyReports.fingerprintMismatch') : undefined)} data-testid="daily-report-correction">
                        {t('dailyReports.createCorrection')}
                      </Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={clearSelection} aria-label={t('dailyReports.cancel')}>{t('dailyReports.cancel')}</Button>
                  </div>
                )}
                data-testid="daily-report-detail"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={statusTone(activeRevision?.status)}>{activeRevision ? revisionStatusLabel(activeRevision.status, t) : '—'}</Badge>
                  {activeRevision && <Badge>{t('dailyReports.revision', { number: activeRevision.revision_number })}</Badge>}
                  {activeRevision && <Badge tone="neutral">{t('dailyReports.sourceKind')}: {activeRevision.source_kind === 'manual' ? t('dailyReports.manualSource') : <bdi dir="ltr">{activeRevision.source_kind}</bdi>}</Badge>}
                </div>
                {activeRevision && (
                  <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
                    <ProvenanceItem label={t('dailyReports.createdBy')} value={activeRevision.created_by} />
                    <ProvenanceItem label={t('dailyReports.submittedBy')} value={activeRevision.submitted_by ?? t('dailyReports.noValue')} />
                    <ProvenanceItem label={t('dailyReports.decidedBy')} value={activeRevision.decided_by ?? t('dailyReports.noValue')} />
                    <ProvenanceItem label={t('dailyReports.decisionReason')} value={activeRevision.decision_reason ?? t('dailyReports.noValue')} />
                    <ProvenanceItem label={t('dailyReports.editVersion')} value={String(activeRevision.edit_version)} />
                    <ProvenanceItem label={t('dailyReports.createdAt')} value={revisionCreatedLabel} />
                    {report.latest_approved_revision_number !== null && <ProvenanceItem label={t('dailyReports.latestApproved')} value={String(report.latest_approved_revision_number)} />}
                    {activeRevision.supersedes_revision_id && <ProvenanceItem label={t('dailyReports.supersedes')} value={activeRevision.supersedes_revision_id} />}
                  </dl>
                )}
                {selectedRevisionNumber && selectedRevisionNumber !== report.latest_revision_number && (
                  <p className="mt-4 rounded-md bg-amber-50 p-3 text-xs text-warning dark:bg-amber-950/30">
                    {t('dailyReports.viewingHistoricalRevision', { number: selectedRevisionNumber })}
                  </p>
                )}
                {!isViewingLatest && <p className="mt-3 text-xs text-graphite-500">{t('dailyReports.actionUnavailable')}</p>}
                {activeRevision && !activeRevision.content_hash_verified && <p className="mt-3 rounded-md bg-red-50 p-3 text-xs text-danger dark:bg-red-950/30">{t('dailyReports.fingerprintMismatch')}</p>}
                {latestRevision && isViewingLatest && (
                  <div className="mt-3 space-y-1 text-xs text-graphite-500" aria-live="polite" data-testid="daily-report-action-gate-reasons">
                    {latestRevision.status === 'draft' && editGate.suggestDisabled && <p>{editGate.reason}</p>}
                    {latestRevision.status === 'draft' && submitGate.suggestDisabled && <p>{submitGate.reason}</p>}
                    {latestRevision.status === 'submitted' && submitterIsCurrentUser && <p className="text-warning">{t('dailyReports.authorCannotReview')}</p>}
                    {latestRevision.status === 'submitted' && !submitterIsCurrentUser && approveGate.suggestDisabled && <p>{approveGate.reason}</p>}
                    {latestRevision.status === 'submitted' && !submitterIsCurrentUser && rejectGate.suggestDisabled && rejectGate.reason !== approveGate.reason && <p>{rejectGate.reason}</p>}
                    {canWithdrawState && !currentUserOwnsWithdrawal && <p>{t('dailyReports.withdrawOwnerOnly')}</p>}
                    {canWithdrawState && currentUserOwnsWithdrawal && withdrawGate.suggestDisabled && <p>{withdrawGate.reason}</p>}
                    {canCorrectState && reviseGate.suggestDisabled && <p>{reviseGate.reason}</p>}
                  </div>
                )}
                {localActionError && <p role="alert" className="mt-3 text-xs text-danger">{t(localActionError)}</p>}
              </Card>

              {actionMode && activeRevision && (
                <Card title={actionMode === 'correct' ? t('dailyReports.createCorrection') : t('dailyReports.reviewReason')}>
                  <form onSubmit={performAction} data-testid="daily-report-action-form">
                    <TextAreaField id="daily-report-action-reason" label={t('dailyReports.reason')} value={actionReason} onChange={setActionReason} rows={3} required help={t('dailyReports.reasonHint')} testId="daily-report-action-reason" />
                    {actionGate.suggestDisabled && <p className="mt-2 text-xs text-graphite-500">{actionGate.reason}</p>}
                    {actionError && <div className="mt-3"><ErrorState error={actionError} onRetry={actionMode === 'correct' ? correctionRetry : actionRetry} /></div>}
                    <div className="mt-4 flex flex-wrap justify-end gap-2">
                      <Button onClick={() => { setActionMode(null); setActionReason(''); setLocalActionError(null) }}>{t('dailyReports.cancel')}</Button>
                      <Button type="submit" variant={actionMode === 'reject' ? 'danger' : 'primary'} disabled={actionGate.suggestDisabled || actionMutationBusy} data-testid="daily-report-action-confirm">
                        {actionMutationBusy ? t(actionMode === 'correct' ? 'dailyReports.creatingCorrection' : 'dailyReports.saving') : t(actionMode === 'correct' ? 'dailyReports.createCorrection' : actionMode === 'submit' ? 'dailyReports.submitForReview' : actionMode === 'approve' ? 'dailyReports.approve' : actionMode === 'reject' ? 'dailyReports.reject' : 'dailyReports.withdraw')}
                      </Button>
                    </div>
                  </form>
                </Card>
              )}

              {selectedRevisionLoading && <Card><Loading /></Card>}
              {selectedRevisionError && <ErrorState error={oldRevisionQuery.error} onRetry={() => { void oldRevisionQuery.refetch() }} />}
              {activeRevision && !selectedRevisionLoading && !selectedRevisionError && <RevisionContent revision={activeRevision} />}
              {activeRevision && <SourceProvenance revision={activeRevision} />}

              <Card title={t('dailyReports.history')}>
                {historyQuery.isPending ? <Loading /> : historyQuery.error ? (
                  <ErrorState error={historyQuery.error} onRetry={() => { void historyQuery.refetch() }} />
                ) : !historyQuery.data?.items.length ? (
                  <EmptyState message={t('common.empty')} />
                ) : (
                  <ol className="space-y-2" data-testid="daily-report-history">
                    {historyQuery.data.items.map((revision) => {
                      const selected = selectedRevisionNumber === revision.revision_number || (!selectedRevisionNumber && revision.revision_number === report.latest_revision_number)
                      return (
                        <li key={revision.id}>
                          <button type="button" onClick={() => selectReport(report.id, revision.revision_number)} aria-current={selected ? 'true' : undefined} className={`flex w-full flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-start focus-visible:outline focus-visible:outline-2 focus-visible:outline-signal ${selected ? 'border-signal bg-signal/5' : 'border-graphite-200 hover:bg-graphite-50 dark:border-graphite-700 dark:hover:bg-graphite-800'}`} data-testid={`daily-report-revision-${revision.revision_number}`}>
                            <span className="flex flex-wrap items-center gap-2">
                              <strong className="text-xs">{t('dailyReports.revision', { number: revision.revision_number })}</strong>
                              <Badge tone={statusTone(revision.status)}>{revisionStatusLabel(revision.status, t)}</Badge>
                            </span>
                            <span className="text-[11px] text-graphite-500">{revision.created_at ? formatDateTime(revision.created_at, locale) : '—'} · v{revision.edit_version}</span>
                          </button>
                          {revision.decision_reason && <p className="ms-3 mt-1 text-xs text-graphite-500">{t('dailyReports.decisionReason')}: {revision.decision_reason}</p>}
                        </li>
                      )
                    })}
                  </ol>
                )}
              </Card>
            </>
          ) : null}
        </div>
      </div>
    </div>
  )

  function setActionMutationErrorReset() {
    actionMutation.reset()
    correctionMutation.reset()
    setLocalActionError(null)
  }
}
