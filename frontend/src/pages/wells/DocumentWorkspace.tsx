/**
 * Document Workspace — the DDR → structured engineering data path.
 *
 * Upload a report, see what the extractors found, promote it into operations/events/twin state, and
 * inspect the provenance chain (page → region → extraction → record → evidence) that every promoted
 * value carries. A dry run is offered first because promotion writes to the well's operational
 * record and an engineer should be able to see what would change.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { DdrProcessingReport, DocumentRow } from '../../api/types'
import { EvidenceList, EvidencePanel } from '../../components/evidence/EvidencePanel'
import { PageHeader } from '../../components/layout/AppShell'
import {
  Async,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Json,
  Loading,
  Table,
  Tabs,
} from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, humanise } from '../../lib/format'

function ProcessingReportView({ report }: { report: DdrProcessingReport }) {
  const { t } = useI18n()
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Badge tone={report.dry_run ? 'info' : 'ok'}>{report.dry_run ? 'dry run' : 'written'}</Badge>
        <Badge tone="neutral">{report.doc_type}</Badge>
        {report.report_date && <Badge tone="neutral">report date {report.report_date}</Badge>}
        <Badge tone="ok">{report.records_promoted} promoted</Badge>
        {report.records_needing_review > 0 && (
          <Badge tone="warning">{report.records_needing_review} need review</Badge>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ['operations', report.operations_created],
          ['events', report.events_created],
          ['survey stations', report.survey_stations],
          ['NPT events', report.npt_events],
          ['NPT hours', report.npt_hours_classified],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-md border border-graphite-100 p-2 dark:border-graphite-800">
            <dt className="text-[11px] text-graphite-500 uppercase">{humanise(String(label))}</dt>
            <dd className="font-mono text-lg">{String(value)}</dd>
          </div>
        ))}
      </dl>

      {report.twin_aspects_updated.length > 0 && (
        <p className="text-xs text-graphite-600 dark:text-graphite-300">
          twin aspects updated: {report.twin_aspects_updated.join(', ')}
        </p>
      )}

      {report.warnings.length > 0 && (
        <div className="rounded-md border border-warning/40 bg-amber-50/50 p-2 dark:bg-amber-950/20">
          <p className="text-xs font-medium text-warning">{t('documents.warnings')}</p>
          <ul className="mt-1 list-disc ps-4 text-[11px]">
            {report.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {report.not_promoted.length === 0 ? (
        <p className="text-xs text-graphite-500">
          Every extracted record was admitted to the well record.
        </p>
      ) : (
        <div>
          <p className="mb-1 text-xs font-medium">{t('documents.notPromoted')}</p>
          <Table
            rows={report.not_promoted}
            rowKey={(row) => `${row.kind}-${row.target}`}
            columns={[
              { key: 'kind', header: 'Kind', render: (row) => <Badge tone="neutral">{row.kind}</Badge> },
              { key: 'target', header: 'Target', render: (row) => row.target },
              { key: 'count', header: 'Count', render: (row) => row.count, align: 'end' },
              { key: 'reason', header: 'Reason', render: (row) => row.reason ?? 'not stated' },
            ]}
          />
          <p className="mt-1 text-[11px] text-graphite-500">
            Records that were not promoted stay visible here with the reason: nothing is dropped
            silently.
          </p>
        </div>
      )}
    </div>
  )
}

export default function DocumentWorkspace() {
  const { t } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedId = searchParams.get('document')
  const [tab, setTab] = useState('overview')
  const [report, setReport] = useState<DdrProcessingReport | null>(null)
  const [docType, setDocType] = useState('ddr')
  const [message, setMessage] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const documents = useQuery({
    queryKey: ['documents', id],
    queryFn: () => drillingApi.listDocuments({ well_id: id }),
  })

  const selected = useMemo<DocumentRow | null>(
    () => documents.data?.items.find((row) => row.id === selectedId) ?? null,
    [documents.data, selectedId],
  )

  const detail = useQuery({
    queryKey: ['document', selectedId],
    queryFn: () => drillingApi.getDocument(selectedId as string),
    enabled: Boolean(selectedId),
  })
  const provenance = useQuery({
    queryKey: ['document-provenance', selectedId],
    queryFn: () => drillingApi.documentProvenance(selectedId as string),
    enabled: Boolean(selectedId) && tab === 'provenance',
  })
  const documentEvidence = useQuery({
    queryKey: ['document-evidence', selectedId],
    queryFn: () => drillingApi.listEvidence({ document_id: selectedId as string, limit: 200 }),
    enabled: Boolean(selectedId) && tab === 'evidence',
  })

  const upload = useMutation({
    mutationFn: (file: File) => drillingApi.uploadDocument(file, { well_id: id, doc_type: docType }),
    onSuccess: (result) => {
      setMessage(`Uploaded ${result.document.title} · ${result.job ? 'ingestion started' : 'no job'}`)
      setSearchParams({ document: result.document.id })
      queryClient.invalidateQueries({ queryKey: ['documents', id] })
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  const process = useMutation({
    mutationFn: ({ documentId, dryRun }: { documentId: string; dryRun: boolean }) =>
      drillingApi.processDocument(documentId, { dry_run: dryRun }),
    onSuccess: (result) => {
      setReport(result.processing)
      setTab('processing')
      queryClient.invalidateQueries({ queryKey: ['well-state', id] })
      queryClient.invalidateQueries({ queryKey: ['well-timeline', id] })
    },
  })

  const onSelect = useCallback(
    (documentId: string) => {
      setSearchParams({ document: documentId })
      setReport(null)
    },
    [setSearchParams],
  )

  const documentTabs = [
    { key: 'overview', label: 'Extraction' },
    { key: 'provenance', label: t('documents.provenance') },
    {
      key: 'evidence',
      label: t('common.evidence'),
      badge: documentEvidence.data && <Badge tone="neutral">{documentEvidence.data.total}</Badge>,
    },
    { key: 'processing', label: t('documents.processing') },
  ]

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('documents.title')}
        description="Upload a report, see what was extracted and how, then promote it into the well record."
        breadcrumb={<Link to={`/wells/${id}/cockpit`}>{t('nav.cockpit')}</Link>}
      />

      <div className="grid gap-3 lg:grid-cols-[320px_1fr]">
        <Card title={t('documents.upload')} subtitle="PDF, DOCX, XLSX, CSV or text">
          <div className="space-y-2">
            <Field label={t('documents.docType')} hint="The type selects the extractor set; it does not guarantee success.">
              <select
                value={docType}
                onChange={(event) => setDocType(event.target.value)}
                className="w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
              >
                {['ddr', 'drilling_program', 'well_program', 'end_of_well_report', 'morning_report', 'unknown'].map(
                  (value) => (
                    <option key={value} value={value}>
                      {humanise(value)}
                    </option>
                  ),
                )}
              </select>
            </Field>
            <input
              ref={fileInput}
              type="file"
              className="w-full rounded border border-graphite-300 p-1.5 text-xs dark:border-graphite-700"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) upload.mutate(file)
              }}
            />
            {upload.isPending && <Loading label="Uploading and ingesting…" />}
            {message && <p className="text-[11px] text-graphite-600 dark:text-graphite-300">{message}</p>}
          </div>

          <div className="mt-4 border-t border-graphite-100 pt-3 dark:border-graphite-800">
            <p className="mb-1 text-xs font-medium">Documents for this well</p>
            <Async query={documents} empty={(data) => (data.items.length === 0 ? <EmptyState message={t('empty.noDocuments')} /> : false)}>
              {(data) => (
                <ul className="space-y-1">
                  {data.items.map((row) => (
                    <li key={row.id}>
                      <button
                        type="button"
                        onClick={() => onSelect(row.id)}
                        className={`w-full rounded border px-2 py-1.5 text-start text-xs ${
                          row.id === selectedId
                            ? 'border-signal bg-signal/5'
                            : 'border-graphite-200 hover:bg-graphite-50 dark:border-graphite-800 dark:hover:bg-graphite-800'
                        }`}
                      >
                        <span className="block truncate font-medium">{row.title}</span>
                        <span className="block text-[10px] text-graphite-500">
                          {row.doc_type} · {row.page_count} pages · {row.extraction_summary.records} records
                        </span>
                        <span className="mt-0.5 flex flex-wrap gap-1">
                          {row.is_demo_fixture && <Badge tone="info">synthetic</Badge>}
                          {row.has_figures && <Badge tone="warning">figures present</Badge>}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Async>
          </div>
        </Card>

        <div className="space-y-3">
          {!selected && <EmptyState message="Select a document to inspect its extraction, provenance and evidence." />}

          {selected && (
            <>
              <Card
                title={selected.title}
                subtitle={`${selected.doc_type} · ${formatDateTime(selected.created_at)} · ${selected.page_count} pages`}
                actions={
                  <>
                    <Button
                      size="sm"
                      onClick={() => process.mutate({ documentId: selected.id, dryRun: true })}
                      disabled={process.isPending}
                    >
                      {t('documents.processDryRun')}
                    </Button>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => process.mutate({ documentId: selected.id, dryRun: false })}
                      disabled={process.isPending}
                    >
                      {t('documents.process')}
                    </Button>
                  </>
                }
              >
                {selected.is_demo_fixture && (
                  <p className="mb-3 rounded border border-signal/30 bg-signal/5 p-2 text-[11px]">
                    {t('documents.demoFixture')}
                  </p>
                )}
                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge tone="neutral">pages {selected.extraction_summary.pages}</Badge>
                  <Badge tone="neutral">regions {selected.extraction_summary.regions}</Badge>
                  <Badge tone="neutral">chunks {selected.extraction_summary.chunks}</Badge>
                  <Badge tone="neutral">records {selected.extraction_summary.records}</Badge>
                  <Badge tone="neutral">evidence {selected.extraction_summary.evidence_links}</Badge>
                  {Object.keys(selected.extraction_summary.extractors).length > 0 && (
                    <Badge tone="info">
                      extractors: {Object.keys(selected.extraction_summary.extractors).join(', ')}
                    </Badge>
                  )}
                  {Object.keys(selected.extraction_summary.extractors).length === 0 && (
                    <Badge tone="warning">no extractor matched this document</Badge>
                  )}
                </div>
                <div className="mt-3">
                  <a
                    href={drillingApi.documentFileUrl(selected.id)}
                    className="text-xs text-signal-deep underline-offset-2 hover:underline dark:text-signal-light"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Download the original upload
                  </a>
                </div>
              </Card>

              <Tabs tabs={documentTabs} active={tab} onChange={setTab} />

              {tab === 'overview' && (
                <Card title={t('documents.extractedRecords')}>
                  <Async query={detail}>
                    {(data) => {
                      const records = data.records ?? []
                      if (records.length === 0)
                        return (
                          <EmptyState
                            message="No structured record was extracted from this document."
                            hint={
                              Object.keys(selected.extraction_summary.extractors).length === 0
                                ? 'No extractor claimed this document type. The text is still searchable for retrieval.'
                                : 'The extractors that matched produced no rows.'
                            }
                          />
                        )
                      return (
                        <Table
                          rows={records}
                          rowKey={(row, index) => String((row as Record<string, unknown>).id ?? index)}
                          columns={[
                            {
                              key: 'kind',
                              header: 'Record',
                              render: (row) => {
                                const record = row as Record<string, unknown>
                                return (
                                  <span>
                                    <span className="font-medium">{String(record.record_kind ?? record.kind ?? 'record')}</span>
                                    <span className="block font-mono text-[10px] text-graphite-500">
                                      {String(record.extractor ?? '')} · confidence{' '}
                                      {record.confidence === null || record.confidence === undefined
                                        ? '—'
                                        : Number(record.confidence).toFixed(2)}
                                    </span>
                                  </span>
                                )
                              },
                            },
                            {
                              key: 'payload',
                              header: 'Content',
                              render: (row) => <Json value={(row as Record<string, unknown>).payload} max={80} />,
                            },
                            {
                              key: 'page',
                              header: t('common.page'),
                              render: (row) => String((row as Record<string, unknown>).page_number ?? '—'),
                              align: 'end',
                            },
                          ]}
                        />
                      )
                    }}
                  </Async>
                </Card>
              )}

              {tab === 'provenance' && (
                <Card title={t('documents.provenance')} subtitle="region → extraction → record">
                  <Async query={provenance}>
                    {(data) =>
                      data.record_count === 0 ? (
                        <EmptyState message="Nothing was extracted, so there is no provenance chain." />
                      ) : (
                        <ul className="space-y-2">
                          {data.chain.map((link, index) => (
                            <li
                              key={index}
                              className="rounded-md border border-graphite-100 p-2.5 text-xs dark:border-graphite-800"
                            >
                              <div className="flex flex-wrap gap-2">
                                <Badge tone="info">
                                  {String((link.record as Record<string, unknown>).record_kind ?? 'record')}
                                </Badge>
                                {link.region && (
                                  <Badge tone="neutral">
                                    page {String((link.region as Record<string, unknown>).page_number ?? '—')} ·{' '}
                                    {String((link.region as Record<string, unknown>).region_kind ?? 'region')}
                                  </Badge>
                                )}
                                {!link.region && <Badge tone="warning">no region captured</Badge>}
                              </div>
                              <Json value={link.record} max={60} />
                            </li>
                          ))}
                        </ul>
                      )
                    }
                  </Async>
                </Card>
              )}

              {tab === 'evidence' && (
                <Card title={t('common.evidence')} subtitle="what was quoted, from where, by which method">
                  <Async query={documentEvidence}>
                    {(data) => (
                      <EvidenceList
                        citations={data.items.map((item) => ({
                          evidence_id: item.id,
                          document_id: item.document_id,
                          page: item.page_number,
                          region: typeof item.locator?.region === 'string' ? item.locator.region : null,
                          excerpt: item.excerpt,
                          method: item.method,
                          confidence: item.confidence,
                          quote_verified: item.quote_verified,
                          locator: item.locator,
                        }))}
                      />
                    )}
                  </Async>
                </Card>
              )}

              {tab === 'processing' && (
                <Card title={t('documents.processing')}>
                  {process.isPending && <Loading label="Extracting and promoting…" />}
                  {process.error && (
                    <p className="text-sm text-danger">
                      {process.error instanceof Error ? process.error.message : String(process.error)}
                    </p>
                  )}
                  {!report && !process.isPending && (
                    <EmptyState
                      message="Run a dry run or a real promotion to see the plan."
                      hint="A dry run computes exactly the same plan and writes nothing."
                    />
                  )}
                  {report && <ProcessingReportView report={report} />}
                </Card>
              )}
            </>
          )}
        </div>
      </div>

      <EvidencePanel
        open={false}
        onClose={() => undefined}
        documentId={selectedId ?? undefined}
        wellId={id}
      />
    </div>
  )
}
