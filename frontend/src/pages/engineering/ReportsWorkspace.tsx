/**
 * Reports workspace.
 *
 * A report here is a structured payload that keeps facts, calculations, evidence, recommendations
 * and assumptions in separate sections, because a report that flattens them is the mechanism by
 * which invented numbers reach a decision. Document rendering (PDF/DOCX) is deliberately not
 * implemented and the page says so rather than offering a button that does nothing.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { Report, ReportSection } from '../../api/types'
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
  Table,
} from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, humanise } from '../../lib/format'

const SECTION_TONE: Record<ReportSection['kind'], 'ok' | 'info' | 'neutral' | 'warning'> = {
  facts: 'ok',
  calculations: 'info',
  evidence: 'neutral',
  recommendations: 'info',
  assumptions: 'warning',
}

function SectionBody({ section }: { section: ReportSection }) {
  const { t } = useI18n()
  if (section.items.length === 0) {
    return (
      <EmptyState
        message={section.empty_reason ?? t('reports.emptySection')}
        hint={section.empty_reason ? undefined : 'Nothing was recorded or computed for this section.'}
      />
    )
  }
  const keys = Array.from(new Set(section.items.flatMap((item) => Object.keys(item)))).slice(0, 8)
  return (
    <Table
      rows={section.items}
      rowKey={(_row, index: number) => `${section.key}-${index}`}
      columns={keys.map((key) => ({
        key,
        header: humanise(key),
        render: (row: Record<string, unknown>) => {
          const value = row[key]
          if (value === null || value === undefined) return '—'
          if (typeof value === 'object') return <Json value={value} max={30} />
          return String(value)
        },
      }))}
    />
  )
}

export default function ReportsWorkspace() {
  const { t } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const [kind, setKind] = useState('daily_drilling')
  const [report, setReport] = useState<Report | null>(null)

  const kinds = useQuery({ queryKey: ['report-kinds'], queryFn: ({ signal }) => drillingApi.reportKinds(signal) })
  const build = useMutation({
    mutationFn: (reportKind: string) => drillingApi.buildReport(id, reportKind),
    onSuccess: (data) => setReport(data.report),
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('reports.title')}
        description="Structured report payloads: facts, calculations, evidence, recommendations and assumptions are kept apart so a reader can tell them apart."
        breadcrumb={<Link to={`/wells/${id}/cockpit`}>{t('nav.cockpit')}</Link>}
      />

      <Card title={t('reports.kind')}>
        <Async query={kinds}>
          {(data) => (
            <div className="space-y-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="min-w-[260px] flex-1">
                  <span className="mb-1 block text-xs font-medium text-graphite-600 dark:text-graphite-300">
                    {t('reports.kind')}
                  </span>
                  <select
                    value={kind}
                    onChange={(event) => setKind(event.target.value)}
                    className="w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
                  >
                    {data.kinds.map((item) => (
                      <option key={item.key} value={item.key}>
                        {item.key} — {item.description}
                      </option>
                    ))}
                  </select>
                </label>
                <Button variant="primary" onClick={() => build.mutate(kind)} disabled={build.isPending}>
                  {t('reports.build')}
                </Button>
              </div>
              <p className="text-[11px] text-graphite-500">{data.note}</p>
              {build.isPending && <Loading />}
              {build.error && <ErrorState error={build.error} onRetry={() => build.mutate(kind)} />}
            </div>
          )}
        </Async>
      </Card>

      {report && (
        <>
          <Card
            title={report.title}
            subtitle={`${report.kind} · generated ${formatDateTime(report.generated_at)}`}
            actions={
              <>
                {report.period_from && <Badge tone="neutral">from {formatDateTime(report.period_from)}</Badge>}
                {report.period_to && <Badge tone="neutral">to {formatDateTime(report.period_to)}</Badge>}
                <Badge tone="neutral">{report.sections.length} sections</Badge>
              </>
            }
          >
            <div className="flex flex-wrap gap-2">
              {report.sections.map((section) => (
                <Badge key={section.key} tone={SECTION_TONE[section.kind]}>
                  {humanise(section.kind)}: {section.items.length}
                </Badge>
              ))}
            </div>
            {report.limitations.length > 0 && (
              <ul className="mt-3 list-disc ps-4 text-[11px] text-warning">
                {report.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            )}
            {report.sources.length > 0 && (
              <p className="mt-2 text-[11px] text-graphite-500">sources: {report.sources.join(', ')}</p>
            )}
          </Card>

          {report.sections.map((section) => (
            <Card
              key={section.key}
              title={section.title}
              actions={<Badge tone={SECTION_TONE[section.kind]}>{humanise(section.kind)}</Badge>}
            >
              <SectionBody section={section} />
            </Card>
          ))}
        </>
      )}

      {!report && !build.isPending && (
        <EmptyState
          message="Choose a report kind and build it."
          hint="Document rendering to PDF/DOCX is not implemented in this deployment: the payload is the deliverable."
        />
      )}
    </div>
  )
}
