/**
 * Agent, skill, engine, tool and action library.
 *
 * Everything is read from the platform registries. An agent whose tools, engines or context
 * sections are not available in this deployment is listed with its declared contract *and* an
 * explicit state, rather than being hidden or, worse, shown as if it worked.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { drillingApi } from '../../api/endpoints'
import { PageHeader } from '../../components/layout/AppShell'
import { Async, Badge, Card, Table, Tabs } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatActionLevel, formatValidationStatus } from '../../lib/format'

function statusTone(status: string): 'ok' | 'warning' | 'danger' | 'neutral' {
  if (status === 'available' || status === 'verified_against_reference') return 'ok'
  if (status === 'not_configured' || status === 'formula_derived') return 'warning'
  if (status === 'unavailable' || status === 'needs_field_validation') return 'danger'
  return 'neutral'
}

export default function LibraryPage() {
  const { t } = useI18n()
  const [tab, setTab] = useState('engines')

  const agents = useQuery({ queryKey: ['platform-agents'], queryFn: ({ signal }) => drillingApi.agents(signal) })
  const engines = useQuery({ queryKey: ['engines'], queryFn: ({ signal }) => drillingApi.listEngines(signal) })
  const tools = useQuery({ queryKey: ['platform-tools'], queryFn: ({ signal }) => drillingApi.tools(signal) })
  const actions = useQuery({ queryKey: ['platform-actions'], queryFn: ({ signal }) => drillingApi.actions(signal) })
  const extractors = useQuery({ queryKey: ['platform-extractors'], queryFn: ({ signal }) => drillingApi.extractors(signal) })
  const units = useQuery({ queryKey: ['units'], queryFn: ({ signal }) => drillingApi.units(signal) })

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('library.title')}
        description="What this deployment actually contains: agents and skills with their declared contracts, engines with validation status, tools with their side effects, and the units the platform understands."
      />

      <Tabs
        tabs={[
          { key: 'engines', label: t('library.engines'), badge: engines.data && <Badge tone="neutral">{engines.data.total}</Badge> },
          { key: 'agents', label: t('library.agents'), badge: agents.data && <Badge tone="neutral">{agents.data.agents.length}</Badge> },
          { key: 'skills', label: t('library.skills'), badge: agents.data && <Badge tone="neutral">{agents.data.skills.length}</Badge> },
          { key: 'tools', label: t('library.tools'), badge: tools.data && <Badge tone="neutral">{tools.data.total}</Badge> },
          { key: 'actions', label: t('library.actions'), badge: actions.data && <Badge tone="neutral">{actions.data.total}</Badge> },
          { key: 'extractors', label: t('library.extractors'), badge: extractors.data && <Badge tone="neutral">{extractors.data.total}</Badge> },
          { key: 'units', label: t('library.units'), badge: units.data && <Badge tone="neutral">{units.data.total}</Badge> },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'engines' && (
        <Card title={t('library.engines')} subtitle="validation status is a per-engine declaration, not a marketing claim">
          <Async query={engines}>
            {(data) => (
              <Table
                rows={data.items}
                rowKey={(row) => row.key}
                columns={[
                  {
                    key: 'engine',
                    header: 'Engine',
                    render: (row) => (
                      <span>
                        <span className="font-medium">{row.name}</span>
                        <span className="block font-mono text-[10px] text-graphite-500">
                          {row.key}@{row.version} · {row.domain_pack}
                        </span>
                      </span>
                    ),
                  },
                  { key: 'category', header: 'Category', render: (row) => row.category },
                  {
                    key: 'validation',
                    header: t('engineering.validationStatus'),
                    render: (row) => <Badge tone={statusTone(row.validation_status)}>{formatValidationStatus(row.validation_status)}</Badge>,
                  },
                  { key: 'level', header: 'Action', render: (row) => formatActionLevel(row.action_level) },
                  {
                    key: 'ports',
                    header: 'Consumes → produces',
                    render: (row) => (
                      <span className="text-[11px]">
                        {row.consumes.join(', ') || '—'} → {row.produces.join(', ') || '—'}
                      </span>
                    ),
                  },
                  {
                    key: 'limitations',
                    header: t('common.limitations'),
                    render: (row) =>
                      row.limitations.length === 0 ? (
                        <Badge tone="warning">none declared</Badge>
                      ) : (
                        <details>
                          <summary className="cursor-pointer text-[11px]">{row.limitations.length} declared</summary>
                          <ul className="list-disc ps-4 text-[11px]">
                            {row.limitations.map((item) => (
                              <li key={item}>{item}</li>
                            ))}
                          </ul>
                        </details>
                      ),
                  },
                ]}
              />
            )}
          </Async>
        </Card>
      )}

      {tab === 'agents' && (
        <Card title={t('library.agents')} subtitle="declared contract: skills, tools, engines, context and autonomy ceiling">
          <Async query={agents}>
            {(data) => (
              <div className="space-y-3">
                <Badge tone="info">{t('library.maxAutonomy')}: {formatActionLevel(data.max_agent_autonomy)}</Badge>
                <div className="grid gap-2 lg:grid-cols-2">
                  {data.agents.map((agent) => (
                    <div key={agent.key} className="rounded border border-graphite-100 p-3 dark:border-graphite-800">
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <p className="text-sm font-medium">{agent.name}</p>
                          <p className="font-mono text-[10px] text-graphite-500">
                            {agent.key}@{agent.version}
                          </p>
                        </div>
                        <Badge tone="info">{formatActionLevel(agent.max_action_level)}</Badge>
                      </div>
                      <p className="mt-1 text-xs text-graphite-600 dark:text-graphite-300">{agent.description}</p>
                      <dl className="mt-2 space-y-1 text-[11px]">
                        <div>
                          <dt className="inline text-graphite-500">{t('library.skills')}: </dt>
                          <dd className="inline">{agent.skills.join(', ') || 'none declared'}</dd>
                        </div>
                        <div>
                          <dt className="inline text-graphite-500">{t('library.engines')}: </dt>
                          <dd className="inline font-mono">{agent.engines.join(', ') || 'none'}</dd>
                        </div>
                        <div>
                          <dt className="inline text-graphite-500">{t('library.tools')}: </dt>
                          <dd className="inline font-mono">{agent.tools.join(', ') || 'none'}</dd>
                        </div>
                        <div>
                          <dt className="inline text-graphite-500">{t('library.contextSections')}: </dt>
                          <dd className="inline font-mono">{agent.context_sections.join(', ') || '—'}</dd>
                        </div>
                        <div>
                          <dt className="inline text-graphite-500">LLM profile: </dt>
                          <dd className="inline font-mono">{agent.llm_profile}</dd>
                        </div>
                        <div>
                          <dt className="inline text-graphite-500">Permission: </dt>
                          <dd className="inline font-mono">{agent.required_permission}</dd>
                        </div>
                      </dl>
                      {agent.guardrails.length > 0 && (
                        <details className="mt-2 text-[11px]">
                          <summary className="cursor-pointer font-medium">{t('library.guardrails')}</summary>
                          <ul className="list-disc ps-4">
                            {agent.guardrails.map((guardrail) => (
                              <li key={guardrail}>{guardrail}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Async>
        </Card>
      )}

      {tab === 'skills' && (
        <Card title={t('library.skills')} subtitle="granular capabilities an agent may compose">
          <Async query={agents}>
            {(data) => (
              <Table
                rows={data.skills}
                rowKey={(row) => row.key}
                columns={[
                  {
                    key: 'skill',
                    header: 'Skill',
                    render: (row) => (
                      <span>
                        <span className="font-medium">{row.name}</span>
                        <span className="block font-mono text-[10px] text-graphite-500">
                          {row.key}@{row.version} · {row.kind}
                        </span>
                      </span>
                    ),
                  },
                  { key: 'description', header: t('common.detail'), render: (row) => row.description },
                  { key: 'engines', header: t('library.engines'), render: (row) => <span className="font-mono text-[10px]">{row.engines.join(', ') || '—'}</span> },
                  { key: 'level', header: 'Action', render: (row) => formatActionLevel(row.action_level) },
                  { key: 'permission', header: 'Permission', render: (row) => <span className="font-mono text-[10px]">{row.required_permission}</span> },
                ]}
              />
            )}
          </Async>
        </Card>
      )}

      {tab === 'tools' && (
        <Card title={t('library.tools')} subtitle="side effects and approval requirements are declared, not assumed">
          <Async query={tools}>
            {(data) => (
              <Table
                rows={data.items}
                rowKey={(row) => row.key}
                columns={[
                  { key: 'tool', header: 'Tool', render: (row) => <span className="font-mono text-xs">{row.key}</span> },
                  { key: 'name', header: 'Name', render: (row) => row.name },
                  { key: 'description', header: t('common.detail'), render: (row) => row.description },
                  { key: 'effects', header: t('library.sideEffects'), render: (row) => row.side_effects.join(', ') || 'none' },
                  { key: 'approval', header: t('library.requiresApproval'), render: (row) => (row.requires_approval ? <Badge tone="warning">yes</Badge> : <Badge tone="ok">no</Badge>) },
                  { key: 'idempotent', header: t('library.idempotent'), render: (row) => (row.idempotent ? 'yes' : 'no') },
                  { key: 'level', header: 'Action', render: (row) => formatActionLevel(row.action_level) },
                ]}
              />
            )}
          </Async>
        </Card>
      )}

      {tab === 'actions' && (
        <Card title={t('library.actions')} subtitle="the action-level model that governs what may run without approval">
          <Async query={actions}>
            {(data) => (
              <div className="space-y-3">
                <div className="rounded border border-graphite-100 p-2 dark:border-graphite-800">
                  <p className="text-xs font-medium">Levels</p>
                  <ul className="mt-1 space-y-0.5 text-[11px]">
                    {Object.entries(data.levels).map(([level, description]) => (
                      <li key={level}>
                        <span className="font-mono font-medium">{level}</span> — {description}
                      </li>
                    ))}
                  </ul>
                </div>
                <Table
                  rows={data.items}
                  rowKey={(row) => row.key}
                  columns={[
                    { key: 'action', header: 'Action', render: (row) => <span className="font-mono text-xs">{row.key}</span> },
                    { key: 'level', header: 'Level', render: (row) => <Badge tone="info">{row.level}</Badge> },
                    { key: 'description', header: t('common.detail'), render: (row) => row.description },
                    { key: 'permission', header: 'Permission', render: (row) => <span className="font-mono text-[10px]">{row.permission}</span> },
                  ]}
                />
              </div>
            )}
          </Async>
        </Card>
      )}

      {tab === 'extractors' && (
        <Card title={t('library.extractors')} subtitle="document types are decided by content signals and schema, never by filename">
          <Async query={extractors}>
            {(data) => (
              <Table
                rows={data.items}
                rowKey={(row) => row.key}
                columns={[
                  { key: 'extractor', header: 'Extractor', render: (row) => <span className="font-mono text-xs">{row.key}@{row.version}</span> },
                  { key: 'name', header: 'Name', render: (row) => row.name },
                  { key: 'doc_types', header: 'Document types', render: (row) => row.doc_types.join(', ') || 'any' },
                ]}
              />
            )}
          </Async>
        </Card>
      )}

      {tab === 'units' && (
        <Card title={t('library.units')} subtitle="SI is canonical; every other unit is an explicit conversion">
          <Async query={units}>
            {(data) => (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-1">
                  {data.dimensions.map((dimension) => (
                    <Badge key={dimension.dimension} tone="neutral">
                      {dimension.dimension} → {dimension.canonical_unit}
                    </Badge>
                  ))}
                </div>
                <Table
                  rows={data.items}
                  rowKey={(row) => row.symbol}
                  columns={[
                    { key: 'symbol', header: 'Symbol', render: (row) => <span className="font-mono text-xs">{row.symbol}</span> },
                    { key: 'name', header: 'Name', render: (row) => row.name },
                    { key: 'dimension', header: 'Dimension', render: (row) => row.dimension },
                    { key: 'factor', header: 'Factor', render: (row) => row.factor, align: 'end' },
                    { key: 'kind', header: 'Kind', render: (row) => row.kind },
                    { key: 'aliases', header: 'Aliases', render: (row) => row.aliases.join(', ') || '—' },
                    { key: 'custom', header: 'Custom conversion', render: (row) => (row.custom_conversion ? 'yes' : 'no') },
                  ]}
                />
              </div>
            )}
          </Async>
        </Card>
      )}
    </div>
  )
}
