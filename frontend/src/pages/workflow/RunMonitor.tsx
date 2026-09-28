/**
 * Run Monitor.
 *
 * Shows a workflow run as it happened: the scope it was started against, node-by-node execution with
 * status, timing, resolution and references, the runtime's own event log, outputs and artifacts, and
 * the human approval gate.
 *
 * Two rules shape this page. First, everything it shows comes from the run envelope the API returns
 * (`{run, workflow, node_runs, artifacts, events, pending_approval, resumable}`) — including after a
 * reload, which is why a run waiting for a human is reconstructed from the server rather than from
 * anything React remembers. Second, a value that was not returned is shown as *not returned*, never
 * as an empty string that reads like a real value.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { ApprovalRow, NodeRunState, RunDetail, RunEvent, RunSummary } from '../../api/types'
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

/** Terminal run states: once one is reached the monitor stops asking the server for updates. */
const LIVE_RUN_STATUSES = new Set(['queued', 'running', 'pending'])

function runStatusTone(status: string): 'ok' | 'warning' | 'danger' | 'info' | 'neutral' {
  switch (status) {
    case 'succeeded':
    case 'completed':
      return 'ok'
    case 'running':
      return 'info'
    case 'queued':
    case 'pending':
      return 'neutral'
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

/** A value the server did not send, said out loud instead of rendered as an empty cell. */
function orNotReturned(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'Not returned'
  return String(value)
}

function JsonOrNone({ value, max = 80 }: { value: unknown; max?: number }) {
  if (value === null || value === undefined) return <span className="text-xs text-graphite-500">Not returned</span>
  if (Array.isArray(value) && value.length === 0) return <span className="text-xs text-graphite-500">Empty</span>
  if (typeof value === 'object' && value !== null && Object.keys(value as object).length === 0) {
    return <span className="text-xs text-graphite-500">Empty</span>
  }
  return <Json value={value} max={max} />
}

function parseProposedAction(raw: string | null): unknown {
  if (!raw) return null
  try {
    return JSON.parse(raw) as unknown
  } catch {
    // The server stores whatever the requester's node proposed. If it is not JSON, show it as text
    // rather than pretending a parse failure means there was nothing to show.
    return raw
  }
}

/**
 * The approval gate.
 *
 * The note is sent as `note` and the server records it as `decision_note`; conditions are the terms
 * the decision is granted under. Rejecting asks for a reason because the record is the point of the
 * gate — the server accepts a decision without one, the interface does not send it.
 */
function ApprovalCard({ approval, onDecided }: { approval: ApprovalRow; onDecided?: (status: string) => void }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')
  const [conditionsText, setConditionsText] = useState('')

  const decide = useMutation({
    mutationFn: (decision: 'approved' | 'rejected') =>
      drillingApi.decideApproval(approval.id, {
        decision,
        note: note.trim() || undefined,
        conditions: conditionsText
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean),
        resume: true,
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['approvals'] })
      if (approval.run_id) {
        queryClient.invalidateQueries({ queryKey: ['run', approval.run_id] })
        queryClient.invalidateQueries({ queryKey: ['runs'] })
      }
      onDecided?.(data.resumed_run ? data.resumed_run.status : data.approval.status)
    },
  })

  const pending = approval.status === 'pending'
  const noteIsEmpty = note.trim() === ''

  return (
    <Card
      data-testid="approval-card"
      // The requester's own title is the heading: it is what the approver is being asked to decide.
      title={approval.title}
      subtitle={`${t('workflow.approvalRequest')} · ${approval.node_id ?? t('common.unknown')} · ${formatActionLevel(
        approval.action_level,
      )}`}
      actions={
        <Badge tone={pending ? 'warning' : approval.status === 'approved' ? 'ok' : 'danger'}>
          {formatStatus(approval.status)}
        </Badge>
      }
    >
      <div className="space-y-4">
        <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <dt className="text-graphite-500">Action level</dt>
            <dd>{formatActionLevel(approval.action_level)}</dd>
          </div>
          <div>
            <dt className="text-graphite-500">Required role</dt>
            <dd>{orNotReturned(approval.required_role)}</dd>
          </div>
          <div>
            <dt className="text-graphite-500">Requested by</dt>
            <dd className="font-mono">{orNotReturned(approval.requested_by)}</dd>
          </div>
          <div>
            <dt className="text-graphite-500">Requested at</dt>
            <dd>{formatDateTime(approval.requested_at)}</dd>
          </div>
          <div>
            <dt className="text-graphite-500">Expires</dt>
            <dd>
              {formatDateTime(approval.expires_at)}
              {approval.overdue && (
                <>
                  {' '}
                  <Badge tone="danger">{t('workflow.overdue')}</Badge>
                </>
              )}
            </dd>
          </div>
          <div className="sm:col-span-2 lg:col-span-3">
            <dt className="text-graphite-500">Run</dt>
            <dd className="font-mono">
              {approval.run_id ? (
                <Link className="underline" to={`/runs?run=${encodeURIComponent(approval.run_id)}`}>
                  {approval.run_id}
                </Link>
              ) : (
                'Not attached to a run'
              )}
            </dd>
          </div>
        </dl>

        {approval.description && (
          <p className="text-sm text-graphite-700 dark:text-graphite-200">{approval.description}</p>
        )}

        <div className="grid gap-3 lg:grid-cols-2">
          <div className="rounded border border-graphite-200 p-2 dark:border-graphite-700">
            <p className="mb-1 text-xs font-medium text-graphite-600 dark:text-graphite-300">
              {t('workflow.riskNotes')}
            </p>
            {approval.risk_notes ? (
              <p className="whitespace-pre-wrap text-xs">{approval.risk_notes}</p>
            ) : (
              <p className="text-xs text-graphite-500">{t('workflow.noneDeclared')}</p>
            )}
          </div>
          <div className="rounded border border-graphite-200 p-2 dark:border-graphite-700">
            <p className="mb-1 text-xs font-medium text-graphite-600 dark:text-graphite-300">
              {t('workflow.evidenceRefs')}
            </p>
            {approval.evidence_refs.length === 0 ? (
              <p className="text-xs text-graphite-500">{t('workflow.noneAttached')}</p>
            ) : (
              <ul className="space-y-0.5 font-mono text-[11px]">
                {approval.evidence_refs.map((ref) => (
                  <li key={ref}>{ref}</li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <details>
          <summary className="cursor-pointer text-xs font-medium">{t('workflow.proposedAction')}</summary>
          <div className="mt-2 space-y-2">
            <JsonOrNone value={parseProposedAction(approval.proposed_action)} max={120} />
            <p className="text-[11px] text-graphite-500">{t('workflow.requestPayload')}</p>
            <JsonOrNone value={approval.request_payload} max={160} />
          </div>
        </details>

        {pending ? (
          <div className="space-y-3">
            <p className="text-xs text-graphite-600 dark:text-graphite-300">{t('workflow.approvalPaused')}</p>
            <Field
              label={t('workflow.decisionNote')}
              hint={t('workflow.decisionNoteHint')}
            >
              <textarea
                value={note}
                onChange={(event) => setNote(event.target.value)}
                rows={2}
                data-testid="approval-note"
                className="w-full rounded border border-graphite-300 px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
              />
            </Field>
            <Field
              label={t('workflow.conditions')}
              hint={t('workflow.conditionsHint')}
            >
              <textarea
                value={conditionsText}
                onChange={(event) => setConditionsText(event.target.value)}
                rows={2}
                data-testid="approval-conditions"
                className="w-full rounded border border-graphite-300 px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
              />
            </Field>
            <div className="flex gap-2">
              <Button
                variant="primary"
                data-testid="approval-approve"
                onClick={() => decide.mutate('approved')}
                disabled={decide.isPending}
              >
                {t('workflow.approve')}
              </Button>
              <Button
                variant="danger"
                data-testid="approval-reject"
                onClick={() => decide.mutate('rejected')}
                disabled={decide.isPending || noteIsEmpty}
              >
                {t('workflow.reject')}
              </Button>
            </div>
            <p className="text-xs text-graphite-500">{t('workflow.rejectionNeedsReason')}</p>
            {decide.error && <ErrorState error={decide.error} />}
          </div>
        ) : (
          <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-3">
            <div>
              <dt className="text-graphite-500">{t('common.decidedAt')}</dt>
              <dd>{formatDateTime(approval.decided_at)}</dd>
            </div>
            <div>
              <dt className="text-graphite-500">{t('workflow.decidedBy')}</dt>
              <dd className="font-mono">{orNotReturned(approval.decided_by)}</dd>
            </div>
            <div>
              <dt className="text-graphite-500">{t('workflow.decisionNote')}</dt>
              <dd className="whitespace-pre-wrap">{orNotReturned(approval.decision_note)}</dd>
            </div>
            <div className="sm:col-span-3">
              <dt className="text-graphite-500">{t('workflow.conditions')}</dt>
              <dd>
                {approval.conditions.length === 0 ? (
                  <span className="text-graphite-500">{t('workflow.noConditions')}</span>
                ) : (
                  <ul className="list-inside list-disc">
                    {approval.conditions.map((condition) => (
                      <li key={condition}>{condition}</li>
                    ))}
                  </ul>
                )}
              </dd>
            </div>
          </dl>
        )}
      </div>
    </Card>
  )
}

/** Scope as recorded on the run. Absent values are named, so "no well" is visible as such. */
function RunScope({ run }: { run: RunSummary }) {
  const items: Array<[string, string | null]> = [
    ['Project', run.project_id],
    ['Well', run.well_id],
    ['Wellbore', run.wellbore_id],
    ['Section', run.section_id],
    ['Operation', run.operation_id],
  ]
  return (
    <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3 lg:grid-cols-5" data-testid="run-scope">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-graphite-500">{label}</dt>
          <dd className="font-mono">{value ? value : <span className="text-graphite-500">None</span>}</dd>
        </div>
      ))}
    </dl>
  )
}

function NodeRunsTable({ nodeRuns }: { nodeRuns: NodeRunState[] }) {
  const { t } = useI18n()
  if (nodeRuns.length === 0) return <EmptyState message={t('workflow.noNodeRuns')} />
  return (
    <Table
      rows={nodeRuns}
      rowKey={(row) => row.id}
      columns={[
        {
          key: 'node',
          header: t('workflow.node'),
          render: (row) => (
            <span>
              <span className="font-medium">{row.node_name ?? row.node_id}</span>
              <span className="block font-mono text-[10px] text-graphite-500">
                {row.node_id} · {row.node_type}
              </span>
            </span>
          ),
        },
        {
          key: 'status',
          header: t('common.status'),
          render: (row) => (
            <span>
              <Badge tone={nodeStatusTone(row.status)}>{formatStatus(row.status)}</Badge>
              {row.resolution && (
                <span className="block font-mono text-[10px] text-graphite-500">{row.resolution}</span>
              )}
            </span>
          ),
        },
        { key: 'attempt', header: t('workflow.attempt'), align: 'end', render: (row) => `${row.attempt ?? 1}/${row.max_attempts ?? 1}` },
        {
          key: 'duration',
          header: t('workflow.duration'),
          align: 'end',
          render: (row) => (row.duration_ms === null || row.duration_ms === undefined ? '—' : formatDuration(row.duration_ms / 3_600_000)),
        },
        { key: 'started', header: t('workflow.startedHeader'), render: (row) => formatDateTime(row.started_at) },
        { key: 'finished', header: t('workflow.finishedHeader'), render: (row) => formatDateTime(row.finished_at) },
        {
          key: 'references',
          header: t('workflow.references'),
          render: (row) => (
            <span className="flex flex-col gap-0.5 font-mono text-[10px]">
              {row.engine_run_id && <span>engine {row.engine_run_id}</span>}
              {row.llm_call_id && <span>llm {row.llm_call_id}</span>}
              {row.tool_call_id && <span>tool {row.tool_call_id}</span>}
              {row.approval_id && <span>approval {row.approval_id}</span>}
              {!row.engine_run_id && !row.llm_call_id && !row.tool_call_id && !row.approval_id && (
                <span className="text-graphite-500">{t('workflow.none')}</span>
              )}
            </span>
          ),
        },
        {
          key: 'outputs',
          header: t('workflow.outputsTab'),
          render: (row) =>
            row.outputs && Object.keys(row.outputs).length > 0 ? (
              <details>
                <summary className="cursor-pointer text-[11px]">{t('common.view')}</summary>
                <Json value={row.outputs} max={60} />
              </details>
            ) : (
              <span className="text-graphite-500">{row.status === 'skipped' ? t('workflow.notRun') : '—'}</span>
            ),
        },
        {
          key: 'error',
          header: t('workflow.error'),
          render: (row) =>
            row.error ? <Json value={row.error} max={30} /> : <span className="text-graphite-500">—</span>,
        },
      ]}
    />
  )
}

function EventLog({ events }: { events: RunEvent[] }) {
  const { t } = useI18n()
  if (events.length === 0) return <EmptyState message={t('workflow.noEvents')} />
  const ordered = [...events].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
  return (
    <ul className="space-y-1" data-testid="run-events">
      {ordered.map((event) => (
        <li
          key={event.id}
          data-testid="run-event"
          data-seq={event.seq}
          className="flex flex-wrap items-start gap-3 rounded border border-graphite-100 px-2 py-1.5 text-xs dark:border-graphite-800"
        >
          <span className="w-10 shrink-0 font-mono text-[10px] text-graphite-500">#{event.seq}</span>
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
  )
}

function RunDetailView({ runId }: { runId: string }) {
  const { t } = useI18n()
  const [tab, setTab] = useState('nodes')
  const [notice, setNotice] = useState<string | null>(null)

  const detail = useQuery({
    queryKey: ['run', runId],
    queryFn: () => drillingApi.getRun(runId),
    // Stopgap while the run is live: the events stream (checkpoint 3) is the real transport, and it
    // replaces this interval rather than sitting next to it. A run waiting for a human is not polled
    // at all — nothing changes until a person decides, and that decision happens on this page.
    refetchInterval: (query) => {
      const status = query.state.data?.run.status
      return status && LIVE_RUN_STATUSES.has(status) ? 2000 : false
    },
  })

  // The run's approval record, whatever state it is in. The envelope carries a *pending* approval,
  // because that is what the run is blocked on; the decision that unblocked it is a separate row, so
  // it is fetched explicitly — otherwise the note and the conditions a person recorded would vanish
  // from the page at the moment they were recorded.
  const approvals = useQuery({
    queryKey: ['run-approvals', runId],
    queryFn: () => drillingApi.listApprovals({ run_id: runId, status: 'any' }),
  })
  const decidedApprovals = (approvals.data?.items ?? []).filter((row) => row.status !== 'pending')

  const resume = useMutation({
    mutationFn: (approvalId: string | null | undefined) =>
      drillingApi.resumeRun(runId, approvalId ?? undefined),
    onSuccess: (run) => {
      setNotice(`${t('workflow.resumedAs')} ${formatStatus(run.status)}`)
      void detail.refetch()
      void approvals.refetch()
    },
  })

  return (
    <div className="space-y-3">
      <Async query={detail}>
        {(data: RunDetail) => (
          <>
            <Card
              title={`Run ${data.run.id}`}
              subtitle={`${data.workflow?.key ?? data.run.workflow_key ?? data.run.workflow_id} @ v${
                data.run.version ?? '—'
              } · ${data.run.trigger_type} · started ${formatDateTime(data.run.started_at)}`}
              actions={
                <>
                  <Badge tone={runStatusTone(data.run.status)} data-testid="run-status">
                    {formatStatus(data.run.status)}
                  </Badge>
                  {data.run.is_dry_run && <Badge tone="info">{t('workflow.dryRun')}</Badge>}
                  {data.run.duration_ms !== null && data.run.duration_ms !== undefined && (
                    <Badge tone="neutral">{formatDuration(data.run.duration_ms / 3_600_000)}</Badge>
                  )}
                  {data.resumable && (
                    <Button
                      size="sm"
                      data-testid="resume-run"
                      onClick={() => resume.mutate(data.pending_approval?.id)}
                      disabled={resume.isPending || data.pending_approval?.status === 'pending'}
                      title={
                        data.pending_approval?.status === 'pending'
                          ? t('workflow.decideBeforeResume')
                          : undefined
                      }
                    >
                      {t('workflow.resume')}
                    </Button>
                  )}
                </>
              }
            >
              <div className="space-y-3">
                {(data.run.status === 'waiting_approval' || data.run.status === 'paused') && (
                  <p
                    data-testid="run-awaiting-approval"
                    className="rounded border border-warning/40 bg-amber-50/50 p-2 text-xs text-warning dark:bg-amber-950/20"
                  >
                    {t('workflow.awaitingApproval')}
                    {data.run.cursor_node_id && ` (${t('workflow.node')}: ${data.run.cursor_node_id})`}
                  </p>
                )}
                {notice && (
                  <p data-testid="run-notice" className="rounded border border-graphite-200 p-2 text-xs dark:border-graphite-700">
                    {notice}
                  </p>
                )}
                {data.run.error && (
                  <p
                    data-testid="run-error"
                    className="rounded border border-danger/40 bg-red-50/60 p-2 text-xs text-danger dark:bg-red-950/20"
                  >
                    {data.run.error}
                    {data.run.error_node_id && <> ({t('workflow.node')} {data.run.error_node_id})</>}
                  </p>
                )}

                <RunScope run={data.run} />

                <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                  <div>
                    <dt className="text-graphite-500">{t('workflow.steps')}</dt>
                    <dd className="font-mono">{data.run.step_count}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.cursorNode')}</dt>
                    <dd className="font-mono">{orNotReturned(data.run.cursor_node_id)}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.finishedHeader')}</dt>
                    <dd>{formatDateTime(data.run.finished_at)}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.initiatedBy')}</dt>
                    <dd className="font-mono">{orNotReturned(data.run.initiated_by)}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.approvedBy')}</dt>
                    <dd className="font-mono">{orNotReturned(data.run.approved_by)}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.workflowVersion')}</dt>
                    <dd className="font-mono">{orNotReturned(data.run.workflow_version_id)}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.pendingApproval')}</dt>
                    <dd className="font-mono">{orNotReturned(data.run.pending_approval_id)}</dd>
                  </div>
                  <div>
                    <dt className="text-graphite-500">{t('workflow.resumable')}</dt>
                    <dd>{data.resumable ? t('common.yes') : t('common.no')}</dd>
                  </div>
                </dl>

                {data.workflow && (
                  <p className="text-xs text-graphite-500">
                    {t('workflow.workflow')}: <span className="font-mono">{data.workflow.name}</span> (
                    {data.workflow.status})
                  </p>
                )}
                {resume.error && <ErrorState error={resume.error} />}
              </div>
            </Card>

            {data.pending_approval && (
              <ApprovalCard
                approval={data.pending_approval}
                onDecided={(status) => {
                  setNotice(`${t('workflow.decisionRecorded')} ${formatStatus(status)}`)
                  void approvals.refetch()
                }}
              />
            )}

            {decidedApprovals.length > 0 && (
              <section className="space-y-3" data-testid="approval-record">
                <h2 className="text-sm font-semibold tracking-tight">{t('workflow.approvalRecord')}</h2>
                <p className="text-xs text-graphite-500">{t('workflow.approvalRecordHint')}</p>
                {decidedApprovals.map((approval) => (
                  <ApprovalCard key={approval.id} approval={approval} />
                ))}
              </section>
            )}

            <Tabs
              tabs={[
                {
                  key: 'nodes',
                  label: t('workflow.nodeRuns'),
                  badge: <Badge tone="neutral">{data.node_runs.length}</Badge>,
                },
                {
                  key: 'events',
                  label: t('workflow.events'),
                  badge: <Badge tone="neutral">{data.events?.length ?? 0}</Badge>,
                },
                { key: 'outputs', label: t('workflow.outputsTab') },
                { key: 'inputs', label: t('workflow.inputsTab') },
              ]}
              active={tab}
              onChange={setTab}
            />

            {tab === 'nodes' && (
              <Card title={t('workflow.nodeRuns')} subtitle={t('workflow.eachExecution')}>
                <NodeRunsTable nodeRuns={data.node_runs} />
              </Card>
            )}

            {tab === 'events' && (
              <Card title={t('workflow.events')} subtitle={t('workflow.eventsSubtitle')}>
                <EventLog events={data.events ?? []} />
              </Card>
            )}

            {tab === 'outputs' && (
              <div className="grid gap-3 xl:grid-cols-2">
                <Card title={t('workflow.runOutputs')}>
                  <JsonOrNone value={data.run.outputs} max={200} />
                </Card>
                <Card title={t('workflow.variables')}>
                  <JsonOrNone value={data.run.variables} max={200} />
                </Card>
                <Card title={t('workflow.artifacts')} className="xl:col-span-2">
                  {data.artifacts.length === 0 ? (
                    <EmptyState message={t('workflow.noArtifacts')} />
                  ) : (
                    <Table
                      rows={data.artifacts}
                      rowKey={(row) => String(row.id ?? row.name)}
                      columns={[
                        { key: 'name', header: t('workflow.name'), render: (row) => String(row.name ?? '—') },
                        { key: 'kind', header: t('workflow.kind'), render: (row) => String(row.kind ?? '—') },
                        { key: 'node', header: t('workflow.node'), render: (row) => <span className="font-mono text-[10px]">{String(row.node_id ?? '—')}</span> },
                        { key: 'rows', header: t('workflow.rows'), align: 'end', render: (row) => (row.row_count === null || row.row_count === undefined ? '—' : String(row.row_count)) },
                        { key: 'bytes', header: t('workflow.bytes'), align: 'end', render: (row) => (row.byte_size === null || row.byte_size === undefined ? '—' : String(row.byte_size)) },
                        {
                          key: 'evidence',
                          header: t('workflow.evidenceRefs'),
                          render: (row) =>
                            Array.isArray(row.evidence_refs) && row.evidence_refs.length > 0 ? (
                              <span className="font-mono text-[10px]">{(row.evidence_refs as string[]).join(', ')}</span>
                            ) : (
                              <span className="text-graphite-500">{t('workflow.none')}</span>
                            ),
                        },
                        {
                          key: 'payload',
                          header: t('workflow.payload'),
                          render: (row) =>
                            row.payload === null || row.payload === undefined ? (
                              <span className="text-graphite-500">—</span>
                            ) : (
                              <details>
                                <summary className="cursor-pointer text-[11px]">{t('common.view')}</summary>
                                <Json value={row.payload} max={60} />
                              </details>
                            ),
                        },
                      ]}
                    />
                  )}
                </Card>
              </div>
            )}

            {tab === 'inputs' && (
              <div className="grid gap-3 xl:grid-cols-2">
                <Card title={t('workflow.runInputs')}>
                  <JsonOrNone value={data.run.inputs} max={200} />
                </Card>
                <Card title={t('workflow.recordedScope')}>
                  <JsonOrNone value={data.run.context} max={200} />
                </Card>
                <Card title={t('workflow.metrics')} className="xl:col-span-2">
                  <JsonOrNone value={data.run.metrics} max={200} />
                </Card>
              </div>
            )}
          </>
        )}
      </Async>
    </div>
  )
}

export default function RunMonitor() {
  const { t } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const runId = searchParams.get('run')
  // One tab holds both the list and the open run: a deep link to a run shows the list it belongs to
  // with that run expanded underneath, which is also what a reload reproduces.
  const [tab, setTab] = useState('runs')

  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: () => drillingApi.listRuns({ limit: 100 }),
    // The list is not polled while a run is open: the open run has its own refresh, and a list that
    // silently reshuffles underneath a reader is worse than a list that is a few seconds old.
    refetchInterval: runId ? false : 5000,
  })
  // The inbox defaults to what needs attention, and can be widened to the decisions already taken:
  // an approval inbox that only ever shows pending requests hides its own audit trail.
  const [approvalStatus, setApprovalStatus] = useState<'pending' | 'approved' | 'rejected' | 'any'>('pending')
  const approvals = useQuery({
    queryKey: ['approvals', approvalStatus],
    queryFn: () => drillingApi.listApprovals({ status: approvalStatus }),
    // Nothing starts or stops waiting for a human without someone acting, so this is a slow refresh,
    // not a live feed — and stopgap at that: the events stream (checkpoint 3) is the real transport.
    refetchInterval: approvalStatus === 'pending' ? 5000 : false,
  })

  const selectRun = (id: string) => setSearchParams({ run: id })

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('workflow.runMonitor')}
        description={t('workflow.runMonitorDescription')}
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
            badge:
              approvalStatus === 'pending' && approvals.data && approvals.data.total > 0 ? (
                <Badge tone="warning">{approvals.data.total}</Badge>
              ) : undefined,
          },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'runs' && (
        <>
          <Card
            title={t('nav.runs')}
            subtitle={runId ? `${t('workflow.selectedRun')} ${runId}` : t('workflow.selectRun')}
            actions={
              runId && (
                <Button size="sm" variant="ghost" onClick={() => setSearchParams({})}>
                  {t('common.clearSelection')}
                </Button>
              )
            }
          >
            <Async
              query={runs}
              empty={(data) => (data.items.length === 0 ? <EmptyState message={t('empty.noRuns')} hint={t('workflow.startRunHint')} /> : false)}
            >
              {(data) => (
                <Table
                  rows={data.items}
                  rowKey={(row: RunSummary) => row.id}
                  onRowClick={(row) => selectRun(row.id)}
                  isRowActive={(row: RunSummary) => row.id === runId}
                  columns={[
                    {
                      key: 'id',
                      header: t('workflow.runColumn'),
                      render: (row) => (
                        <Link
                          to={`/runs?run=${encodeURIComponent(row.id)}`}
                          onClick={(event) => {
                            event.preventDefault()
                            selectRun(row.id)
                          }}
                          className="font-mono text-[11px] underline"
                        >
                          {row.id}
                        </Link>
                      ),
                    },
                    {
                      key: 'workflow',
                      header: t('workflow.workflow'),
                      render: (row) => (
                        <span className="font-mono text-[11px]">{row.workflow_key ?? row.workflow_id}</span>
                      ),
                    },
                    { key: 'version', header: 'v', align: 'end', render: (row) => (row.version === null ? '—' : row.version) },
                    {
                      key: 'status',
                      header: t('common.status'),
                      render: (row) => <Badge tone={runStatusTone(row.status)}>{formatStatus(row.status)}</Badge>,
                    },
                    { key: 'trigger', header: t('workflow.trigger'), render: (row) => row.trigger_type },
                    { key: 'well', header: t('workflow.well'), render: (row) => <span className="font-mono text-[10px]">{row.well_id ?? '—'}</span> },
                    { key: 'started', header: t('workflow.startedHeader'), render: (row) => formatDateTime(row.started_at) },
                    {
                      key: 'duration',
                      header: t('workflow.duration'),
                      align: 'end',
                      render: (row) =>
                        row.duration_ms === null || row.duration_ms === undefined
                          ? '—'
                          : formatDuration(row.duration_ms / 3_600_000),
                    },
                  ]}
                />
              )}
            </Async>
          </Card>

          {runId && <RunDetailView runId={runId} />}
        </>
      )}

      {tab === 'approvals' && (
        <Card
          title={t('workflow.approvals')}
          subtitle={t('workflow.approvalsSubtitle')}
          actions={
            <select
              aria-label={t('workflow.approvalStatusFilter')}
              value={approvalStatus}
              onChange={(event) => setApprovalStatus(event.target.value as typeof approvalStatus)}
              className="rounded border border-graphite-300 bg-white px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-900"
            >
              <option value="pending">{t('workflow.approvalStatusPending')}</option>
              <option value="approved">{t('workflow.approvalStatusApproved')}</option>
              <option value="rejected">{t('workflow.approvalStatusRejected')}</option>
              <option value="any">{t('workflow.approvalStatusAny')}</option>
            </select>
          }
        >
          <Async
            query={approvals}
            empty={(data) =>
              data.items.length === 0 ? (
                <EmptyState
                  message={
                    approvalStatus === 'pending' ? t('workflow.noApprovals') : t('workflow.noDecidedApprovals')
                  }
                />
              ) : (
                false
              )
            }
          >
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
