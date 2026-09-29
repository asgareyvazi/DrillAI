/**
 * Engineering Workspace.
 *
 * The engine catalogue comes from `/registry/engines` — nothing here hard-codes an engine name,
 * input, output or validation status. Running an engine shows the result *with* its assumptions,
 * limitations, violations, hashes and declared validation status, and each output value can open
 * the evidence panel. The dependency graph and the change-impact report come from the platform's
 * own port-level graph, so "what goes stale when this changes" is an answer the backend owns.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { EngineListItem, EngineRunEnvelope, ImpactReport } from '../../api/types'
import { EvidencePanel } from '../../components/evidence/EvidencePanel'
import { PageHeader } from '../../components/layout/AppShell'
import {
  Async,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Json,
  Loading,
  Table,
  Tabs,
  Value,
} from '../../components/common'
import { useI18n } from '../../i18n'
import { formatDateTime, formatValidationStatus } from '../../lib/format'
import { useSession } from '../../stores/session'

const OILFIELD_SUFFIX: Record<string, string> = {
  _si: '',
  _m_s: 'm/s',
  _kpa: 'kPa',
}

function statusTone(status: string): 'ok' | 'warning' | 'danger' | 'neutral' {
  if (status === 'verified_against_reference') return 'ok'
  if (status === 'formula_derived') return 'warning'
  if (status === 'needs_field_validation') return 'danger'
  return 'neutral'
}

/** Renders the outputs of an engine run without assuming which keys an engine produces. */
function OutputTable({ outputs, onEvidence }: { outputs: Record<string, unknown>; onEvidence: () => void }) {
  const { unitSystem } = useSession()
  const { t } = useI18n()
  const rows = Object.entries(outputs).filter(([, value]) => typeof value === 'number' || typeof value === 'string')
  const structural = Object.entries(outputs).filter(([, value]) => typeof value !== 'number' && typeof value !== 'string')

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {rows.map(([key, value]) => (
          <Value
            key={key}
            label={key}
            value={value}
            unit={key.endsWith('_si') ? 'Pa' : key.includes('velocity') ? 'm/s' : null}
            unitSystem={unitSystem}
            source={`engine output · ${key}`}
            onEvidence={onEvidence}
          />
        ))}
      </div>
      {rows.length === 0 && <EmptyState message="This engine returned no scalar output." />}
      {structural.length > 0 && (
        <div className="space-y-2">
          {structural.map(([key, value]) => (
            <details key={key} className="rounded border border-graphite-100 p-2 dark:border-graphite-800">
              <summary className="cursor-pointer text-xs font-medium">{key}</summary>
              <Json value={value} max={120} />
            </details>
          ))}
        </div>
      )}
      <p className="text-[11px] text-graphite-500">
        {t('common.units') ?? 'Values are canonical SI as stored; the display unit switch only changes presentation.'}{' '}
        {Object.keys(OILFIELD_SUFFIX).length > 0 ? '' : ''}
      </p>
    </div>
  )
}

/**
 * Why a run's context may be incomplete.
 *
 * The wellbore and section are read to give an engine run its *scope*, and until now a failure in that
 * read was invisible: the runner simply sent the request without them. The engine then computed a real
 * number at a different scope from the one the page appeared to be working at, and nothing on screen
 * said so. That is the failure mode this type exists to prevent — the run is still allowed (a
 * well-scoped run is a legitimate thing to ask for), but not silently.
 */
type ScopeFailure = {
  /** Which read failed, so the sentence names the context the run will not carry. */
  what: 'wellbore' | 'section'
  error: unknown
  retry: () => void
}

function EngineRunner({ engine, wellId, wellboreId, sectionId, scope }: {
  engine: EngineListItem
  wellId: string
  wellboreId?: string
  sectionId?: string
  scope?: ScopeFailure | null
}) {
  const { t } = useI18n()
  const [inputs, setInputs] = useState<Record<string, unknown>>({})
  const [raw, setRaw] = useState('{\n  \n}')
  const [parseError, setParseError] = useState<string | null>(null)
  const [result, setResult] = useState<EngineRunEnvelope | null>(null)
  const [showEvidence, setShowEvidence] = useState(false)

  const required = useMemo(() => {
    const schema = engine.input_schema as { required?: string[] }
    return schema.required ?? []
  }, [engine.input_schema])

  const run = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      drillingApi.runEngine(engine.key, {
        inputs: payload,
        well_id: wellId,
        wellbore_id: wellboreId,
        section_id: sectionId,
      }),
    onSuccess: (data) => setResult(data),
  })

  function submit() {
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>
      setParseError(null)
      setInputs(parsed)
      run.mutate(parsed)
    } catch (error) {
      setParseError(error instanceof Error ? error.message : 'invalid JSON')
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={statusTone(engine.validation_status)}>
          {formatValidationStatus(engine.validation_status)}
        </Badge>
        <Badge tone="neutral">v{engine.version}</Badge>
        <Badge tone="neutral">{engine.category}</Badge>
        {engine.deterministic ? <Badge tone="ok">deterministic</Badge> : <Badge tone="warning">non-deterministic</Badge>}
        <Badge tone="neutral">{engine.action_level}</Badge>
        {engine.requires_well_context && <Badge tone="info">requires well context</Badge>}
      </div>

      <p className="text-xs text-graphite-600 dark:text-graphite-300">{engine.summary}</p>

      <div className="grid gap-3 lg:grid-cols-2">
        <Field
          label={t('engineering.inputs')}
          hint={`JSON matching the engine input schema. Required: ${required.join(', ') || 'none declared'}`}
        >
          <textarea
            value={raw}
            onChange={(event) => setRaw(event.target.value)}
            spellCheck={false}
            rows={14}
            className="w-full rounded border border-graphite-300 bg-graphite-50 p-2 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-950"
          />
        </Field>
        <div className="space-y-2">
          <p className="text-xs font-medium">Declared input ports</p>
          <div className="flex flex-wrap gap-1">
            {engine.consumes.map((port) => (
              <Badge key={port} tone="neutral">
                {port}
              </Badge>
            ))}
            {engine.consumes.length === 0 && <span className="text-[11px] text-graphite-500">none declared</span>}
          </div>
          <p className="text-xs font-medium">Declared outputs</p>
          <div className="flex flex-wrap gap-1">
            {engine.produces.map((port) => (
              <Badge key={port} tone="info">
                {port}
              </Badge>
            ))}
          </div>
          {engine.assumptions.length > 0 && (
            <>
              <p className="text-xs font-medium">{t('common.assumptions')}</p>
              <ul className="list-disc ps-4 text-[11px] text-graphite-600 dark:text-graphite-300">
                {engine.assumptions.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {engine.limitations.length > 0 && (
            <>
              <p className="text-xs font-medium">{t('common.limitations')}</p>
              <ul className="list-disc ps-4 text-[11px] text-graphite-600 dark:text-graphite-300">
                {engine.limitations.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {engine.references.length > 0 && (
            <p className="text-[11px] text-graphite-500">references: {engine.references.join('; ')}</p>
          )}
        </div>
      </div>

      {/*
        The scope this run will actually be sent with, stated when it is not the scope the page meant
        to use. Shown above the button so it is read before the run, not after.
      */}
      {scope && (
        <div data-testid="engine-scope-failure" className="space-y-1">
          <ErrorState error={scope.error} onRetry={scope.retry} />
          <p className="text-xs text-graphite-600 dark:text-graphite-300">
            {scope.what === 'wellbore' ? t('engineering.scopeUnavailable') : t('engineering.scopeSectionUnavailable')}
          </p>
        </div>
      )}

      {parseError && <p className="text-xs text-danger">Invalid JSON: {parseError}</p>}
      {run.error && <ErrorState error={run.error} />}

      <Button variant="primary" onClick={submit} disabled={run.isPending}>
        {t('engineering.runEngine')}
      </Button>
      {run.isPending && <Loading />}

      {result && (
        <Card
          title={t('engineering.result')}
          subtitle={`${result.engine.key}@${result.engine.version} · inputs ${result.inputs_hash.slice(0, 10)}… → outputs ${result.outputs_hash.slice(0, 10)}…`}
          actions={
            <>
              <Badge tone={result.is_feasible ? 'ok' : 'danger'}>
                {result.is_feasible ? t('engineering.feasible') : t('engineering.infeasible')}
              </Badge>
              <Button size="sm" variant="ghost" onClick={() => setShowEvidence(true)}>
                {t('common.showEvidence')}
              </Button>
            </>
          }
        >
          <div className="space-y-3">
            <OutputTable outputs={result.outputs} onEvidence={() => setShowEvidence(true)} />

            {result.constraint_violations.length > 0 && (
              <div className="rounded border border-danger/40 bg-red-50/60 p-2 dark:bg-red-950/20">
                <p className="text-xs font-medium text-danger">{t('engineering.violations')}</p>
                <Json value={result.constraint_violations} max={80} />
              </div>
            )}
            {result.warnings.length > 0 && (
              <ul className="list-disc ps-4 text-[11px] text-warning">
                {result.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            )}
            <p className="text-[11px] text-graphite-500">
              validation status: {formatValidationStatus(result.engine.validation_status)} · assumptions and
              limitations are the engine's own declarations, not commentary
            </p>
            <Json value={{ inputs }} max={40} />
          </div>
        </Card>
      )}

      <EvidencePanel
        open={showEvidence}
        onClose={() => setShowEvidence(false)}
        subjectKind="engine_run"
        subjectId={result?.engine_run_id}
        wellId={wellId}
        title={`${t('common.evidence')} · ${engine.key}`}
      />
    </div>
  )
}

function ImpactPanel({ wellId }: { wellId: string }) {
  const { t } = useI18n()
  const graph = useQuery({ queryKey: ['dependency-graph'], queryFn: ({ signal }) => drillingApi.dependencies(signal) })
  const [selected, setSelected] = useState<string[]>([])
  const [impact, setImpact] = useState<ImpactReport | null>(null)

  const check = useMutation({
    mutationFn: (ports: string[]) => drillingApi.impact(wellId, ports),
    onSuccess: (data) => setImpact(data.impact),
  })

  return (
    <div className="space-y-3">
      <p className="text-xs text-graphite-500">{t('engineering.impactHint')}</p>
      <Async query={graph}>
        {(data) => (
          <>
            <div className="flex flex-wrap gap-1">
              {data.graph.ports.map((port) => (
                <button
                  key={port}
                  type="button"
                  onClick={() =>
                    setSelected((current) =>
                      current.includes(port) ? current.filter((item) => item !== port) : [...current, port],
                    )
                  }
                  className={`rounded px-1.5 py-0.5 text-[11px] ${
                    selected.includes(port)
                      ? 'bg-signal text-white'
                      : 'bg-graphite-100 text-graphite-700 hover:bg-graphite-200 dark:bg-graphite-800 dark:text-graphite-200'
                  }`}
                >
                  {port}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="primary"
                disabled={selected.length === 0 || check.isPending}
                onClick={() => check.mutate(selected)}
              >
                {t('engineering.impact')}
              </Button>
              <span className="text-[11px] text-graphite-500">
                {data.graph.nodes.length} nodes · {data.graph.edges.length} edges · derived from{' '}
                {data.graph.generated_from}
              </span>
            </div>
          </>
        )}
      </Async>

      {check.error && <ErrorState error={check.error} />}

      {impact && (
        <div className="space-y-2">
          {impact.unknown_ports.length > 0 && (
            <p className="text-xs text-warning">
              Unknown ports (not declared by any engine): {impact.unknown_ports.join(', ')}
            </p>
          )}
          <Table
            rows={impact.affected}
            rowKey={(row) => row.engine_key}
            empty={<EmptyState message="No engine declares a dependency on the selected inputs." />}
            columns={[
              {
                key: 'engine',
                header: t('common.engine'),
                render: (row) => (
                  <span>
                    <span className="font-medium">{row.engine_name}</span>
                    <span className="block font-mono text-[10px] text-graphite-500">
                      {row.engine_key}@{row.version}
                    </span>
                  </span>
                ),
              },
              {
                key: 'stale',
                header: t('engineering.staleResults'),
                render: (row) => (
                  <Badge tone={row.stale ? 'danger' : row.ran_before ? 'ok' : 'neutral'}>
                    {row.stale ? 'stale' : row.ran_before ? 'current' : 'never run'}
                  </Badge>
                ),
              },
              { key: 'reason', header: 'Reason', render: (row) => row.reason },
              {
                key: 'last',
                header: 'Last run',
                render: (row) => formatDateTime(row.last_run_at),
              },
            ]}
          />
          {impact.recommendations_stale && (
            <p className="text-xs text-warning">
              Recommendations built on these results are flagged stale and must be re-derived before use.
            </p>
          )}
          {impact.notes.length > 0 && (
            <ul className="list-disc ps-4 text-[11px] text-graphite-500">
              {impact.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

export default function EngineeringWorkspace() {
  const { t } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const [tab, setTab] = useState('catalogue')
  const [selectedEngine, setSelectedEngine] = useState<string | null>(null)
  const [category, setCategory] = useState<string>('all')

  const queryClient = useQueryClient()

  /**
   * Re-read the scope after a failure.
   *
   * Invalidation rather than `refetch()`, because the two reads are chained: the section list is only
   * asked for once a wellbore is known. Invalidating both marks the section query stale and lets it
   * run once with the wellbore that comes back, instead of firing it now with nothing to ask about and
   * again a moment later.
   */
  const readScopeAgain = (includeSections: boolean) => {
    void queryClient.invalidateQueries({ queryKey: ['wellbores', id] })
    if (includeSections) void queryClient.invalidateQueries({ queryKey: ['sections'] })
  }

  const engines = useQuery({ queryKey: ['engines'], queryFn: ({ signal }) => drillingApi.listEngines(signal) })
  const wellbores = useQuery({ queryKey: ['wellbores', id], queryFn: ({ signal }) => drillingApi.listWellbores(id, signal) })
  const sections = useQuery({
    queryKey: ['sections', wellbores.data?.items[0]?.id],
    queryFn: ({ signal }) => drillingApi.listSections(wellbores.data?.items[0]?.id as string, signal),
    enabled: Boolean(wellbores.data?.items[0]?.id),
  })
  const runs = useQuery({ queryKey: ['well-engine-runs', id], queryFn: ({ signal }) => drillingApi.wellEngineRuns(id, { limit: 50 }, signal) })

  /**
   * Set when the wellbore or section read failed, in that order of severity.
   *
   * The two are not equivalent: without the wellbore there is no section either, because the section
   * list is only asked for once a wellbore is known. Reporting the first failure is therefore the
   * truthful thing to do, and the retry re-reads both so one press can restore the full scope.
   */
  // Not memoised on purpose: it is a description of the current query states, read on every render,
  // and holding it in a memo would only add a dependency list that has to stay true.
  const scopeFailure: ScopeFailure | null = wellbores.error
    ? { what: 'wellbore', error: wellbores.error, retry: () => readScopeAgain(true) }
    : sections.error
      ? { what: 'section', error: sections.error, retry: () => readScopeAgain(false) }
      : null

  const engine = useMemo<EngineListItem | null>(
    () => engines.data?.items.find((item) => item.key === selectedEngine) ?? null,
    [engines.data, selectedEngine],
  )

  const categories = useMemo(() => {
    const set = new Set((engines.data?.items ?? []).map((item) => item.category))
    return ['all', ...Array.from(set).sort()]
  }, [engines.data])

  const filtered = useMemo(
    () => (engines.data?.items ?? []).filter((item) => category === 'all' || item.category === category),
    [engines.data, category],
  )

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('engineering.title')}
        description="Engines are listed as the registry declares them. Calculations show their inputs, assumptions, limitations and validation status."
        breadcrumb={<Link to={`/wells/${id}/cockpit`}>{t('nav.cockpit')}</Link>}
        actions={
          <Link to={`/wells/${id}/optimisation`}>
            <Button variant="primary" size="sm">
              {t('nav.optimisation')}
            </Button>
          </Link>
        }
      />

      <Tabs
        tabs={[
          { key: 'catalogue', label: t('engineering.catalogue'), badge: engines.data && <Badge tone="neutral">{engines.data.total}</Badge> },
          { key: 'impact', label: t('engineering.impact') },
          { key: 'runs', label: t('audit.engineRuns'), badge: runs.data && <Badge tone="neutral">{runs.data.total}</Badge> },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'catalogue' && (
        <div className="grid gap-3 lg:grid-cols-[300px_1fr]">
          <Card title={t('engineering.catalogue')} dense>
            <label className="mb-2 block px-1">
              <span className="mb-1 block text-[11px] text-graphite-500">{t('common.filter')}</span>
              <select
                value={category}
                onChange={(event) => setCategory(event.target.value)}
                className="w-full rounded border border-graphite-300 bg-white px-1.5 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-900"
              >
                {categories.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <Async query={engines} empty={(data) => (data.items.length === 0 ? <EmptyState message="The engine registry is empty." /> : false)}>
              {() => (
                <ul className="max-h-[60vh] space-y-0.5 overflow-auto">
                  {filtered.map((item) => (
                    <li key={item.key}>
                      <button
                        type="button"
                        onClick={() => setSelectedEngine(item.key)}
                        className={`w-full rounded px-2 py-1.5 text-start text-xs ${
                          item.key === selectedEngine
                            ? 'bg-signal/10 font-medium text-signal-deep dark:text-signal-light'
                            : 'hover:bg-graphite-100 dark:hover:bg-graphite-800'
                        }`}
                      >
                        <span className="block truncate">{item.name}</span>
                        <span className="block font-mono text-[10px] text-graphite-500">{item.key}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Async>
          </Card>

          <div className="space-y-3">
            {!engine && (
              <EmptyState
                message="Select an engine to inspect its contract and run it."
                hint="Nothing on this page is hard-coded: the catalogue is /registry/engines."
              />
            )}
            {engine && (
              <Card
                title={engine.name}
                subtitle={`${engine.key} · ${engine.domain_pack} domain pack`}
                actions={<Badge tone="neutral">{engine.action_level}</Badge>}
              >
                <EngineRunner
                  engine={engine}
                  wellId={id}
                  wellboreId={wellbores.data?.items[0]?.id}
                  sectionId={sections.data?.items.at(-1)?.id}
                  scope={scopeFailure}
                />
              </Card>
            )}
          </div>
        </div>
      )}

      {tab === 'impact' && (
        <Card title={t('engineering.dependencies')} subtitle="port-level graph built from engine declarations">
          <ImpactPanel wellId={id} />
        </Card>
      )}

      {tab === 'runs' && (
        <Card title={t('audit.engineRuns')} subtitle="every run keeps its inputs, outputs and hashes">
          <Async query={runs} empty={(data) => (data.items.length === 0 ? <EmptyState message={t('empty.noEngineRuns')} /> : false)}>
            {(data) => (
              <Table
                rows={data.items}
                rowKey={(row) => row.id}
                columns={[
                  {
                    key: 'engine',
                    header: t('common.engine'),
                    render: (row) => (
                      <span className="font-mono text-xs">
                        {row.engine_key}@{row.engine_version}
                      </span>
                    ),
                  },
                  {
                    key: 'status',
                    header: t('common.status'),
                    render: (row) => (
                      <Badge tone={row.status === 'succeeded' ? 'ok' : 'warning'}>{row.status}</Badge>
                    ),
                  },
                  {
                    key: 'feasible',
                    header: 'Feasible',
                    render: (row) =>
                      row.is_feasible === null ? (
                        '—'
                      ) : (
                        <Badge tone={row.is_feasible ? 'ok' : 'danger'}>{row.is_feasible ? 'yes' : 'no'}</Badge>
                      ),
                  },
                  { key: 'violations', header: 'Violations', render: (row) => row.constraint_violations.length, align: 'end' },
                  { key: 'trigger', header: t('audit.triggeredBy'), render: (row) => row.triggered_by },
                  { key: 'at', header: 'When', render: (row) => formatDateTime(row.created_at) },
                  {
                    key: 'hash',
                    header: t('audit.inputsHash'),
                    render: (row) => <span className="font-mono text-[10px]">{row.inputs_hash.slice(0, 10)}…</span>,
                  },
                ]}
              />
            )}
          </Async>
        </Card>
      )}
    </div>
  )
}
