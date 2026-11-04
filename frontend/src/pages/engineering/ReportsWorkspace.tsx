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
import type { EngineValueUnitContract, Report, ReportSection } from '../../api/types'
import { UnitAwareResult } from '../../components/engineering/UnitAwareResult'
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function unitFields(contract: unknown, key: string): Record<string, EngineValueUnitContract> {
  if (!isRecord(contract) || !isRecord(contract[key])) return {}
  return contract[key] as Record<string, EngineValueUnitContract>
}

function stripUnitContractMetadata(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripUnitContractMetadata)
  if (!isRecord(value)) return value
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== 'unit_contract')
      .map(([key, child]) => [key, stripUnitContractMetadata(child)]),
  )
}

function SectionBody({ section }: { section: ReportSection }) {
  const { t } = useI18n()
  if (section.items.length === 0) {
    return (
      <EmptyState
        message={section.note ?? t('reports.emptySection')}
        hint={section.note ? undefined : t('reports.emptySectionHint')}
      />
    )
  }
  if (section.kind === 'calculations') {
    return (
      <div className="space-y-3">
        {section.note ? <p className="text-xs text-graphite-500">{section.note}</p> : null}
        {section.items.map((item, index) => {
          const contract = item.units
          const violations = Array.isArray(item.constraint_violations) ? item.constraint_violations : []
          return (
            <details key={`${section.key}-${index}`} className="rounded border border-graphite-100 p-3 dark:border-graphite-800">
              <summary className="cursor-pointer text-xs font-medium">
                {String(item.engine_key ?? t('common.engine'))}@{String(item.engine_version ?? '—')} · {String(item.status ?? '—')}
              </summary>
              <div className="mt-3 space-y-3">
                <div>
                  <h4 className="mb-1 text-xs font-medium">{t('engineering.inputs')}</h4>
                  <UnitAwareResult value={item.inputs ?? {}} unitFields={unitFields(contract, 'input_fields')} />
                </div>
                <div>
                  <h4 className="mb-1 text-xs font-medium">{t('engineering.outputs')}</h4>
                  <UnitAwareResult value={item.outputs ?? {}} unitFields={unitFields(contract, 'output_fields')} />
                </div>
                {violations.length > 0 && (
                  <div>
                    <h4 className="mb-1 text-xs font-medium text-danger">{t('engineering.violations')}</h4>
                    <UnitAwareResult
                      value={{ violations }}
                      unitFields={unitFields(contract, 'violation_fields')}
                    />
                  </div>
                )}
                <p className="break-all font-mono text-[10px] text-graphite-500">
                  inputs {String(item.inputs_hash ?? '—')} · outputs {String(item.outputs_hash ?? '—')}
                </p>
              </div>
            </details>
          )
        })}
      </div>
    )
  }
  const keys = Array.from(new Set(section.items.flatMap((item) => Object.keys(item)))).slice(0, 8)
  return (
    <div className="space-y-3">
      {section.note ? <p className="text-xs text-graphite-500">{section.note}</p> : null}
      <Table
        rows={section.items}
        rowKey={(_row, index: number) => `${section.key}-${index}`}
        columns={keys.map((key) => ({
          key,
          header: humanise(key),
          render: (row: Record<string, unknown>) => {
            const value = row[key]
            if (value === null || value === undefined) return '—'
            if (key === 'parameters' && (Array.isArray(value) || isRecord(value))) {
              return (
                <UnitAwareResult
                  value={stripUnitContractMetadata(value)}
                  unitFields={unitFields(row.units, 'parameter_fields')}
                />
              )
            }
            if (typeof value === 'object') return <Json value={value} max={30} />
            return String(value)
          },
        }))}
      />
    </div>
  )
}

function downloadReportJson(blob: Blob, kind: string, wellId: string): void {
  const safeKind = kind.replace(/[^a-zA-Z0-9_-]/g, '_')
  const safeWellId = wellId.replace(/[^a-zA-Z0-9_-]/g, '_')
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `drillai-${safeKind}-${safeWellId}.json`
  link.style.display = 'none'
  document.body.append(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
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
  const exportJson = useMutation({
    mutationFn: (reportKind: string) => drillingApi.exportReport(id, reportKind),
    onSuccess: (blob, reportKind) => downloadReportJson(blob, reportKind, id),
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
                <Button
                  onClick={() => exportJson.mutate(report.kind)}
                  disabled={exportJson.isPending}
                  data-testid="report-export-json"
                >
                  {exportJson.isPending ? t('reports.exporting') : t('reports.exportJson')}
                </Button>
              </>
            }
          >
            <div className="flex flex-wrap gap-2">
              {report.sections.map((section) => (
                <Badge key={section.key} tone={SECTION_TONE[section.kind]}>
                  {humanise(section.kind)}: {section.count}
                </Badge>
              ))}
            </div>
            {exportJson.error ? (
              <ErrorState error={exportJson.error} onRetry={() => exportJson.mutate(report.kind)} />
            ) : null}
            <p className="mt-3 text-xs text-graphite-500">{report.rendering_note}</p>
            <details className="mt-3 rounded border border-graphite-200 p-3 text-xs dark:border-graphite-800">
              <summary className="cursor-pointer font-medium">{t('reports.metadata')}</summary>
              <div className="mt-2"><Json value={report.metadata} max={80} /></div>
            </details>
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
