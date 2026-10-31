/**
 * Operations Advisor workspace.
 *
 * The advisor's answer is displayed as the contract defines it: facts, calculations, evidence,
 * inference, recommendation, unknown. The distinction is the product — a reader must be able to see
 * which sentence is a recorded fact, which is an engine result, which is reasoning, and which is a
 * gap. The optional LLM narration is visually separated from the computed sections and cannot
 * change them: the API assembles facts and calculations before the model is called at all.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { AdvisorAnswer } from '../../api/types'
import { EvidencePanel } from '../../components/evidence/EvidencePanel'
import { PageHeader } from '../../components/layout/AppShell'
import {
  Async,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Json,
  Loading,
} from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, formatValidationStatus } from '../../lib/format'

const SECTION_ORDER = ['facts', 'calculations', 'evidence', 'inference', 'recommendation', 'unknown'] as const
type SectionKey = (typeof SECTION_ORDER)[number]

const SECTION_TONE: Record<SectionKey, 'ok' | 'info' | 'neutral' | 'warning' | 'danger'> = {
  facts: 'ok',
  calculations: 'info',
  evidence: 'neutral',
  inference: 'warning',
  recommendation: 'info',
  unknown: 'danger',
}

export function AdvisorItemCard({ item }: { item: Record<string, unknown> }) {
  const { t } = useI18n()
  const label = String(item.label ?? item.key ?? item.title ?? item.kind ?? 'item')
  const value = item.value ?? item.detail ?? item.statement ?? null
  const unit = typeof item.unit === 'string' ? item.unit : null
  const source = typeof item.source === 'string' ? item.source : null
  const engine = typeof item.engine_key === 'string' ? item.engine_key : null
  const engineVersion = typeof item.engine_version === 'string' ? item.engine_version : null
  const validationStatus = typeof item.validation_status === 'string' ? item.validation_status : null
  const fieldValidationRequired =
    item.field_validation_required === true || validationStatus === 'needs_field_validation'
  const basis = typeof item.basis === 'string' ? item.basis : null
  const how = typeof item.how_to_obtain === 'string' ? item.how_to_obtain : null
  const evidenceCount = Array.isArray(item.evidence) ? (item.evidence as unknown[]).length : 0

  return (
    <li className="rounded-md border border-graphite-100 p-2.5 dark:border-graphite-800">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-xs font-medium">{label}</span>
        {value !== null && typeof value !== 'object' && (
          <span className="font-mono text-sm">
            {String(value)}
            {unit && <span className="ms-1 text-[11px] text-graphite-500">{unit}</span>}
          </span>
        )}
      </div>
      {value !== null && typeof value === 'object' && <Json value={value} max={60} />}
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-graphite-500">
        {engine && (
          <Badge tone="info">
            {engine}
            {engineVersion ? `@${engineVersion}` : ''}
          </Badge>
        )}
        {validationStatus && (
          <Badge tone={fieldValidationRequired ? 'danger' : 'neutral'}>
            {t('engineering.validationStatus')}: {formatValidationStatus(validationStatus, t)}
          </Badge>
        )}
        {source && <span className="font-mono">{source}</span>}
        {basis && <span>basis: {basis}</span>}
        {how && <span>how: {how}</span>}
        {evidenceCount > 0 && <Badge tone="neutral">{evidenceCount} evidence</Badge>}
      </div>
      {fieldValidationRequired && (
        <p role="note" data-testid="field-validation-warning" className="mt-2 rounded border border-danger/40 bg-red-50/60 p-2 text-xs text-danger dark:bg-red-950/20">
          {t('engineering.fieldValidationWarning')}
        </p>
      )}
    </li>
  )
}

export function AnswerView({
  answer,
  onEvidence,
  llmRequested,
}: {
  answer: AdvisorAnswer
  onEvidence: () => void
  llmRequested: boolean
}) {
  const { t } = useI18n()
  const narrativeText =
    answer.narrative?.status === 'generated' && typeof answer.narrative.text === 'string'
      ? answer.narrative.text
      : null
  const narrativeStatusMessage =
    answer.narrative?.status === 'blocked_by_guardrail'
      ? t('advisor.narrativeBlocked')
      : answer.narrative?.status === 'unavailable'
        ? t('advisor.narrativeUnavailable')
        : llmRequested
          ? t('advisor.narrativeNone')
          : t('advisor.narrativeNotRequested')
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="info">{answer.question}</Badge>
        <span className="text-xs text-graphite-500">{answer.question_text}</span>
        <Badge tone="neutral">{formatDateTime(answer.generated_at)}</Badge>
        <Button size="sm" variant="ghost" onClick={onEvidence}>
          {t('common.showEvidence')}
        </Button>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        {SECTION_ORDER.map((key) => {
          const items = answer[key]
          const title =
            key === 'facts'
              ? t('advisor.facts')
              : key === 'calculations'
                ? t('advisor.calculations')
                : key === 'evidence'
                  ? t('advisor.evidence')
                  : key === 'inference'
                    ? t('advisor.inference')
                    : key === 'recommendation'
                      ? t('advisor.recommendation')
                      : t('advisor.unknown')
          return (
            <Card
              key={key}
              title={title}
              actions={<Badge tone={SECTION_TONE[key]}>{items.length}</Badge>}
              subtitle={
                key === 'facts'
                  ? 'read from recorded rows'
                  : key === 'calculations'
                    ? 'engine outputs with their version'
                    : key === 'inference'
                      ? 'reasoning that goes beyond the records'
                      : key === 'unknown'
                        ? 'what the platform does not know, and how to obtain it'
                        : undefined
              }
            >
              {items.length === 0 ? (
                <EmptyState message="Nothing in this section." />
              ) : (
                <ul className="space-y-2">
                  {items.map((item, index) => (
                    <AdvisorItemCard key={`${key}-${index}`} item={item} />
                  ))}
                </ul>
              )}
            </Card>
          )
        })}
      </div>

      <Card
        title={t('advisor.narrative')}
        subtitle={
          answer.narrative?.model
            ? `${t('advisor.copyOnlyFrom')} ${answer.narrative.model}`
            : undefined
        }
      >
        {narrativeText !== null ? (
          <blockquote className="border-s-2 border-signal/40 ps-3 text-sm whitespace-pre-wrap">
            {narrativeText}
          </blockquote>
        ) : (
          <EmptyState message={narrativeStatusMessage} hint={t('advisor.llmHint')} />
        )}
        <p className="mt-2 text-[11px] text-graphite-500">
          {t('advisor.llmHint')}
        </p>
      </Card>

      {answer.sources.length > 0 && (
        <p className="text-[11px] text-graphite-500">sources: {answer.sources.join(', ')}</p>
      )}
    </div>
  )
}

export default function AdvisorWorkspace() {
  const { t } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const [searchParams] = useSearchParams()
  const linkedAlertId = searchParams.get('alert')
  const [question, setQuestion] = useState(linkedAlertId ? 'what_went_wrong' : 'where_are_we')
  const [useLlm, setUseLlm] = useState(false)
  const [answer, setAnswer] = useState<AdvisorAnswer | null>(null)
  const [answerUsedLlm, setAnswerUsedLlm] = useState(false)
  const [showEvidence, setShowEvidence] = useState(false)

  const questions = useQuery({ queryKey: ['advisor-questions'], queryFn: ({ signal }) => drillingApi.advisorQuestions(signal) })
  const ask = useMutation({
    mutationFn: (payload: { question: string; use_llm: boolean }) => drillingApi.askAdvisor(id, payload),
    onSuccess: (data, variables) => {
      setAnswer(data.answer)
      setAnswerUsedLlm(variables.use_llm)
    },
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('advisor.title')}
        description="Facts, calculations, evidence, inference, recommendation and unknown are separated on purpose: the advisor never blurs a record with a guess."
        breadcrumb={<Link to={`/wells/${id}/cockpit`}>{t('nav.cockpit')}</Link>}
        actions={
          linkedAlertId ? (
            <Link
              to={`/wells/${id}/cockpit?tab=live&alert=${encodeURIComponent(linkedAlertId)}`}
              data-testid="advisor-linked-alert-badge"
            >
              <Badge tone="warning">
                Alert context: <span dir="ltr">{linkedAlertId.slice(0, 8)}</span>
              </Badge>
            </Link>
          ) : undefined
        }
      />

      <Card title={t('advisor.question')}>
        <Async query={questions}>
          {(data) => (
            <div className="space-y-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="min-w-[260px] flex-1">
                  <span className="mb-1 block text-xs font-medium text-graphite-600 dark:text-graphite-300">
                    {t('advisor.question')}
                  </span>
                  <select
                    value={question}
                    onChange={(event) => setQuestion(event.target.value)}
                    className="w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
                  >
                    {data.questions.map((item) => (
                      <option key={item.key} value={item.key}>
                        {item.description}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={useLlm} onChange={(event) => setUseLlm(event.target.checked)} />
                  {t('advisor.useLlm')}
                </label>
                <Button
                  variant="primary"
                  onClick={() => ask.mutate({ question, use_llm: useLlm })}
                  disabled={ask.isPending}
                >
                  {t('advisor.ask')}
                </Button>
              </div>

              <details className="rounded border border-graphite-100 p-2 text-[11px] dark:border-graphite-800">
                <summary className="cursor-pointer font-medium">{t('advisor.contract')}</summary>
                <ul className="mt-1 list-disc ps-4">
                  {Object.entries(data.contract).map(([key, text]) => (
                    <li key={key}>
                      <span className="font-medium">{key}</span>: {text}
                    </li>
                  ))}
                </ul>
              </details>

              {ask.isPending && <Loading label="Assembling facts and calculations…" />}
              {ask.error && <ErrorState error={ask.error} onRetry={() => ask.mutate({ question, use_llm: useLlm })} />}
            </div>
          )}
        </Async>
      </Card>

      {answer && (
        <AnswerView
          answer={answer}
          onEvidence={() => setShowEvidence(true)}
          llmRequested={answerUsedLlm}
        />
      )}

      <EvidencePanel
        open={showEvidence}
        onClose={() => setShowEvidence(false)}
        subjectKind="advisor_answer"
        subjectId={answer?.question}
        wellId={id}
        items={(answer?.evidence ?? []).map((item) => ({
          document_id: (item.document_id as string) ?? null,
          page: (item.page as number) ?? null,
          excerpt: (item.excerpt as string) ?? null,
          method: (item.method as string) ?? null,
          confidence: (item.confidence as number) ?? null,
          title: (item.document_title as string) ?? null,
        }))}
      />
    </div>
  )
}
