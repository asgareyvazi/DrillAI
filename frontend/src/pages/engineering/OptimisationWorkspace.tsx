/**
 * Drilling Parameter Optimisation workspace.
 *
 * The product rule is that a recommendation is never a single unexplained number. This workspace
 * therefore always shows, together: the decision variables, the objectives the *engines* compute,
 * the constraints that were applied, the Pareto frontier, the rejected candidates with the reason
 * each was rejected, the trade-offs, and the objectives the platform refuses to evaluate because no
 * model exists.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { drillingApi, type OptimisePayload } from '../../api/endpoints'
import type { CandidateView, OptimisationExplanation, WhyNotEntry } from '../../api/types'
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
import { formatDateTime, formatNumber, humanise } from '../../lib/format'
import { useSession } from '../../stores/session'

const DEFAULT_PARAMETERS = `[
  { "name": "flow_rate_si", "min_si": 0.025, "max_si": 0.045, "steps": 5, "unit": "m3/s" },
  { "name": "mud_weight_si", "min_si": 1220.0, "max_si": 1300.0, "steps": 3, "unit": "kg/m3" }
]`

const DEFAULT_HYDRAULICS = `{
  "elements": [
    { "kind": "drillpipe", "name": "5in drillpipe", "from_depth_si": 0.0, "to_depth_si": 2400.0, "od_si": 0.127, "id_si": 0.1086 },
    { "kind": "hole", "name": "8-1/2 in hole", "from_depth_si": 0.0, "to_depth_si": 2400.0, "od_si": 0.2159 }
  ],
  "bit_depth_si": 2400.0,
  "flow_rate_si": 0.035,
  "mud_weight_si": 1240.0,
  "plastic_viscosity_si": 0.018,
  "yield_point_si": 8.0,
  "bit_diameter_si": 0.2159,
  "tfa_si": 0.00045,
  "nozzle_count": 3,
  "pore_pressure_gradient_si": 12500.0,
  "fracture_gradient_si": 15500.0
}`

const DEFAULT_LIMITS = `{ "spp_si": 25000000 }`

function toneFor(kind: string): 'ok' | 'danger' | 'warning' | 'info' | 'neutral' {
  switch (kind) {
    case 'selected':
      return 'ok'
    case 'infeasible':
      return 'danger'
    case 'dominated':
      return 'warning'
    case 'trade_off':
      return 'info'
    default:
      return 'neutral'
  }
}

function WhyNotList({ entries }: { entries: WhyNotEntry[] }) {
  const { t, locale } = useI18n()
  if (entries.length === 0) return <EmptyState message="There were no alternative candidates to explain." />
  return (
    <ul className="space-y-2">
      {entries.map((entry) => (
        <li key={entry.candidate_id} className="rounded-md border border-graphite-100 p-3 dark:border-graphite-800">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={toneFor(entry.kind)}>{humanise(entry.kind)}</Badge>
            <span className="font-mono text-xs">{entry.candidate}</span>
            <span className="text-xs text-graphite-600 dark:text-graphite-300">{entry.reason}</span>
          </div>
          {Object.keys(entry.comparison).length > 0 && (
            <table className="mt-2 w-full text-[11px]">
              <thead>
                <tr className="text-graphite-500">
                  <th className="text-start font-medium">{t('optimisation.objectives')}</th>
                  <th className="text-end font-medium">{t('optimisation.candidate')}</th>
                  <th className="text-end font-medium">{t('optimisation.recommended')}</th>
                  <th className="text-end font-medium">{entry.comparison[Object.keys(entry.comparison)[0] as string]?.sense}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(entry.comparison).map(([objective, comparison]) => (
                  <tr key={objective}>
                    <td className="py-0.5">{objective}</td>
                    <td className="py-0.5 text-end font-mono">{formatNumber(comparison.candidate, locale, 4)}</td>
                    <td className="py-0.5 text-end font-mono">{formatNumber(comparison.recommended, locale, 4)}</td>
                    <td className="py-0.5 text-end">
                      {comparison.candidate !== null && comparison.recommended !== null && (
                        <Badge
                          tone={
                            (comparison.sense === 'maximize'
                              ? comparison.candidate > comparison.recommended
                              : comparison.candidate < comparison.recommended)
                              ? 'ok'
                              : 'neutral'
                          }
                        >
                          {comparison.sense === 'maximize'
                            ? comparison.candidate > comparison.recommended
                              ? 'better'
                              : 'worse'
                            : comparison.candidate < comparison.recommended
                              ? 'better'
                              : 'worse'}
                        </Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {entry.violations && entry.violations.length > 0 && <Json value={entry.violations} max={60} />}
        </li>
      ))}
    </ul>
  )
}

function CandidateTable({
  rows,
  onSelect,
  selected,
}: {
  rows: CandidateView[]
  onSelect: (candidate: CandidateView) => void
  selected: CandidateView | null
}) {
  const { t, locale } = useI18n()
  if (rows.length === 0) return <EmptyState message="No candidate reached a rankable state." />
  const objectiveKeys = Array.from(new Set(rows.flatMap((row) => Object.keys(row.objectives ?? {}))))
  return (
    <Table
      rows={rows}
      rowKey={(row) => row.candidate_id}
      onRowClick={onSelect}
      columns={[
        { key: 'candidate', header: t('optimisation.candidate'), render: (row) => <span className="font-mono text-xs">{row.candidate_id}</span> },
        { key: 'rank', header: t('optimisation.rank'), render: (row) => row.rank ?? '—', align: 'end' },
        {
          key: 'frontier',
          header: t('optimisation.frontier'),
          render: (row) => (row.on_frontier ? <Badge tone="ok">on frontier</Badge> : <Badge tone="neutral">dominated</Badge>),
        },
        ...objectiveKeys.map((key) => ({
          key,
          header: key,
          align: 'end' as const,
          render: (row: CandidateView) => formatNumber(row.objectives?.[key] ?? null, locale, 4),
        })),
        {
          key: 'state',
          header: t('common.status'),
          render: (row) =>
            row.feasible ? <Badge tone="ok">feasible</Badge> : <Badge tone="danger">{row.rejected_because ?? 'infeasible'}</Badge>,
        },
        {
          key: 'selected',
          header: '',
          render: (row) => (selected?.candidate_id === row.candidate_id ? <Badge tone="info">selected</Badge> : null),
        },
      ]}
    />
  )
}

export default function OptimisationWorkspace() {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const [tab, setTab] = useState('run')

  const [parametersRaw, setParametersRaw] = useState(DEFAULT_PARAMETERS)
  const [hydraulicsRaw, setHydraulicsRaw] = useState(DEFAULT_HYDRAULICS)
  const [limitsRaw, setLimitsRaw] = useState(DEFAULT_LIMITS)
  const [samples, setSamples] = useState(24)
  const [selectedObjectives, setSelectedObjectives] = useState<string[]>([
    'ecd_margin_si',
    'spp_utilisation',
    'annular_velocity_m_s',
  ])
  const [parseError, setParseError] = useState<string | null>(null)
  const [explanation, setExplanation] = useState<OptimisationExplanation | null>(null)
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateView | null>(null)

  const objectivesCatalogue = useQuery({
    queryKey: ['optimisation-objectives'],
    queryFn: ({ signal }) => drillingApi.optimisationObjectives(signal),
  })

  const wellbores = useQuery({ queryKey: ['wellbores', id], queryFn: ({ signal }) => drillingApi.listWellbores(id, signal) })
  const sections = useQuery({
    queryKey: ['sections', wellbores.data?.items[0]?.id],
    queryFn: ({ signal }) => drillingApi.listSections(wellbores.data?.items[0]?.id as string, signal),
    enabled: Boolean(wellbores.data?.items[0]?.id),
  })

  const optimise = useMutation({
    mutationFn: (payload: OptimisePayload) => drillingApi.optimise(id, payload),
    onSuccess: (data) => {
      setExplanation(data.optimisation.explanation)
      setSelectedCandidate(data.optimisation.explanation.recommended)
      setTab('explain')
    },
  })

  const recommended = useMemo(() => explanation?.recommended ?? null, [explanation])
  const activeCandidate = selectedCandidate ?? recommended

  function submit() {
    try {
      const payload: OptimisePayload = {
        parameters: JSON.parse(parametersRaw),
        hydraulics_inputs: JSON.parse(hydraulicsRaw),
        limits: JSON.parse(limitsRaw),
        objectives: selectedObjectives.map((key) => ({ key })),
        samples,
        section_id: sections.data?.items.at(-1)?.id ?? null,
        title: 'Interactive optimisation from the Engineering Workspace',
      }
      setParseError(null)
      optimise.mutate(payload)
    } catch (error) {
      setParseError(error instanceof Error ? error.message : 'invalid JSON')
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('optimisation.title')}
        description="Candidates are generated, evaluated with the hydraulics and torque & drag engines, filtered by constraints, ranked on the Pareto frontier and explained candidate by candidate."
        breadcrumb={<Link to={`/wells/${id}/cockpit`}>{t('nav.cockpit')}</Link>}
        actions={
          explanation?.recommendation_id && (
            <Link to={`/wells/${id}/cockpit`}>
              <Button size="sm">View recommendation</Button>
            </Link>
          )
        }
      />

      <Tabs
        tabs={[
          { key: 'run', label: t('optimisation.problem') },
          {
            key: 'explain',
            label: t('optimisation.whyNot'),
            badge: explanation && <Badge tone="neutral">{explanation.why_not.length}</Badge>,
          },
          { key: 'objectives', label: t('optimisation.objectives') },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'run' && (
        <div className="grid gap-3 xl:grid-cols-[1fr_1fr]">
          <Card title={t('optimisation.parameters')} subtitle="swept decision variables, in SI">
            <Field label={t('optimisation.parameters')} hint="Each variable declares min, max, steps and its unit.">
              <textarea
                value={parametersRaw}
                onChange={(event) => setParametersRaw(event.target.value)}
                rows={6}
                spellCheck={false}
                className="w-full rounded border border-graphite-300 bg-graphite-50 p-2 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-950"
              />
            </Field>
            <div className="mt-3">
              <Field label="Hydraulics base inputs (SI)">
                <textarea
                  value={hydraulicsRaw}
                  onChange={(event) => setHydraulicsRaw(event.target.value)}
                  rows={18}
                  spellCheck={false}
                  className="w-full rounded border border-graphite-300 bg-graphite-50 p-2 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-950"
                />
              </Field>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <Field label={t('optimisation.limits')}>
                <textarea
                  value={limitsRaw}
                  onChange={(event) => setLimitsRaw(event.target.value)}
                  rows={3}
                  spellCheck={false}
                  className="w-full rounded border border-graphite-300 bg-graphite-50 p-2 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-950"
                />
              </Field>
              <Field label={t('optimisation.samples')} hint="grid points evaluated (2–500)">
                <input
                  type="number"
                  min={2}
                  max={500}
                  value={samples}
                  onChange={(event) => setSamples(Number(event.target.value))}
                  className="w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
                />
              </Field>
            </div>
          </Card>

          <Card
            title={t('optimisation.objectives')}
            subtitle="only objectives an engine computes can be selected"
            actions={objectivesCatalogue.data && <Badge tone="neutral">{objectivesCatalogue.data.computable.length} available</Badge>}
          >
            <Async query={objectivesCatalogue}>
              {(data) => (
                <div className="space-y-3">
                  <ul className="space-y-1.5">
                    {data.computable.map((objective) => (
                      <li
                        key={objective.key}
                        className="flex items-start gap-2 rounded border border-graphite-100 p-2 dark:border-graphite-800"
                      >
                        <input
                          type="checkbox"
                          id={`objective-${objective.key}`}
                          checked={selectedObjectives.includes(objective.key)}
                          onChange={(event) =>
                            setSelectedObjectives((current) =>
                              event.target.checked
                                ? [...current, objective.key]
                                : current.filter((item) => item !== objective.key),
                            )
                          }
                          className="mt-0.5"
                        />
                        <label htmlFor={`objective-${objective.key}`} className="min-w-0 flex-1 cursor-pointer">
                          <span className="block text-xs font-medium">{objective.label}</span>
                          <span className="block text-[11px] text-graphite-500">
                            {objective.sense} · source: <span className="font-mono">{objective.source}</span>
                          </span>
                          {objective.unit && <span className="text-[11px] text-graphite-500">unit {objective.unit}</span>}
                        </label>
                      </li>
                    ))}
                  </ul>

                  <div className="rounded border border-warning/40 bg-amber-50/50 p-2 dark:bg-amber-950/20">
                    <p className="text-xs font-medium text-warning">{t('optimisation.notEvaluated')}</p>
                    <p className="mt-1 text-[11px]">{t('optimisation.notEvaluatedHint')}</p>
                    <ul className="mt-1 list-disc ps-4 text-[11px]">
                      {Object.entries(data.not_evaluated).map(([key, reason]) => (
                        <li key={key}>
                          <span className="font-mono">{key}</span>: {reason}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </Async>

            {parseError && <p className="mt-2 text-xs text-danger">Invalid JSON: {parseError}</p>}
            {optimise.error && (
              <div className="mt-2">
                <ErrorState error={optimise.error} />
              </div>
            )}
            <div className="mt-3">
              <Button variant="primary" onClick={submit} disabled={optimise.isPending || selectedObjectives.length === 0}>
                {t('optimisation.run')}
              </Button>
            </div>
            {optimise.isPending && <Loading label="Sweeping, evaluating and ranking candidates…" />}
          </Card>
        </div>
      )}

      {tab === 'explain' && (
        <>
          {!explanation && (
            <EmptyState
              message="No optimisation run has been executed in this session."
              hint="Run one from the Problem tab; results are persisted server-side with their engine runs."
            />
          )}
          {explanation && (
            <div className="space-y-3">
              <Card
                title={t('optimisation.recommended')}
                subtitle={`${explanation.candidates_evaluated} candidates · ${explanation.feasible_count} feasible · ${explanation.pareto_count} on the Pareto frontier · run ${explanation.optimization_run_id}`}
                actions={
                  <>
                    <Badge tone="neutral">{formatDateTime(explanation.created_at)}</Badge>
                    <Badge tone="neutral">{explanation.problem_key}</Badge>
                  </>
                }
              >
                {!recommended ? (
                  <EmptyState message="No feasible candidate was found, so nothing is recommended." hint="Relax a constraint or extend the parameter ranges and re-run." />
                ) : (
                  <div className="space-y-3">
                    <dl className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                      {Object.entries(recommended.values).map(([key, value]) => (
                        <Value key={key} label={key} value={value} unit={key.endsWith('_si') ? 'Pa' : null} unitSystem={unitSystem} source={`candidate ${recommended.candidate_id}`} />
                      ))}
                    </dl>
                    <div>
                      <p className="mb-1 text-xs font-medium">Computed objectives</p>
                      <Table
                        rows={Object.entries(recommended.objectives).map(([key, value]) => ({
                          key,
                          value,
                          source: explanation.objective_sources?.[key]?.source ?? 'engine output',
                          sense: explanation.objective_sources?.[key]?.sense ?? '—',
                        }))}
                        rowKey={(row) => row.key}
                        columns={[
                          { key: 'objective', header: t('optimisation.objectives'), render: (row) => humanise(row.key) },
                          { key: 'value', header: 'Value', render: (row) => formatNumber(row.value, locale, 4), align: 'end' },
                          { key: 'sense', header: 'Direction', render: (row) => String(row.sense) },
                          { key: 'source', header: t('common.source'), render: (row) => <span className="font-mono text-[10px]">{String(row.source)}</span> },
                        ]}
                      />
                    </div>
                    {explanation.recommendation_id && (
                      <p className="text-[11px] text-graphite-500">
                        persisted as recommendation <span className="font-mono">{explanation.recommendation_id}</span>{' '}
                        with the engine run IDs {explanation.engine_run_ids.length > 0 ? explanation.engine_run_ids.join(', ') : 'unavailable'}
                      </p>
                    )}
                    {explanation.notes.length > 0 && (
                      <ul className="list-disc ps-4 text-[11px] text-graphite-500">
                        {explanation.notes.map((note) => (
                          <li key={note}>{note}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </Card>

              <div className="grid gap-3 xl:grid-cols-[3fr_2fr]">
                <Card title={t('optimisation.frontier')} subtitle="click a candidate to compare it">
                  <CandidateTable
                    rows={explanation.pareto_frontier}
                    onSelect={setSelectedCandidate}
                    selected={activeCandidate}
                  />
                  {explanation.available_options.length > explanation.pareto_frontier.length && (
                    <>
                      <p className="mt-3 mb-1 text-xs font-medium">Other feasible options</p>
                      <CandidateTable
                        rows={explanation.available_options}
                        onSelect={setSelectedCandidate}
                        selected={activeCandidate}
                      />
                    </>
                  )}
                </Card>

                <Card title={t('optimisation.whyNot')} subtitle="every rejected candidate, with the reason">
                  <WhyNotList entries={explanation.why_not} />
                </Card>
              </div>

              <div className="grid gap-3 xl:grid-cols-2">
                <Card title={t('optimisation.tradeOffs')}>
                  {explanation.trade_offs.length === 0 ? (
                    <EmptyState message="No trade-off was computed for this run." />
                  ) : (
                    <Json value={explanation.trade_offs} max={200} />
                  )}
                </Card>
                <Card title={t('optimisation.sensitivities')}>
                  {explanation.sensitivities.length === 0 ? (
                    <EmptyState
                      message="Sensitivity was not computed."
                      hint="The optimiser skips sensitivity analysis below three feasible candidates."
                    />
                  ) : (
                    <Json value={explanation.sensitivities} max={200} />
                  )}
                </Card>
              </div>

              {explanation.infeasible.length > 0 && (
                <Card title="Infeasible candidates" subtitle="rejected by a declared constraint, with the violation">
                  <Json value={explanation.infeasible} max={200} />
                </Card>
              )}

              <Card title="Reproducibility" subtitle="engine versions and hashes bound to this result">
                <div className="flex flex-wrap gap-2">
                  {Object.entries(explanation.engine_versions).map(([engineKey, version]) => (
                    <Badge key={engineKey} tone="neutral">
                      {engineKey}@{version}
                    </Badge>
                  ))}
                </div>
                <div className="mt-2">
                  <Json value={explanation.engine_run_ids} max={40} />
                </div>
              </Card>
            </div>
          )}
        </>
      )}

      {tab === 'objectives' && (
        <Card title={t('optimisation.objectives')} subtitle="the objective contract this deployment can compute">
          <Async query={objectivesCatalogue}>
            {(data) => (
              <div className="space-y-3">
                <p className="text-xs text-graphite-500">{data.note}</p>
                <Table
                  rows={data.computable}
                  rowKey={(row) => row.key}
                  columns={[
                    { key: 'key', header: 'Key', render: (row) => <span className="font-mono text-xs">{row.key}</span> },
                    { key: 'label', header: 'Objective', render: (row) => row.label },
                    { key: 'sense', header: 'Direction', render: (row) => row.sense },
                    { key: 'source', header: t('common.source'), render: (row) => <span className="font-mono text-[10px]">{row.source}</span> },
                  ]}
                />
                <div className="rounded border border-warning/40 bg-amber-50/50 p-2 dark:bg-amber-950/20">
                  <p className="text-xs font-medium text-warning">{t('optimisation.notEvaluated')}</p>
                  <Table
                    rows={Object.entries(data.not_evaluated).map(([key, reason]) => ({ key, reason }))}
                    rowKey={(row) => row.key}
                    columns={[
                      { key: 'key', header: 'Key', render: (row) => <span className="font-mono text-xs">{row.key}</span> },
                      { key: 'reason', header: 'Reason', render: (row) => row.reason },
                    ]}
                  />
                </div>
              </div>
            )}
          </Async>
        </Card>
      )}
    </div>
  )
}
