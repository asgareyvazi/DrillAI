import { useState } from 'react'
import type { FormEvent } from 'react'
import type { ExtractionRecord, ExtractedRecordReviewRequest } from '../../api/types'
import { Button, ErrorState, Field } from '../common'
import { useI18n } from '../../i18n'
import { formatDateTime, humanise } from '../../lib/format'

const inputClass =
  'w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900'

function toLocalDateTime(value: string | null): string {
  if (!value) return ''
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return ''
  const local = new Date(timestamp - new Date(timestamp).getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

function localDateTimeToUtc(value: string): string | undefined {
  if (!value) return undefined
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : undefined
}

function newKey(record: ExtractionRecord): string {
  return `ui-record-review-${record.id}-v${record.review_version}-${crypto.randomUUID()}`
}

export function ExtractedRecordReviewForm({
  record,
  onSubmit,
  onCancel,
  onChanged,
  onRefresh,
  pending,
  error,
}: {
  record: ExtractionRecord
  onSubmit: (body: ExtractedRecordReviewRequest, key: string) => void
  onCancel: () => void
  onChanged: () => void
  onRefresh: () => void
  pending: boolean
  error: unknown
}) {
  const { t } = useI18n()
  const canValidate =
    record.validation_state === 'extracted' ||
    record.validation_state === 'rule_validated' ||
    record.validation_state === 'human_validated'
  const canReject = record.validation_state !== 'rejected' && record.validation_state !== 'superseded'
  const reviewStateLabels: Record<string, string> = {
    extracted: t('documents.reviewStateExtracted'),
    rule_validated: t('documents.reviewStateRuleValidated'),
    human_validated: t('documents.reviewStateHumanValidated'),
    rejected: t('documents.reviewStateRejected'),
    superseded: t('documents.reviewStateSuperseded'),
  }
  const reviewStateLabel = reviewStateLabels[record.validation_state] ?? humanise(record.validation_state)
  const [decision, setDecision] = useState<'validate' | 'reject'>(canValidate ? 'validate' : 'reject')
  const [payloadText, setPayloadText] = useState(() => JSON.stringify(record.payload, null, 2))
  const [observedAt, setObservedAt] = useState(() => toLocalDateTime(record.observed_at))
  const [observedAtChanged, setObservedAtChanged] = useState(false)
  const [depthMd, setDepthMd] = useState(() => (record.depth_md_si === null ? '' : String(record.depth_md_si)))
  const [depthMdChanged, setDepthMdChanged] = useState(false)
  const [depthTvd, setDepthTvd] = useState(() => (record.depth_tvd_si === null ? '' : String(record.depth_tvd_si)))
  const [depthTvdChanged, setDepthTvdChanged] = useState(false)
  const [reason, setReason] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState(() => newKey(record))

  const changed = () => {
    setIdempotencyKey(newKey(record))
    setValidationError(null)
    onChanged()
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    let correctedPayload: unknown
    try {
      correctedPayload = JSON.parse(payloadText)
    } catch {
      setValidationError(t('documents.reviewPayloadJsonInvalid'))
      return
    }
    if (correctedPayload === null || typeof correctedPayload !== 'object' || Array.isArray(correctedPayload)) {
      setValidationError(t('documents.reviewPayloadMustBeObject'))
      return
    }
    const utcObservedAt = localDateTimeToUtc(observedAt)
    if (observedAtChanged && observedAt && !utcObservedAt) {
      setValidationError(t('documents.reviewTimeInvalid'))
      return
    }
    const changes: ExtractedRecordReviewRequest['changes'] = {
      payload: correctedPayload as Record<string, unknown>,
    }
    if (observedAtChanged) changes.observed_at = utcObservedAt ?? null
    for (const [name, value, wasChanged] of [
      ['depth_md_si', depthMd, depthMdChanged],
      ['depth_tvd_si', depthTvd, depthTvdChanged],
    ] as const) {
      if (!wasChanged) continue
      if (!value.trim()) {
        changes[name] = null
        continue
      }
      const numeric = Number(value)
      if (!Number.isFinite(numeric) || numeric < 0) {
        setValidationError(t('documents.reviewDepthInvalid'))
        return
      }
      changes[name] = numeric
    }
    if (!reason.trim()) {
      setValidationError(t('documents.reviewReasonRequired'))
      return
    }
    setValidationError(null)
    onSubmit(
      {
        expected_version: record.review_version,
        decision,
        reason: reason.trim(),
        changes,
      },
      idempotencyKey,
    )
  }

  return (
    <form className="space-y-3" data-testid="extracted-record-review-form" onSubmit={submit}>
      <div className="flex flex-wrap gap-2 text-xs">
        <span>{t('documents.reviewVersion', { version: record.review_version })}</span>
        <span>·</span>
        <span>{reviewStateLabel}</span>
        {record.validated_at ? <span>· {formatDateTime(record.validated_at)}</span> : null}
      </div>
      {record.validation_notes ? (
        <p className="rounded border border-graphite-200 p-2 text-xs text-graphite-600 dark:border-graphite-800 dark:text-graphite-300">
          {t('documents.reviewPreviousNote')}: {record.validation_notes}
        </p>
      ) : null}
      <Field label={t('documents.reviewDecision')}>
        <select
          className={inputClass}
          value={decision}
          disabled={pending || !canReject}
          onChange={(event) => {
            setDecision(event.target.value as 'validate' | 'reject')
            changed()
          }}
          data-testid="extracted-record-review-decision"
        >
          {canValidate ? <option value="validate">{t('documents.validateRecord')}</option> : null}
          {canReject ? <option value="reject">{t('documents.rejectRecord')}</option> : null}
        </select>
      </Field>
      <Field label={t('documents.reviewPayload')} hint={t('documents.reviewPayloadHint')}>
        <textarea
          className={`${inputClass} min-h-64 font-mono text-xs`}
          value={payloadText}
          rows={14}
          dir="ltr"
          spellCheck={false}
          disabled={pending}
          onChange={(event) => {
            setPayloadText(event.target.value)
            changed()
          }}
          data-testid="extracted-record-review-payload"
        />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('documents.reviewObservedAt')} hint={t('documents.reviewTimezoneHint')}>
          <input
            className={inputClass}
            type="datetime-local"
            value={observedAt}
            disabled={pending}
            onChange={(event) => {
              setObservedAt(event.target.value)
              setObservedAtChanged(true)
              changed()
            }}
            data-testid="extracted-record-review-observed-at"
          />
        </Field>
        <Field label={t('documents.reviewDepthMd')} hint="m (SI)">
          <input
            className={inputClass}
            type="number"
            min="0"
            step="any"
            value={depthMd}
            disabled={pending}
            onChange={(event) => {
              setDepthMd(event.target.value)
              setDepthMdChanged(true)
              changed()
            }}
            data-testid="extracted-record-review-depth-md"
          />
        </Field>
        <Field label={t('documents.reviewDepthTvd')} hint="m (SI)">
          <input
            className={inputClass}
            type="number"
            min="0"
            step="any"
            value={depthTvd}
            disabled={pending}
            onChange={(event) => {
              setDepthTvd(event.target.value)
              setDepthTvdChanged(true)
              changed()
            }}
            data-testid="extracted-record-review-depth-tvd"
          />
        </Field>
      </div>
      <Field label={t('documents.reviewReason')} hint={t('documents.reviewReasonHint')}>
        <textarea
          className={inputClass}
          value={reason}
          maxLength={2000}
          rows={3}
          required
          disabled={pending}
          onChange={(event) => {
            setReason(event.target.value)
            changed()
          }}
          data-testid="extracted-record-review-reason"
        />
      </Field>
      {validationError ? <p role="alert" className="text-sm text-danger">{validationError}</p> : null}
      {error ? <ErrorState error={error} /> : null}
      {error ? (
        <Button onClick={onRefresh} disabled={pending} data-testid="extracted-record-review-refresh">
          {t('documents.reviewReload')}
        </Button>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          type="submit"
          variant="primary"
          disabled={pending || !canReject || (decision === 'validate' && !canValidate) || !reason.trim()}
          data-testid="extracted-record-review-submit"
        >
          {pending ? t('documents.reviewSaving') : t('documents.reviewSave')}
        </Button>
        <Button onClick={onCancel} disabled={pending}>{t('common.cancel')}</Button>
      </div>
    </form>
  )
}
