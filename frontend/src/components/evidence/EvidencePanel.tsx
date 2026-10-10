/**
 * The reusable Evidence Panel.
 *
 * Product rule §18: every place where a number, a state or a recommendation appears must be able
 * to answer "why?" and "show the evidence". This component is that answer, and it is the only one:
 * it is driven by the evidence API, it renders page → region → excerpt, the extraction method and
 * the confidence, it says when a quote could not be verified, and it never invents an excerpt for
 * an item that has no evidence link.
 *
 * A subject may be addressed two ways:
 *   * by its own identity (`subjectKind` + `subjectId`) — the platform's evidence links, or
 *   * by explicit citations passed in (`items`) — evidence already returned inside a payload such
 *     as an engine result or an advisor answer.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { canRetry } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import type { EvidenceItem } from '../../api/types'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber, humanise } from '../../lib/format'
import { Badge, Button, Drawer, EmptyState, ErrorState, Loading } from '../common'

export interface EvidenceCitation {
  document_id?: string | null
  page?: number | null
  region?: string | null
  excerpt?: string | null
  method?: string | null
  confidence?: number | null
  quote_verified?: boolean | null
  evidence_id?: string | null
  locator?: Record<string, unknown> | null
  title?: string | null
}

function Citation({ citation }: { citation: EvidenceCitation }) {
  const { t } = useI18n()
  const locator = citation.locator ?? {}
  const region = citation.region ?? (typeof locator.region === 'string' ? locator.region : null)
  return (
    <li className="rounded-md border border-graphite-200 p-3 dark:border-graphite-800">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-graphite-500">
        {citation.title && <span className="font-medium text-graphite-700 dark:text-graphite-200">{citation.title}</span>}
        {citation.document_id && <span className="font-mono">{citation.document_id}</span>}
        {citation.page !== null && citation.page !== undefined && (
          <Badge tone="neutral">
            {t('common.page')} {citation.page}
          </Badge>
        )}
        {region && <Badge tone="neutral">{region}</Badge>}
        {citation.method && <Badge tone="info">{citation.method}</Badge>}
        {citation.confidence !== null && citation.confidence !== undefined && (
          <Badge tone={citation.confidence >= 0.8 ? 'ok' : citation.confidence >= 0.5 ? 'warning' : 'danger'}>
            {t('common.confidence')} {formatNumber(citation.confidence, 'en', 2)}
          </Badge>
        )}
        {citation.quote_verified === false && <Badge tone="warning">quote not verified in source</Badge>}
        {citation.quote_verified === true && <Badge tone="ok">quote verified</Badge>}
      </div>
      {citation.excerpt ? (
        <blockquote className="mt-2 border-s-2 border-signal/50 ps-2 text-sm text-graphite-700 italic dark:text-graphite-200">
          {citation.excerpt}
        </blockquote>
      ) : (
        <p className="mt-2 text-xs text-graphite-500 italic">
          The platform holds a link to this location but no stored excerpt.
        </p>
      )}
    </li>
  )
}

export function EvidenceList({ citations, emptyMessage }: { citations: EvidenceCitation[]; emptyMessage?: string }) {
  const { t } = useI18n()
  if (citations.length === 0) return <EmptyState message={emptyMessage ?? t('empty.noEvidence')} />
  return (
    <ul className="space-y-2">
      {citations.map((citation, index) => (
        <Citation key={citation.evidence_id ?? `${citation.document_id}-${index}`} citation={citation} />
      ))}
    </ul>
  )
}

function itemToCitation(item: EvidenceItem): EvidenceCitation {
  return {
    evidence_id: item.id,
    document_id: item.document_id,
    page: item.page_number,
    region: typeof item.locator?.region === 'string' ? item.locator.region : null,
    excerpt: item.excerpt,
    method: item.method,
    confidence: item.confidence,
    quote_verified: item.quote_verified,
    locator: item.locator,
  }
}

export function EvidencePanel({
  open,
  onClose,
  title,
  subjectKind,
  subjectId,
  wellId,
  documentId,
  items,
}: {
  open: boolean
  onClose: () => void
  title?: string
  subjectKind?: string
  subjectId?: string
  wellId?: string
  documentId?: string
  /** Citations already available in the payload; queried links are appended. */
  items?: EvidenceCitation[]
}) {
  const { t } = useI18n()
  const [minConfidence, setMinConfidence] = useState(0)

  const linked = useQuery({
    queryKey: ['evidence', { subjectKind, subjectId, wellId, documentId }],
    queryFn: ({ signal }) =>
      drillingApi.listEvidence({
        ...(subjectKind ? { subject_kind: subjectKind } : {}),
        ...(subjectId ? { subject_id: subjectId } : {}),
        ...(wellId ? { well_id: wellId } : {}),
        ...(documentId ? { document_id: documentId } : {}),
        limit: 200,
      } as never, signal),
    enabled: open,
    retry: false,
  })

  const inline = items ?? []
  const fetched = (linked.data?.items ?? []).map(itemToCitation)
  const merged = [...inline, ...fetched].filter(
    (citation) => (citation.confidence ?? 1) >= minConfidence,
  )

  return (
    <Drawer open={open} onClose={onClose} wide title={title ?? t('common.evidence')}>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-graphite-500">
            {subjectKind ? (
              <>
                <span className="font-mono">{subjectKind}</span>
                {subjectId && <> · <span className="font-mono">{subjectId}</span></>}
              </>
            ) : (
              'citations carried by this result'
            )}
          </p>
          <label className="flex items-center gap-2 text-xs">
            {t('common.confidence')} ≥
            <select
              value={minConfidence}
              onChange={(event) => setMinConfidence(Number(event.target.value))}
              className="rounded border border-graphite-300 bg-white px-1 py-0.5 dark:border-graphite-700 dark:bg-graphite-900"
            >
              {[0, 0.5, 0.7, 0.9].map((value) => (
                <option key={value} value={value}>
                  {value.toFixed(1)}
                </option>
              ))}
            </select>
          </label>
        </div>

        {linked.isLoading && <Loading />}
        {linked.error && <ErrorState error={linked.error} onRetry={() => linked.refetch()} />}
        {!linked.isLoading && !linked.error && merged.length === 0 && (
          <EmptyState
            message={t('empty.noEvidence')}
            hint={
              linked.data && linked.data.total === 0
                ? 'the platform has no evidence link for this subject; the value is stored data, not a quotation'
                : undefined
            }
          />
        )}
        {merged.length > 0 && <EvidenceList citations={merged} emptyMessage={t('empty.noEvidence')} />}

        {linked.data && linked.data.total > merged.length && (
          <p className="text-[11px] text-graphite-500">
            showing {merged.length} of {linked.data.total} links
          </p>
        )}
      </div>
    </Drawer>
  )
}

/**
 * The "Why?" affordance used next to values, timeline entries, engine results and recommendations.
 * It is deliberately a button, not a tooltip: evidence is a panel the engineer reads, not a hover.
 */
export function WhyButton({
  onClick,
  label,
}: {
  onClick: () => void
  label?: string
}) {
  const { t } = useI18n()
  return (
    <Button variant="ghost" size="sm" onClick={onClick} title={t('common.showEvidence')}>
      {label ?? t('common.whyQuestion')}
    </Button>
  )
}

/**
 * The compact evidence strip used in a page header.
 *
 * It is a summary *and* the way into the panel, so the two are one control rather than a number next
 * to a link — the count is the reason to open it.
 *
 * Which is also why it must not disappear when the count cannot be read. This strip used to render
 * `null` on any failure, and the effect was not a missing number but a missing *door*: the only way
 * into the evidence panel vanished from the cockpit, and a well whose evidence could not be counted
 * looked like a well whose cockpit simply had no evidence control. The panel itself is still there
 * and still works — it holds the citations attached to this screen — so the strip stays, says the
 * summary could not be read, and offers a retry for the count alone. The list of links is *not*
 * claimed to be empty, and the citations in the panel are untouched.
 */
export function EvidenceSummaryStrip({
  wellId,
  onOpen,
}: {
  wellId: string
  onOpen: () => void
}) {
  const { t } = useI18n()
  const summary = useQuery({
    queryKey: ['evidence-summary', wellId],
    queryFn: ({ signal }) => drillingApi.evidenceSummary({ well_id: wellId }, signal),
    retry: false,
  })

  if (summary.isLoading) return null

  if (summary.error || !summary.data) {
    return (
      <div
        data-testid="evidence-summary-strip"
        data-summary-state={summary.error ? 'failed' : 'unavailable'}
        className="flex flex-wrap items-center gap-2 rounded-md border border-graphite-200 px-3 py-1.5 text-xs dark:border-graphite-800"
      >
        <span className="font-medium">{t('common.evidence')}</span>
        <span className="text-graphite-500">{t('cockpit.evidenceSummaryUnavailable')}</span>
        <button
          type="button"
          onClick={onOpen}
          className="text-signal-deep underline-offset-2 hover:underline dark:text-signal-light"
        >
          {t('common.showEvidence')}
        </button>
        {summary.error && canRetry(summary.error) && (
          <button
            type="button"
            data-testid="evidence-summary-retry"
            onClick={() => void summary.refetch()}
            className="text-graphite-600 underline underline-offset-2 dark:text-graphite-300"
          >
            {t('common.retry')}
          </button>
        )}
      </div>
    )
  }

  const data = summary.data
  return (
    <button
      type="button"
      data-testid="evidence-summary-strip"
      data-summary-state="loaded"
      onClick={onOpen}
      className="flex flex-wrap items-center gap-2 rounded-md border border-graphite-200 px-3 py-1.5 text-xs hover:bg-graphite-50 dark:border-graphite-800 dark:hover:bg-graphite-800"
    >
      <span className="font-medium">
        {t('common.evidence')}: {data.link_count} links
      </span>
      <span className="text-graphite-500">
        {data.verified_quotes} verified · {data.documents_referenced} documents
      </span>
      {Object.entries(data.by_kind).slice(0, 3).map(([kind, count]) => (
        <Badge key={kind} tone="neutral">
          {humanise(kind)}: {count}
        </Badge>
      ))}
      <span className="text-signal-deep underline-offset-2 hover:underline dark:text-signal-light">
        {t('common.showEvidence')}
      </span>
    </button>
  )
}

export { formatDateTime }
