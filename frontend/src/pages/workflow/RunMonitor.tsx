/**
 * Run Monitor.
 *
 * Shows a workflow run as it happened: node-by-node execution with status, timings, outputs, engine
 * run and LLM call identifiers, the event stream, and the approval gate. A run that is waiting for a
 * human is shown as *paused*, with the approval, its action level and the exact payload that will be
 * executed if it is approved — the platform refuses to resume an undecided approval, and the UI has
 * no path around that.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { ApprovalRow, NodeRunState, RunEvent, RunSummary } from '../../api/types'
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
} from '../../components/common'
import { useI18n } from '../../i18n'
import { formatActionLevel, formatDateTime, formatDuration, formatStatus } from '../../lib/format'

function runStatusTone(status: string): 'ok' | 'warning' | 'danger' | 'info' | 'neutral' {
  switch (status) {
    case 'succeeded':
    case 'completed':
      return 'ok'
    case 'running':
      return 'info'
    case 'waiting_approval':
    case 'paused':
      return 'warning'
    case 'failed':
    case 'cancelled':
      return 'danger'
    default:
      return 'neutral'
  }
}

function nodeStatusTone(status: string): 'ok' | 'warning' | 'danger' | 'info' | 'neutral' {
  switch (status) {
    case 'succeeded':
      return 'ok'
    case 'running':
      return 'info'
    case 'waiting_approval':
    case 'skipped':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return 'neutral'
  }
}

function ApprovalCard({ approval }: { approval: ApprovalRow }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [comment, setComment] = useState('')

  const decide = useMutation({
    mutationFn: (decision: 'approved' | 'rejected') =>
      drillingApi.decideApproval(approval.id, { decision, comment: comment || undefined, resume: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['approvals'] })
      queryClient.invalidateQueries({ queryKey: ['run', approval.run_id] })
      queryClient.invalidateQueries({ queryKey: ['run-nodes', approval.run_id] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  return (
    <Card
      title={`${t('workflow.approvals')}: ${approval.title ?? approval.node_id}`}
      subtitle={`${approval.node_id} · ${formatActionLevel(approval.action_level)} · requested ${formatDateTime(approval.requested_at)}`}
      actions={<Badge tone={approval.status === 'pending' ? 'warning' : approval.status === 'approved' ? 'ok' : 'danger'}>{formatStatus(approval.status)}</Badge>}
    >
      <div className="space-y-3">
        {approval.status === 'pending' ? (
          <>
            <p className="text-xs text-graphite-600 dark:text-graphite-300">
              The run is paused. Nothing at this action level executes until a human decides — approving
              resumes the run exactly where it stopped.
            </p>
            <Field label="Comment" hint="Recorded with the decision in the approval record.">
              <input
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                className="w-full rounded border border-graphite-300 px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
              />
            </Field>
            <div className="flex gap-2">
              <Button variant="primary" onClick={() => decide.mutate('approved')} disabled={decide.isPending}>
                {t('workflow.approve')}
              </Button>
              <Button variant="danger" onClick={() => decide.mutate('rejected')} disabled={decide.isPending}>
                {t('workflow.reject')}
              </Button>
            </div>
            {decide.error && <ErrorState error={decide.error} />}
          </>
        ) : (
          <dl className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <dt className="text-graphite-500">{t('common.status')}</dt>
              <dd>{formatStatus(approval.status)}</dd>
            </div>
            <div>
              <dt className="text-graphite-500">{t('common.decidedAt')}</dt>
              <dd>{formatDateTime(approval.decided_at)}</dd>
            </div>
            <div>
              <dt className="text-graphite-500">Decided by</dt>
              <dd className="font-mono">{approval.decided_by ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-graphite-500">Comment</dt>
              <dd>{approval.comment ?? '—'}</dd>
            </div>
          </dl>
        )}

        <details open={approval.status === 'pending'}>
          <summary className="cursor-pointer text-xs font-medium">Payload under approval</summary>
          <Json value={approval.payload ?? {}} max={120} />
        </details>
      </div>
    </Card>
  )
}

function RunDetail({ runId }: { runId: string }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState('nodes')

  const run = useQuery({
    queryKey: ['run', runId],
    queryFn: () => drillingApi.getRun(runId),
    refetchInterval: (query) => {
      const status = (query.state.data as RunSummary | undefined)?.status
      return status === 'running' ? 2000 : false
    },
  })
  const nodes = useQuery({ queryKey: ['run-nodes', runId], queryFn: () => drillingApi.runNodes(runId) })
  const events = useQuery({ queryKey: ['run-events', runId], queryFn: () => drillingApi.runEvents(runId) })

  const resume = useMutation({
    mutationFn: () => drillingApi.resumeRun(runId, run.data?.pending_approval?.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['run-nodes', runId] })
    },
  })

  return (
    <div className="space-y-3">
      <Async query={run}>
        {(data) => (
          <Card
            title={`Run ${data.id}`}
            subtitle={`${data.workflow_id} v${data.version} · ${data.trigger_type} · started ${formatDateTime(data.started_at)}`}
            actions={
              <>
                <Badge tone={runStatusTone(data.status)}>{formatStatus(data.status)}</Badge>
                {data.duration_ms !== null && data.duration_ms !== undefined && (
                  <Badge tone="neutral">{formatDuration(data.duration_ms / 3_600_000)}</Badge>
                )}
                {data.resumable && (
                  <Button size="sm" onClick={() => resume.mutate()} disabled={resume.isPending}>
                    {t('workflow.resume')}
                  </Button>
                )}
              </>
            }
          >
            <div className="space-y-3">
              {data.status === 'waiting_approval' || data.status === 'paused' ? (
                <p className="rounded border border-warning/40 bg-amber-50/50 p-2 text-xs text-warning dark:bg-amber-950/20">
                  {t('workflow.awaitingApproval')}
                </p>
              ) : null}
              {data.error && (
                <p className="rounded border border-danger/40 bg-red-50/60 p-2 text-xs text-danger dark:bg-red-950/20">
                  {data.error}
                  {data.error_node_id && <> (node {data.error_node_id})</>}
                </p>
              )}
              <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                <div>
                  <dt className="text-graphite-500">Steps</dt>
                  <dd className="font-mono">{data.step_count}</dd>
                </div>
                <div>
                  <dt className="text-graphite-500">Cursor node</dt>
                  <dd className="font-mono">{data.cursor_node_id ?? '—'}</dd>
                </div>
                <div>
                  <dt className="text-graphite-500">Finished</dt>
                  <dd>{formatDateTime(data.finished_at)}</dd>
                </div>
                <div>
                  <dt className="text-graphite-500">Initiated by</dt>
                  <dd className="font-mono">{data.initiated_by ?? '—'}</dd>
                </div>
              </dl>
              {resume.error && <ErrorState error={resume.error} />}
            </div>
          </Card>
        )}
      </Async>

      <Async query={run}>
        {(data) => (data.pending_approval ? <ApprovalCard approval={data.pending_approval} /> : <></>)}
      </Async>

      <Tabs
        tabs={[
          { key: 'nodes', label: t('workflow.nodeRuns'), badge: nodes.data && <Badge tone="neutral">{nodes.data.total}</Badge> },
          { key: 'events', label: t('workflow.events'), badge: events.data && <Badge tone="neutral">{events.data.total}</Badge> },
          { key: 'outputs', label: 'Outputs' },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'nodes' && (
        <Card title={t('workflow.nodeRuns')}>
          <Async query={nodes} empty={(data) => (data.items.length === 0 ? <EmptyState message="No node has executed yet." /> : false)}>
            {(data) => (
              <Table
                rows={data.items}
                rowKey={(row: NodeRunState) => row.id}
                columns={[
                  {
                    key: 'node',
                    header: 'Node',
                    render: (row) => (
                      <span>
                        <span className="font-medium">{row.name ?? row.node_id}</span>
                        <span className="block font-mono text-[10px] text-graphite-500">
                          {row.node_id} · {row.node_type}
                        </span>
                      </span>
                    ),
                  },
                  { key: 'status', header: t('common.status'), render: (row) => <Badge tone={nodeStatusTone(row.status)}>{formatStatus(row.status)}</Badge> },
                  { key: 'attempt', header: 'Attempt', render: (row) => row.attempt ?? 1, align: 'end' },
                  { key: 'started', header: 'Started', render: (row) => formatDateTime(row.started_at) },
                  { key: 'finished', header: 'Finished', render: (row) => formatDateTime(row.finished_at) },
                  {
                    key: 'links',
                    header: 'References',
                    render: (row) => (
                      <span className="flex flex-col gap-0.5 font-mono text-[10px]">
                        {row.engine_run_id && <span>engine {row.engine_run_id}</span>}
                        {row.llm_call_id && <span>llm {row.llm_call_id}</span>}
                        {row.tool_call_id && <span>tool {row.tool_call_id}</span>}
                        {row.approval_id && <span>approval {row.approval_id}</span>}
                      </span>
                    ),
                  },
                  {
                    key: 'outputs',
                    header: 'Outputs',
                    render: (row) =>
                      row.outputs && Object.keys(row.outputs).length > 0 ? (
                        <details>
                          <summary className="cursor-pointer text-[11px]">view</summary>
                          <Json value={row.outputs} max={60} />
                        </details>
                      ) : (
                        '—'
                      ),
                  },
                  {
                    key: 'error',
                    header: 'Error',
                    render: (row) => (row.error ? <Json value={row.error} max={30} /> : '—'),
                  },
                ]}
              />
            )}
          </Async>
        </Card>
      )}

      {tab === 'events' && (
        <Card title={t('workflow.events')} subtitle="the runtime's own log, in order">
          <Async query={events} empty={(data) => (data.items.length === 0 ? <EmptyState message="No event has been recorded." /> : false)}>
            {(data) => (
              <ul className="space-y-1">
                {data.items.map((event: RunEvent) => (
                  <li key={event.id} className="flex flex-wrap items-start gap-3 rounded border border-graphite-100 px-2 py-1.5 text-xs dark:border-graphite-800">
                    <span className="w-36 shrink-0 font-mono text-[10px] text-graphite-500">
                      {formatDateTime(event.occurred_at ?? event.created_at ?? null)}
                    </span>
                    <Badge tone={event.level === 'error' ? 'danger' : event.level === 'warning' ? 'warning' : 'neutral'}>
                      {event.type}
                    </Badge>
                    {event.node_id && <span className="font-mono text-[10px]">{event.node_id}</span>}
                    <span className="min-w-0 flex-1">{event.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </Card>
      )}

      {tab === 'outputs' && (
        <Async query={run}>
          {(data) => (
            <div className="grid gap-3 xl:grid-cols-2">
              <Card title="Run outputs">
                {Object.keys(data.outputs ?? {}).length === 0 ? (
                  <EmptyState message="The run has produced no output yet." />
                ) : (
                  <Json value={data.outputs} max={200} />
                )}
              </Card>
              <Card title="Variables">
                {Object.keys(data.variables ?? {}).length === 0 ? (
                  <EmptyState message="No variables were set." />
                ) : (
                  <Json value={data.variables} max={200} />
                )}
              </Card>
              {data.artifacts && data.artifacts.length > 0 && (
                <Card title="Artifacts" className="xl:col-span-2">
                  <Json value={data.artifacts} max={200} />
                </Card>
              )}
            </div>
          )}
        </Async>
      )}
    </div>
  )
}

export default function RunMonitor() {
  const { t } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const runId = searchParams.get('run')
  const [tab, setTab] = useState('runs')

  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: () => drillingApi.listRuns({ limit: 100 }),
    refetchInterval: runId ? false : 5000,
  })
  const approvals = useQuery({
    queryKey: ['approvals'],
    queryFn: () => drillingApi.listApprovals({ status: 'pending' }),
    refetchInterval: 5000,
  })

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('workflow.runMonitor')}
        description="Node execution, engine and model references, the event log, and the human approval gate."
        actions={
          <Link to="/workflows">
            <Button size="sm">{t('workflow.title')}</Button>
          </Link>
        }
      />

      <Tabs
        tabs={[
          { key: 'runs', label: t('nav.runs'), badge: runs.data && <Badge tone="neutral">{runs.data.total}</Badge> },
          {
            key: 'approvals',
            label: t('workflow.approvals'),
            badge: approvals.data && approvals.data.total > 0 && <Badge tone="warning">{approvals.data.total}</Badge>,
          },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'runs' && (
        <>
          <Card
            title="Runs"
            subtitle={runId ? `selected run ${runId}` : 'select a run to inspect it'}
            actions={runId && <Button size="sm" variant="ghost" onClick={() => setSearchParams({})}>Clear selection</Button>}
          >
            <Async query={runs} empty={(data) => (data.items.length === 0 ? <EmptyState message={t('empty.noRuns')} hint="Start a run from the Workflow Studio." /> : false)}>
              {(data) => (
                <Table
                  rows={data.items}
                  rowKey={(row: RunSummary) => row.id}
                  onRowClick={(row) => setSearchParams({ run: row.id })}
                  columns={[
                    { key: 'id', header: 'Run', render: (row) => <span className="font-mono text-[11px]">{row.id}</span> },
                    { key: 'workflow', header: 'Workflow', render: (row) => <span className="font-mono text-[11px]">{row.workflow_key ?? row.workflow_id}</span> },
                    { key: 'version', header: 'v', render: (row) => row.version, align: 'end' },
                    { key: 'status', header: t('common.status'), render: (row) => <Badge tone={runStatusTone(row.status)}>{formatStatus(row.status)}</Badge> },
                    { key: 'trigger', header: 'Trigger', render: (row) => row.trigger_type },
                    { key: 'well', header: 'Well', render: (row) => <span className="font-mono text-[10px]">{row.well_id ?? '—'}</span> },
                    { key: 'started', header: 'Started', render: (row) => formatDateTime(row.started_at) },
                    {
                      key: 'duration',
                      header: 'Duration',
                      align: 'end',
                      render: (row) => (row.duration_ms === null || row.duration_ms === undefined ? '—' : formatDuration(row.duration_ms / 3_600_000)),
                    },
                  ]}
                />
              )}
            </Async>
          </Card>

          {runId && <RunDetail runId={runId} />}
        </>
      )}

      {tab === 'approvals' && (
        <Card title={t('workflow.approvals')} subtitle="actions above the caller's authorization ceiling require a recorded decision">
          <Async query={approvals} empty={(data) => (data.items.length === 0 ? <EmptyState message="No approval is waiting." /> : false)}>
            {(data) => (
              <ul className="space-y-3">
                {data.items.map((approval) => (
                  <li key={approval.id}>
                    <ApprovalCard approval={approval} />
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </Card>
      )}

      {runs.isLoading && <Loading />}
    </div>
  )
}
