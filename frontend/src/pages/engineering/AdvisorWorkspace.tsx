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
import type { AdvisorAnswer, EngineValueUnitContract } from '../../api/types'
import { UnitAwareResult } from '../../components/engineering/UnitAwareResult'
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function unitFields(units: unknown, key: string): Record<string, EngineValueUnitContract> {
  if (!isRecord(units) || !isRecord(units[key])) return {}
  return units[key] as Record<string, EngineValueUnitContract>
}

const CITATION_KIND_KEYS: Record<string, string> = {
  database_record: 'advisor.databaseRecord',
  derived_record: 'advisor.derivedRecord',
  scoped_aggregate: 'advisor.scopedAggregate',
  measurement_point: 'advisor.measurementPoint',
  extracted_record: 'advisor.extractedRecord',
  engine_run: 'advisor.engineRun',
  recommendation_record: 'advisor.recommendationRecord',
  document_excerpt: 'advisor.documentExcerpt',
  scoped_query: 'advisor.scopedQuery',
  claim_reference: 'advisor.claimReference',
}

export function AdvisorItemCard({ item }: { item: Record<string, unknown> }) {
  const { t } = useI18n()
  const label = String(item.label ?? item.key ?? item.title ?? item.kind ?? 'item')
  const statement = typeof item.statement === 'string' ? item.statement : null
  const value = item.value ?? item.detail ?? null
  const unit = typeof item.unit === 'string' ? item.unit : null
  const source = typeof item.source === 'string' ? item.source : null
  const sourceId = typeof item.source_id === 'string' ? item.source_id : null
  const evidenceRef = typeof item.evidence_ref === 'string' ? item.evidence_ref : null
  const provenance = isRecord(item.provenance) ? item.provenance : null
  const engine = typeof item.engine_key === 'string' ? item.engine_key : null
  const engineVersion = typeof item.engine_version === 'string' ? item.engine_version : null
  const validationStatus = typeof item.validation_status === 'string' ? item.validation_status : null
  const fieldValidationRequired =
    item.field_validation_required === true || validationStatus === 'needs_field_validation'
  const basis = typeof item.basis === 'string' ? item.basis : null
  const how = typeof item.how_to_obtain === 'string' ? item.how_to_obtain : null
  const evidenceCount = Array.isArray(item.evidence) ? (item.evidence as unknown[]).length : 0
  const outputs = isRecord(item.outputs) ? item.outputs : null
  const violations = Array.isArray(item.violations) ? item.violations : []
  const citations = Array.isArray(item.citations) ? item.citations.filter(isRecord) : []
  const grounding = isRecord(item.grounding) ? item.grounding : null
  const freshness = grounding && isRecord(grounding.freshness) ? grounding.freshness : null
  const freshnessStatus = typeof freshness?.status === 'string' ? freshness.status : null
  const freshnessLabelKey =
    freshnessStatus === 'fresh'
      ? 'advisor.fresh'
      : freshnessStatus === 'stale'
        ? 'advisor.stale'
        : freshnessStatus === 'missing'
          ? 'advisor.missingFreshness'
          : freshnessStatus === 'unknown'
            ? 'advisor.unknownFreshness'
            : freshnessStatus === 'not_assessed'
              ? 'advisor.notAssessed'
              : null
  const freshnessTone =
    freshnessStatus === 'fresh'
      ? 'ok'
      : freshnessStatus === 'not_assessed'
        ? 'neutral'
        : freshnessStatus
          ? 'warning'
          : 'neutral'

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
      {statement && (
        <p className="mt-1 text-sm leading-relaxed text-graphite-700 dark:text-graphite-200">{statement}</p>
      )}
      {outputs && Object.keys(outputs).length > 0 && (
        <div className="mt-2 space-y-1">
          <h4 className="text-[11px] font-medium">{t('engineering.outputs')}</h4>
          <UnitAwareResult value={outputs} unitFields={unitFields(item.units, 'output_fields')} />
        </div>
      )}
      {violations.length > 0 && (
        <div className="mt-2 space-y-1">
          <h4 className="text-[11px] font-medium text-danger">{t('engineering.violations')}</h4>
          <UnitAwareResult
            value={{ violations }}
            unitFields={unitFields(item.units, 'violation_fields')}
          />
        </div>
      )}
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
        {sourceId && <span dir="ltr" className="font-mono">{t('advisor.sourceId')}: {sourceId}</span>}
        {evidenceRef && <span dir="ltr" className="font-mono">{t('advisor.evidenceRef')}: {evidenceRef}</span>}
        {basis && <span>basis: {basis}</span>}
        {how && <span>how: {how}</span>}
        {evidenceCount > 0 && <Badge tone="neutral">{evidenceCount} evidence</Badge>}
        {freshnessLabelKey && (
          <Badge tone={freshnessTone} data-testid="advisor-freshness">
            {t('advisor.freshness')}: {t(freshnessLabelKey)}
            {typeof freshness?.age_seconds === 'number' ? ` · ${freshness.age_seconds.toFixed(0)} s` : ''}
          </Badge>
        )}
      </div>
      {citations.length > 0 && (
        <div className="mt-2 space-y-1" data-testid="advisor-citations">
          <h4 className="text-[11px] font-medium">{t('advisor.citations')}</h4>
          <ul className="space-y-1 text-[11px] text-graphite-600 dark:text-graphite-300">
            {citations.map((citation, index) => {
              const kind = typeof citation.kind === 'string' ? citation.kind : ''
              const kindKey = CITATION_KIND_KEYS[kind] ?? 'advisor.citation'
              const citationSource = typeof citation.source === 'string' ? citation.source : null
              const citationId = typeof citation.source_id === 'string' ? citation.source_id : null
              const documentTitle = typeof citation.document_title === 'string' ? citation.document_title : null
              const pageNumber = typeof citation.page_number === 'number' ? citation.page_number : null
              const citationScopeRecord = isRecord(citation.scope) ? citation.scope : null
              const citationDocumentScopeRecord =
                citationScopeRecord && isRecord(citationScopeRecord.document_scope)
                  ? citationScopeRecord.document_scope
                  : null
              const scopeFields = ['organization_id', 'project_id', 'well_id', 'wellbore_id', 'section_id', 'operation_id']
              const citationScopeParts = citationScopeRecord
                ? scopeFields
                    .filter((field) => typeof citationScopeRecord[field] === 'string')
                    .map((field) => `${field}=${String(citationScopeRecord[field])}`)
                : []
              const documentScopeParts = citationDocumentScopeRecord
                ? scopeFields
                    .filter((field) => typeof citationDocumentScopeRecord[field] === 'string')
                    .map((field) => `document.${field}=${String(citationDocumentScopeRecord[field])}`)
                : []
              const citationScope = [...citationScopeParts, ...documentScopeParts].join(' · ')
              return (
                <li key={String(citation.citation_id ?? index)} data-testid="advisor-citation" className="flex flex-wrap gap-1">
                  <span>{t(kindKey)}:</span>
                  <span dir="ltr" className="font-mono">
                    {[citationSource, citationId, documentTitle, pageNumber === null ? null : `p. ${pageNumber}`]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                  {citationScope && (
                    <span dir="ltr" className="font-mono text-graphite-500">
                      {t('advisor.citationScope')}: {citationScope}
                    </span>
                  )}
                  {citation.quote_verified === true && <Badge tone="ok">{t('common.quoteVerified')}</Badge>}
                  {citation.quote_verified === false && <Badge tone="warning">{t('common.quoteNotVerified')}</Badge>}
                </li>
              )
            })}
          </ul>
        </div>
      )}
      {provenance && Object.keys(provenance).length > 0 && (
        <details className="mt-2 rounded border border-graphite-100 p-2 text-[11px] dark:border-graphite-800">
          <summary className="cursor-pointer font-medium">{t('advisor.provenance')}</summary>
          <Json value={provenance} max={100} />
        </details>
      )}
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
  const grounding = isRecord(answer.grounding) ? answer.grounding : null
  const groundingScope = grounding && isRecord(grounding.scope) ? grounding.scope : null
  const freshnessSummary = grounding && isRecord(grounding.freshness) ? grounding.freshness : null
  const scopeText = groundingScope
    ? [groundingScope.organization_id, groundingScope.project_id, groundingScope.well_id, groundingScope.wellbore_id]
        .filter((value): value is string => typeof value === 'string' && value.length > 0)
        .join(' / ')
    : ''
  const narrativeGroundingRefs = Array.isArray(answer.narrative?.grounding_refs)
    ? answer.narrative.grounding_refs.filter(isRecord)
    : []
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

      {grounding && (
        <Card title={t('advisor.grounding')}>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs" data-testid="advisor-grounding-summary">
            <span>
              {t('advisor.citedClaims')}: {typeof grounding.cited_claim_count === 'number' ? grounding.cited_claim_count : 0}/
              {typeof grounding.claim_count === 'number' ? grounding.claim_count : 0}
            </span>
            <span>
              {t('advisor.abstentions')}: {typeof grounding.abstained_or_unknown_count === 'number' ? grounding.abstained_or_unknown_count : 0}
            </span>
            <span>
              {t('advisor.staleReadings')}: {typeof freshnessSummary?.stale_measurement_claim_count === 'number' ? freshnessSummary.stale_measurement_claim_count : 0}
            </span>
          </div>
          {scopeText && (
            <p className="mt-2 text-[11px] text-graphite-500">
              {t('advisor.scope')}: <code dir="ltr">{scopeText}</code>
            </p>
          )}
          <p className="mt-2 text-[11px] text-graphite-500">{t('advisor.groundingPolicy')}</p>
        </Card>
      )}

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
        {narrativeGroundingRefs.length > 0 && (
          <div className="mt-2 space-y-1 text-[11px]" data-testid="advisor-narrative-grounding">
            <h4 className="font-medium">{t('advisor.narrativeSources')}</h4>
            <ul className="space-y-1">
              {narrativeGroundingRefs.map((reference, index) => {
                const referenceLabel = [reference.section, reference.key]
                  .filter((value): value is string => typeof value === 'string')
                  .join(': ')
                const citationIds = Array.isArray(reference.citation_ids)
                  ? reference.citation_ids.filter((value): value is string => typeof value === 'string')
                  : []
                return (
                  <li key={`${referenceLabel}-${index}`} className="flex flex-wrap gap-1">
                    <span>{referenceLabel}</span>
                    {citationIds.length > 0 ? (
                      <span dir="ltr" className="font-mono">{citationIds.join(' · ')}</span>
                    ) : (
                      <Badge tone="neutral">{t('advisor.abstention')}</Badge>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
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
          page: (item.page_number as number) ?? null,
          region: (item.region_id as string) ?? null,
          excerpt: (item.excerpt as string) ?? null,
          method: (item.method as string) ?? null,
          confidence: (item.confidence as number) ?? null,
          quote_verified: (item.quote_verified as boolean | null) ?? null,
          evidence_id: (item.evidence_id as string) ?? (item.id as string) ?? null,
          locator: (item.locator as Record<string, unknown>) ?? null,
          scope: {
            project_id: (item.project_id as string | null) ?? null,
            well_id: (item.well_id as string | null) ?? null,
            document_well_id: (item.document_well_id as string | null) ?? null,
            wellbore_id: (item.wellbore_id as string | null) ?? null,
            section_id: (item.section_id as string | null) ?? null,
            operation_id: (item.operation_id as string | null) ?? null,
          },
          title: (item.document_title as string) ?? null,
        }))}
      />
    </div>
  )
}
