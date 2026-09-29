/**
 * Workflow Studio.
 *
 * A visual editor over the platform's own workflow graph contract:
 *
 *   * the node palette is generated from `/registry/node-types` — adding a node type on the server
 *     makes it appear here without touching this file;
 *   * the graph is saved through `PUT /workflows/{id}/graph`, which validates it server-side;
 *   * validation is offered before saving, and every issue is shown with its code and node;
 *   * a run is started from here and lands in the Run Monitor, where node execution, events and
 *     human approvals are visible.
 *
 * Client-side drawing is editor convenience only: the server always validates the graph it is
 * given, so a hand-edited payload cannot bypass the checks.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { NodeTypeSpec, Workflow, WorkflowGraph, WorkflowValidation } from '../../api/types'
import {
  flowToGraph,
  graphToFlow,
  INPUT_HANDLE,
  isGraphDirty,
  OUTPUT_HANDLE,
  type StudioEdgeData,
  type StudioNodeData,
} from '../../lib/workflowGraph'
import { holdsAll, holdsPermission } from '../../lib/permissions'
import { useSession } from '../../stores/session'
import { NodeConfigEditor, toConfigSchema } from './NodeConfigEditor'
import { ApiError } from '../../api/client'
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
  Tabs,
} from '../../components/common'
import { useI18n } from '../../i18n'
import { formatActionLevel, formatDateTime } from '../../lib/format'

const FAMILY_COLOURS: Record<string, string> = {
  data: '#0f7b8f',
  engineering: '#1d4ed8',
  analytics: '#6d28d9',
  rag: '#0e7490',
  ai: '#9333ea',
  logic: '#b45309',
  human: '#b91c1c',
  output: '#15803d',
  integration: '#4b5563',
}

/** One custom node type, registered once and reused for every node — the xyflow recommendation. */
function StudioNode({ data, selected }: NodeProps) {
  const nodeData = data as StudioNodeData
  const colour = FAMILY_COLOURS[nodeData.family] ?? '#4b5563'
  return (
    <div
      className={`min-w-[180px] rounded-md border bg-white shadow-sm dark:bg-graphite-900 ${
        selected ? 'border-signal ring-2 ring-signal/30' : 'border-graphite-300 dark:border-graphite-700'
      }`}
    >
      {/* Without handles React Flow has nothing to anchor an edge to, so a loaded graph renders as
          disconnected boxes and a drag cannot create an edge at all. */}
      <Handle type="target" id={INPUT_HANDLE} position={Position.Left} />
      <Handle type="source" id={OUTPUT_HANDLE} position={Position.Right} />
      <div
        className="flex items-center justify-between rounded-t-md px-2 py-1 text-[11px] font-medium text-white"
        style={{ backgroundColor: colour }}
      >
        <span className="truncate">{nodeData.label}</span>
        <span className="ms-2 shrink-0 opacity-80">{nodeData.actionLevel}</span>
      </div>
      <div className="px-2 py-1.5">
        <p className="font-mono text-[10px] text-graphite-500">{nodeData.nodeType}</p>
        <p className="text-[10px] text-graphite-500">{nodeData.family}</p>
      </div>
    </div>
  )
}

const nodeTypes = { studio: StudioNode }

const EMPTY_GRAPH: WorkflowGraph = { nodes: [], edges: [], variables: {}, settings: {} }

function StudioBody({
  workflow,
  onDirtyChange,
}: {
  workflow: Workflow
  onDirtyChange?: (dirty: boolean) => void
}) {
  const { t } = useI18n()
  const { devRoles } = useSession()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [nodes, setNodes] = useState<Node[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [variables, setVariables] = useState<Record<string, unknown>>({})
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [validation, setValidation] = useState<WorkflowValidation | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [baseline, setBaseline] = useState<WorkflowGraph | null>(null)
  const [savedVersion, setSavedVersion] = useState<number | null>(null)
  const [runWellId, setRunWellId] = useState('')

  const nodeTypesCatalogue = useQuery({
    queryKey: ['node-types'],
    queryFn: ({ signal }) => drillingApi.nodeTypes(signal),
  })
  /**
   * Which version the editor is looking at.
   *
   * `GET /workflows/{id}` without a version answers with the *published* version when there is one,
   * which is right for a run and wrong for an editor: after saving a draft, reopening the workflow
   * would show the published graph and the draft would look lost. The editor therefore asks for the
   * newest saved version explicitly, and can be pointed at any other row of the history.
   */
  const versions = useQuery({
    queryKey: ['workflow-versions', workflow.id],
    queryFn: ({ signal }) => drillingApi.workflowVersions(workflow.id, { limit: 20 }, signal),
  })
  const [viewVersion, setViewVersion] = useState<number | null>(null)
  const newestVersion = versions.data?.items[0]?.version ?? null
  const targetVersion = viewVersion ?? newestVersion ?? undefined
  const graph = useQuery({
    queryKey: ['workflow-graph', workflow.id, targetVersion ?? 'newest'],
    queryFn: ({ signal }) => drillingApi.getWorkflow(workflow.id, targetVersion === undefined ? {} : { version: targetVersion }, signal),
    enabled: versions.isSuccess && (newestVersion !== null || viewVersion !== null),
  })
  // What this identity may actually do. The server decides; this only keeps the editor from
  // offering a button whose only possible outcome is a refusal.
  const identity = useQuery({ queryKey: ['identity', devRoles], queryFn: ({ signal }) => drillingApi.identity(signal) })
  const permissions = identity.data?.permissions
  const canPublish = holdsPermission(permissions, 'workflow.publish')
  const canDraft = holdsPermission(permissions, 'workflow.draft')

  // The saved graph is loaded once into editor state and remembered as the baseline, so "unsaved
  // changes" is a real comparison against the stored version rather than a flag the editor sets
  // whenever a key is pressed.
  const loadedGraph = graph.data?.graph ?? null
  // A definition that has never been saved has no version at all. That is a normal state for a
  // workflow created a moment ago, not an error: the studio's job is then to let the user build the
  // first graph, so an empty graph is the honest starting point. The history is the authority on
  // "no version exists"; the 404 branch covers a version that disappeared between the two calls.
  const noVersionYet =
    (versions.isSuccess && versions.data.total === 0 && viewVersion === null) ||
    (graph.error instanceof ApiError && graph.error.status === 404)
  const graphToLoad = loadedGraph ?? (noVersionYet ? EMPTY_GRAPH : null)
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null)
  useEffect(() => {
    if (!graphToLoad) return
    const flow = graphToFlow(graphToLoad)
    setNodes(flow.nodes)
    setEdges(flow.edges)
    setVariables(graphToLoad.variables ?? {})
    setBaseline(graphToLoad)
    setSelectedId(null)
    setSelectedEdgeId(null)
    setValidation(null)
  }, [graphToLoad, workflow.id])

  // The version the canvas is holding comes from the server, never from a client-side counter.
  const serverVersion = graph.data?.version?.version ?? null
  useEffect(() => {
    setLoadedVersion(serverVersion)
  }, [serverVersion])

  // The message describes what the last action did; it is cleared when the user looks at another
  // definition or another version, not when the server reports the version that action produced.
  useEffect(() => {
    setSavedVersion(null)
    setMessage(null)
    setViewVersion(null)
  }, [workflow.id])

  const dirty = isGraphDirty(nodes, edges, variables, baseline)

  /**
   * Unsaved graph edits must not disappear silently.
   *
   * Two exits are protected: closing/reloading the tab (the browser's own confirmation, which is the
   * only mechanism available for a reload) and leaving this workflow inside the app, which is handled
   * by the studio's own dialog so the user can save, discard, or stay.
   *
   * `dirtyRef` exists because the `beforeunload` listener is registered once; reading state that
   * changes on every edit from a stale closure would leave the guard permanently off.
   */
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty
  useEffect(() => {
    onDirtyChange?.(dirty)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the callback is stable state setter
  }, [dirty])
  useEffect(() => {
    function onBeforeUnload(event: BeforeUnloadEvent) {
      if (!dirtyRef.current) return
      event.preventDefault()
      // Browsers show their own wording; returning a value is what triggers the prompt.
      event.returnValue = 'The workflow graph has unsaved changes.'
      return event.returnValue
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])

  const specByKey = useMemo(() => {
    const map: Record<string, NodeTypeSpec> = {}
    for (const spec of nodeTypesCatalogue.data?.items ?? []) map[spec.key] = spec
    return map
  }, [nodeTypesCatalogue.data])

  // Decorate nodes with family/level from the registry so the canvas shows real metadata.
  const decoratedNodes = useMemo(
    () =>
      nodes.map((node) => {
        const data = node.data as StudioNodeData
        const spec = specByKey[data.nodeType]
        return spec
          ? {
              ...node,
              data: {
                ...data,
                family: spec.family,
                actionLevel: spec.action_level,
                label: data.label || spec.name,
              } satisfies StudioNodeData,
            }
          : node
      }),
    [nodes, specByKey],
  )

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((current) => applyNodeChanges(changes, current))
  }, [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((current) => applyEdgeChanges(changes, current))
  }, [])
  const onConnect = useCallback((connection: Connection) => {
    // The handle the user dragged is the canvas's own wiring. What is stored is where the edge goes:
    // the definition keeps its own handle values (the seeded graphs carry none), so a graph built by
    // dragging saves exactly the edge the user drew and nothing invented alongside it.
    setEdges((current) => addEdge({ ...connection, sourceHandle: null, targetHandle: null }, current))
  }, [])

  /**
   * Connect two nodes without a mouse drag.
   *
   * Dragging a handle is the only way to create an edge in most editors, which leaves keyboard users
   * unable to build a graph at all. This is the same operation with a control: it appends the edge
   * through the same `addEdge` path the canvas uses, so both routes produce identical graphs.
   */
  const connectNodes = useCallback((sourceId: string, targetId: string) => {
    if (!sourceId || !targetId || sourceId === targetId) return
    setEdges((current) => {
      if (current.some((edge) => edge.source === sourceId && edge.target === targetId)) return current
      return addEdge({ source: sourceId, target: targetId, sourceHandle: null, targetHandle: null }, current)
    })
  }, [])

  // Saving sends the graph the canvas holds, with everything the editor does not model copied
  // forward from the loaded version (see lib/workflowGraph). The version number in the message is
  // the server's, and when the server reports the same version back it means the graph was
  // unchanged, which is stated rather than dressed up as a new version.
  const save = useMutation({
    mutationFn: () =>
      drillingApi.saveWorkflowGraph(workflow.id, {
        graph: flowToGraph(nodes, edges, variables, baseline),
        change_reason: 'edited in Workflow Studio',
      }),
    onSuccess: (result) => {
      const unchanged = loadedVersion !== null && result.version === loadedVersion
      setSavedVersion(result.version)
      // The editor now holds what was just stored, so it follows the newest version rather than
      // staying on whichever historic one the user had loaded.
      setViewVersion(null)
      setMessage(
        unchanged
          ? t('workflow.unchangedSave', { version: result.version })
          : t('workflow.savedVersion', {
              version: result.version,
              nodes: result.node_count,
              edges: result.edge_count,
            }),
      )
      queryClient.invalidateQueries({ queryKey: ['workflow-graph', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflow-versions', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  const validate = useMutation({
    mutationFn: () => drillingApi.validateWorkflow(flowToGraph(nodes, edges, variables, baseline)),
    onSuccess: (data) => setValidation(data.validation),
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  // Publishing names a version explicitly: the version the editor last saved, or the published one
  // when nothing is pending. The button is a convenience; `workflow.publish` is checked server-side.
  const publish = useMutation({
    mutationFn: () => drillingApi.publishWorkflow(workflow.id, savedVersion ?? loadedVersion ?? undefined),
    onSuccess: (result) => {
      setMessage(
        t('workflow.publishedVersionMessage', { version: result.version }) +
          (viewVersion !== null && viewVersion !== result.version
            ? ` ${t('workflow.publishedOtherVersion', { version: viewVersion })}`
            : ''),
      )
      queryClient.invalidateQueries({ queryKey: ['workflow-graph', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflow-versions', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  const saveAndPublish = useMutation({
    mutationFn: async () => {
      const version = await drillingApi.saveWorkflowGraph(workflow.id, {
        graph: flowToGraph(nodes, edges, variables, baseline),
        change_reason: 'edited in Workflow Studio',
        publish: true,
      })
      return version
    },
    onSuccess: (result) => {
      setSavedVersion(result.version)
      setViewVersion(null)
      setMessage(
        result.published_at
          ? t('workflow.savedAndPublished', { version: result.version })
          : t('workflow.savedNotPublished', { version: result.version }),
      )
      queryClient.invalidateQueries({ queryKey: ['workflow-graph', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflow-versions', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  // A run needs a well. A definition has no `well_id`, so the context is chosen here explicitly:
  // an unscoped run is not allowed to happen by accident.
  const wells = useQuery({ queryKey: ['wells'], queryFn: ({ signal }) => drillingApi.listWells({ limit: 200 }, signal) })
  const selectedWell = wells.data?.items.find((well) => well.id === runWellId) ?? null

  const startRun = useMutation({
    mutationFn: () =>
      drillingApi.startRun(workflow.id, {
        well_id: runWellId,
        project_id: selectedWell?.project_id ?? undefined,
        version: savedVersion ?? loadedVersion ?? undefined,
        trigger_type: 'manual',
      }),
    onSuccess: (run) => navigate(`/runs?run=${run.id}`),
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  const selectedNode = nodes.find((node) => node.id === selectedId) ?? null
  const selectedEdge = edges.find((edge) => edge.id === selectedEdgeId) ?? null

  function updateEdge(patch: Partial<{ condition: string; label: string; is_loop_back: boolean }>) {
    if (!selectedEdge) return
    setEdges((current) =>
      current.map((edge) => {
        if (edge.id !== selectedEdge.id) return edge
        const data = (edge.data ?? {}) as StudioEdgeData
        const existing = data.source ?? ({ source: edge.source, target: edge.target } as WorkflowGraph['edges'][number])
        const source: WorkflowGraph['edges'][number] = { ...existing, ...patch }
        // A conditional edge is shown on the canvas by its condition, so keep the two in step.
        const shown = patch.condition !== undefined ? patch.condition : (patch.label ?? edge.label)
        return {
          ...edge,
          label: shown || undefined,
          data: { ...data, source } satisfies StudioEdgeData,
        }
      }),
    )
  }
  const selectedSpec = selectedNode ? specByKey[(selectedNode.data as StudioNodeData).nodeType] : undefined

  /**
   * Why publishing is not offered, in the words of whatever is stopping it.
   *
   * Four reasons are collapsed into one disabled attribute, and a disabled button with no explanation
   * is its own bug: the user cannot tell whether to save first, ask for a role, or wait. They also
   * cannot tell "you are not permitted" from "your permissions could not be read" — and the studio
   * used to say the first while meaning the second, because a failed identity read leaves the
   * permission list undefined and every check then answers *no*. Telling an engineer they lack a
   * permission the server never denied is a false statement about their own account, so the unknown
   * case is named separately. The button stays disabled either way: it is the server that decides,
   * and offering an action that is certain to be refused is worse than not offering it.
   */
  const publishDisabledReason = identity.isLoading
    ? t('workflow.publishChecking')
    : !canPublish
      ? identity.error || !identity.data
        ? t('workflow.cannotPublishUnknown')
        : t('workflow.cannotPublish', { roles: identity.data.role_keys.join(', ') || 'none' })
      : dirty
        ? t('workflow.publishAfterSave')
        : t('workflow.publishHint')
  // The payload carries one issue list with a severity per issue; the badge counts what came back
  // rather than asking the server for a second, redundant pair of arrays.
  const validationIssues = {
    errors: validation?.issues.filter((issue) => issue.severity === 'error') ?? [],
    warnings: validation?.issues.filter((issue) => issue.severity === 'warning') ?? [],
  }

  function addNode(spec: NodeTypeSpec) {
    // Node ids must be unique in the graph, and the registry key alone does not guarantee that: the
    // same type may legitimately appear twice, and a loaded graph may already hold the id this
    // naming scheme would pick. Take the first free suffix instead of trusting the count.
    const base = spec.key.replace(/\./g, '_')
    const taken = new Set(nodes.map((node) => node.id))
    let id = `${base}_${nodes.length + 1}`
    for (let suffix = 1; taken.has(id); suffix += 1) id = `${base}_${nodes.length + 1 + suffix}`
    setNodes((current) => [
      ...current,
      {
        id,
        type: 'studio',
        position: { x: 120 + (current.length % 4) * 240, y: 100 + Math.floor(current.length / 4) * 140 },
        data: {
          label: spec.name,
          nodeType: spec.key,
          family: spec.family,
          actionLevel: spec.action_level,
          config: {},
        } satisfies StudioNodeData,
      },
    ])
    setSelectedId(id)
  }

  return (
    <div className="grid gap-3 xl:grid-cols-[260px_1fr_300px]">
      <Card title={t('workflow.palette')} subtitle={t('workflow.paletteHint')} dense>
        <Async query={nodeTypesCatalogue}>
          {(data) => (
            <div className="max-h-[65vh] space-y-2 overflow-auto">
              {Object.entries(data.families).map(([family, keys]) => (
                <div key={family}>
                  <p className="px-1 text-[10px] font-semibold tracking-wide text-graphite-500 uppercase">
                    {family}
                  </p>
                  <ul className="mt-0.5 space-y-0.5">
                    {keys.map((key) => {
                      const spec = data.items.find((item) => item.key === key)
                      if (!spec) return null
                      return (
                        <li key={key}>
                          <button
                            type="button"
                            title={`${spec.description} · ${formatActionLevel(spec.action_level)}`}
                            onClick={() => addNode(spec)}
                            className="w-full rounded px-2 py-1 text-start text-xs hover:bg-graphite-100 dark:hover:bg-graphite-800"
                          >
                            <span className="block truncate">{spec.name}</span>
                            <span className="block font-mono text-[10px] text-graphite-500">{spec.key}</span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </Async>
      </Card>

      {/*
        The permission list is what the editor's capabilities are read from, so a failure there is not
        cosmetic: every check answers *no* and the disabled buttons would claim the identity lacks a
        permission. The reason is also carried in the buttons' titles, but a tooltip is not a place to
        tell somebody their account is unauthorised, so it is said here, in the open, with a retry.
      */}
      {identity.error && (
        <div data-testid="identity-unavailable" className="space-y-1">
          <ErrorState error={identity.error} onRetry={() => void identity.refetch()} />
          <p className="text-xs text-graphite-600 dark:text-graphite-300">{t('workflow.cannotPublishUnknown')}</p>
        </div>
      )}

      <Card
        title={t('workflow.canvas')}
        subtitle={`${nodes.length} nodes · ${edges.length} edges`}
        dense
        actions={
          <>
            <Badge tone={dirty ? 'warning' : 'ok'}>{dirty ? t('workflow.dirtyBadge') : t('workflow.savedBadge')}</Badge>
            <Button size="sm" onClick={() => validate.mutate()} disabled={validate.isPending}>
              {t('workflow.validate')}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => save.mutate()}
              disabled={save.isPending || !canDraft}
              title={canDraft ? t('workflow.saveHint') : t('workflow.cannotDraft')}
            >
              {t('workflow.save')}
            </Button>
            <Button
              size="sm"
              onClick={() => publish.mutate()}
              disabled={publish.isPending || dirty || !canPublish}
              title={publishDisabledReason}
            >
              {t('workflow.publish')}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => saveAndPublish.mutate()}
              disabled={saveAndPublish.isPending || !holdsAll(permissions, ['workflow.draft', 'workflow.publish'])}
              title={publishDisabledReason}
            >
              {t('workflow.saveAndPublish')}
            </Button>
          </>
        }
      >
        <div className="h-[65vh] w-full">
          <ReactFlow
            nodes={decoratedNodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => {
              setSelectedId(node.id)
              setSelectedEdgeId(null)
            }}
            onEdgeClick={(_, edge) => {
              setSelectedEdgeId(edge.id)
              setSelectedId(null)
            }}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
        {!canDraft && (
          <p className="p-2 text-[11px] text-warning">
            {t('workflow.readOnlyIdentity')}
          </p>
        )}
        {message && (
          <p
            role="status"
            data-testid="studio-message"
            className="p-2 text-[11px] text-graphite-600 dark:text-graphite-300"
          >
            {message}
          </p>
        )}
        {noVersionYet && (
          <p className="p-2 text-[11px] text-graphite-500">{t('workflow.neverSaved')}</p>
        )}
        {graph.error && !noVersionYet && <ErrorState error={graph.error} />}
        {save.error && <ErrorState error={save.error} />}
        {publish.error && <ErrorState error={publish.error} />}
        {saveAndPublish.error && <ErrorState error={saveAndPublish.error} />}
        {startRun.error && <ErrorState error={startRun.error} />}
      </Card>

      <Card title={t('workflow.definition')} dense>
        <div className="flex flex-wrap items-start gap-4 p-2">
          <div className="min-w-[220px] flex-1">
            <p className="text-[11px] font-semibold tracking-wide text-graphite-500 uppercase">
              {t('workflow.latestVersion')}
            </p>
            <p className="font-mono text-sm" data-testid="loaded-version">
              v{loadedVersion ?? '—'}
              {newestVersion !== null && loadedVersion !== null && loadedVersion !== newestVersion && (
                <span className="ms-2 text-[11px] text-warning">
                  {t('workflow.viewingOlderVersion', { version: newestVersion })}
                </span>
              )}
              {savedVersion !== null && savedVersion !== loadedVersion && (
                <span className="ms-2 text-[11px] text-warning">
                  {t('workflow.savedDraft', { version: savedVersion })}
                </span>
              )}
            </p>
            <p className="text-[11px] text-graphite-500">
              {t('workflow.onServer')} {graph.data?.version?.node_count ?? nodes.length} nodes ·{' '}
              {graph.data?.version?.edge_count ?? edges.length} edges ·{' '}
              <span title={graph.data?.version?.graph_hash ?? ''}>
                {(graph.data?.version?.graph_hash ?? '—').slice(0, 12)}
              </span>
            </p>
          </div>
          <div className="min-w-[220px] flex-1">
            <p className="text-[11px] font-semibold tracking-wide text-graphite-500 uppercase">
              {t('workflow.publishedVersion')}
            </p>
            <p
              className="font-mono text-sm"
              data-testid="published-version"
              data-published-at={graph.data?.version?.published_at ?? ''}
            >
              {graph.data?.version?.published_at ? `v${graph.data.version.version}` : 'not published'}
            </p>
            <p className="text-[11px] text-graphite-500">
              {graph.data?.version?.published_at
                ? `published ${formatDateTime(graph.data.version.published_at)}`
                : t('workflow.noVersionForRun')}
            </p>
          </div>
          <div className="min-w-[260px] flex-1" data-testid="run-context">
            <Field label={t('workflow.runContext')} hint={t('workflow.runContextHint')}>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  aria-label={t('workflow.runContext')}
                  value={runWellId}
                  onChange={(event) => setRunWellId(event.target.value)}
                  className="min-w-[200px] flex-1 rounded border border-graphite-300 bg-white px-2 py-1.5 text-xs dark:border-graphite-700 dark:bg-graphite-900"
                >
                  <option value="">— select a well —</option>
                  {(wells.data?.items ?? []).map((well) => (
                    <option key={well.id} value={well.id}>
                      {well.name}
                    </option>
                  ))}
                </select>
                <Button
                  size="sm"
                  onClick={() => startRun.mutate()}
                  disabled={startRun.isPending || !runWellId || dirty}
                  title={t('empty.noRuns')}
                >
                  {t('workflow.run')}
                </Button>
              </div>
            </Field>
            {!runWellId && <p className="mt-1 text-[11px] text-warning">{t('workflow.noWellSelected')}</p>}
            {runWellId && dirty && (
              <p className="mt-1 text-[11px] text-warning">
                {t('workflow.dirtyRunWarning')}
              </p>
            )}
            {wells.error && <ErrorState error={wells.error} />}
          </div>
        </div>
      </Card>

      <div className="space-y-3">
        <Card title={t('workflow.nodeConfig')} dense>
          {!selectedNode && <EmptyState message={t('workflow.selectNode')} />}
          {selectedNode && selectedSpec && (
            <div className="space-y-2">
              <Field label={t('workflow.nodeId')}>
                <input
                  aria-label={t('workflow.nodeId')}
                  value={selectedNode.id}
                  readOnly
                  className="w-full rounded border border-graphite-200 bg-graphite-50 px-2 py-1 font-mono text-xs dark:border-graphite-800 dark:bg-graphite-950"
                />
              </Field>
              <Field label={t('workflow.nodeType')}>
                <input
                  aria-label={t('workflow.nodeType')}
                  value={selectedSpec.key}
                  readOnly
                  className="w-full rounded border border-graphite-200 bg-graphite-50 px-2 py-1 font-mono text-xs dark:border-graphite-800 dark:bg-graphite-950"
                />
              </Field>
              <div className="flex flex-wrap gap-1">
                <Badge tone="neutral">{selectedSpec.family}</Badge>
                <Badge tone="info">{formatActionLevel(selectedSpec.action_level)}</Badge>
                {selectedSpec.terminal && <Badge tone="ok">terminal</Badge>}
              </div>
              <p className="text-[11px] text-graphite-500">{selectedSpec.description}</p>
              {selectedSpec.required_inputs.length > 0 && (
                <p className="text-[11px] text-graphite-500">
                  {t('workflow.requiredInputs')}: {selectedSpec.required_inputs.join(', ')}
                </p>
              )}
              <Field label={t('workflow.nodeLabel')}>
                <input
                  value={(selectedNode.data as StudioNodeData).label}
                  onChange={(event) =>
                    setNodes((current) =>
                      current.map((node) =>
                        node.id === selectedNode.id
                          ? { ...node, data: { ...node.data, label: event.target.value } }
                          : node,
                      ),
                    )
                  }
                  className="w-full rounded border border-graphite-300 px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-900"
                />
              </Field>
              <Field label={t('workflow.connectTo')} hint={t('workflow.connectHint')}>
                <select
                  aria-label={t('workflow.connectTo')}
                  value=""
                  onChange={(event) => {
                    connectNodes(selectedNode.id, event.target.value)
                    event.target.value = ''
                  }}
                  className="w-full rounded border border-graphite-300 px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-900"
                >
                  <option value="">{t('workflow.connectPlaceholder')}</option>
                  {nodes
                    .filter((node) => node.id !== selectedNode.id)
                    .map((node) => (
                      <option key={node.id} value={node.id}>
                        {(node.data as StudioNodeData).label} ({node.id})
                      </option>
                    ))}
                </select>
              </Field>
              <NodeConfigEditor
                config={(selectedNode.data as StudioNodeData).config ?? {}}
                schema={toConfigSchema(selectedSpec.config_schema)}
                onChange={(config) =>
                  setNodes((current) =>
                    current.map((node) =>
                      node.id === selectedNode.id ? { ...node, data: { ...node.data, config } } : node,
                    ),
                  )
                }
              />
              {selectedSpec.config_schema && (
                <details className="text-[11px]">
                  <summary className="cursor-pointer">config schema</summary>
                  <Json value={selectedSpec.config_schema} max={60} />
                </details>
              )}
              <Button
                size="sm"
                variant="danger"
                onClick={() => {
                  setNodes((current) => current.filter((node) => node.id !== selectedNode.id))
                  setEdges((current) =>
                    current.filter((edge) => edge.source !== selectedNode.id && edge.target !== selectedNode.id),
                  )
                  setSelectedId(null)
                }}
              >
                Delete node
              </Button>
            </div>
          )}
        </Card>

        <Card title={t('workflow.selectedEdge')} dense>
          {!selectedEdge && <EmptyState message={t('workflow.selectEdge')} />}
          {selectedEdge && (
            <div className="space-y-2">
              <p className="font-mono text-[11px]">
                {selectedEdge.source} → {selectedEdge.target}
              </p>
              <Field
                label={t('workflow.edgeCondition')}
                hint="An expression; the edge is taken when it is truthy. Leave empty for an unconditional edge."
              >
                <input
                  aria-label={t('workflow.edgeCondition')}
                  value={((selectedEdge.data as StudioEdgeData | undefined)?.source?.condition ?? '') || ''}
                  onChange={(event) => updateEdge({ condition: event.target.value })}
                  className="w-full rounded border border-graphite-300 px-2 py-1 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-900"
                />
              </Field>
              <Field label={t('workflow.edgeLabel')}>
                <input
                  aria-label={t('workflow.edgeLabel')}
                  value={((selectedEdge.data as StudioEdgeData | undefined)?.source?.label ?? '') || ''}
                  onChange={(event) => updateEdge({ label: event.target.value })}
                  className="w-full rounded border border-graphite-300 px-2 py-1 text-[11px] dark:border-graphite-700 dark:bg-graphite-900"
                />
              </Field>
              <label className="flex items-center gap-2 text-[11px]">
                <input
                  type="checkbox"
                  checked={Boolean((selectedEdge.data as StudioEdgeData | undefined)?.source?.is_loop_back)}
                  onChange={(event) => updateEdge({ is_loop_back: event.target.checked })}
                />
                {t('workflow.edgeLoopBack')}
                <span className="text-graphite-500">
                  (excluded from entry/exit detection, so a retry loop is not read as a cycle)
                </span>
              </label>
              <Button
                size="sm"
                variant="danger"
                onClick={() => {
                  setEdges((current) => current.filter((edge) => edge.id !== selectedEdge.id))
                  setSelectedEdgeId(null)
                }}
              >
                {t('workflow.deleteEdge')}
              </Button>
            </div>
          )}
        </Card>

        <Card title={t('workflow.versionHistory')} dense>
          <Async query={versions}>
            {(data) =>
              data.items.length === 0 ? (
                <EmptyState message={t('workflow.noVersionsYet')} />
              ) : (
                <ul className="space-y-1 text-[11px]">
                  {data.items.map((row) => (
                    <li
                      key={row.id}
                      className="flex flex-wrap items-center gap-2 rounded border border-graphite-100 p-1.5 dark:border-graphite-800"
                    >
                      <Badge tone={row.published_at ? 'ok' : 'neutral'}>v{row.version}</Badge>
                      <span className="font-mono text-graphite-500">{row.graph_hash.slice(0, 10)}</span>
                      <span className="text-graphite-500">
                        {row.node_count} nodes · {row.edge_count} edges
                      </span>
                      <span className="text-graphite-500">{formatDateTime(row.created_at)}</span>
                      {row.published_at && <Badge tone="info">published</Badge>}
                      {row.validation_errors > 0 && (
                        <Badge tone="danger">{row.validation_errors} error(s)</Badge>
                      )}
                      <Button
                        size="sm"
                        variant={row.version === loadedVersion ? 'primary' : 'ghost'}
                        disabled={row.version === loadedVersion}
                        onClick={() => setViewVersion(row.version)}
                        title={t('workflow.loadVersionHint')}
                      >
                        {row.version === loadedVersion ? t('workflow.versionLoaded') : t('workflow.loadVersion')}
                      </Button>
                      {row.change_reason && <span className="w-full text-graphite-500">{row.change_reason}</span>}
                    </li>
                  ))}
                </ul>
              )
            }
          </Async>
        </Card>

        <Card title={t('workflow.issues')} dense>
          {!validation && <EmptyState message={t('workflow.validateHint')} />}
          {validation && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-1">
                <Badge tone={validation.is_valid ? 'ok' : 'danger'}>
                  {validation.is_valid
                    ? t('workflow.valid')
                    : t('workflow.validityError', { count: validationIssues.errors.length })}
                </Badge>
                {validationIssues.warnings.length > 0 && (
                  <Badge tone="warning">
                    {t('workflow.validityWarning', { count: validationIssues.warnings.length })}
                  </Badge>
                )}
                <span className="text-[11px] text-graphite-500">
                  {t('workflow.graphCounts', {
                    nodes: validation.node_count,
                    edges: validation.edge_count,
                    entry: validation.entry_nodes.length,
                    exit: validation.exit_nodes.length,
                  })}
                </span>
              </div>
              {validation.issues.length === 0 ? (
                <EmptyState message={t('workflow.noIssues')} />
              ) : (
                <ul className="space-y-1 text-[11px]">
                  {validation.issues.map((issue, index) => (
                    <li key={index} className="rounded border border-graphite-100 p-1.5 dark:border-graphite-800">
                      <Badge tone={issue.severity === 'error' ? 'danger' : 'warning'}>{issue.severity}</Badge>{' '}
                      <span className="font-mono">{issue.code}</span>
                      <span className="block">{issue.message}</span>
                      {issue.node_id && (
                        <span className="font-mono text-graphite-500">
                          {t('workflow.issueNode', { id: issue.node_id })}
                        </span>
                      )}
                      {issue.edge_id && (
                        <span className="font-mono text-graphite-500">
                          {t('workflow.issueEdge', { id: issue.edge_id })}
                        </span>
                      )}
                      {issue.details && Object.keys(issue.details).length > 0 && (
                        <Json value={issue.details} max={24} />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

export default function WorkflowStudio() {
  const { t } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedId = searchParams.get('workflow')
  const [tab, setTab] = useState('studio')
  const [newName, setNewName] = useState('')
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null)

  // The studio reports its dirty state upwards so switching workflows can ask before discarding.
  const [dirty, setDirty] = useState(false)
  const dirtyRef = useRef(false)
  dirtyRef.current = dirty

  const workflows = useQuery({ queryKey: ['workflows'], queryFn: ({ signal }) => drillingApi.listWorkflows(signal) })
  const nodeTypes = useQuery({ queryKey: ['node-types'], queryFn: ({ signal }) => drillingApi.nodeTypes(signal) })

  const create = useMutation({
    mutationFn: () =>
      drillingApi.createWorkflow({
        key: newName.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 60) || 'workflow',
        name: newName || 'New workflow',
        description: 'Created in Workflow Studio',
      }),
    onSuccess: (workflow) => {
      setNewName('')
      workflows.refetch()
      setSearchParams({ workflow: workflow.id })
    },
  })

  const selected = workflows.data?.items.find((item) => item.id === selectedId) ?? null

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('workflow.title')}
        description="Graphs are validated server-side; the palette comes from the node registry, so the editor can never offer a node the runtime cannot execute."
        actions={
          <Link to="/runs">
            <Button size="sm">{t('workflow.runMonitor')}</Button>
          </Link>
        }
      />

      <Card title={t('workflow.definition')} dense>
        <div className="flex flex-wrap items-end gap-2">
          <label className="min-w-[240px] flex-1">
            <span className="mb-1 block text-xs font-medium text-graphite-600 dark:text-graphite-300">
              Workflows
            </span>
            <select
              aria-label="Workflows"
              value={selectedId ?? ''}
              onChange={(event) => {
                const next = event.target.value
                if (dirtyRef.current && next !== selectedId) {
                  setPendingSwitch(next)
                  return
                }
                setSearchParams(next ? { workflow: next } : {})
              }}
              className="w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            >
              <option value="">{t('workflow.selectWorkflow')}</option>
              {(workflows.data?.items ?? []).map((workflow) => (
                <option key={workflow.id} value={workflow.id}>
                  {workflow.name} ({workflow.status} · v{workflow.current_version}
                  {workflow.published_version_id ? ', published' : ', unpublished'})
                </option>
              ))}
            </select>
          </label>
          <Field label={t('workflow.name')}>
            <input
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              placeholder="Daily Drilling Intelligence"
              className="w-64 rounded border border-graphite-300 px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            />
          </Field>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            {t('workflow.createWorkflow')}
          </Button>
          {create.error && <ErrorState error={create.error} />}
        </div>
        {workflows.data && workflows.data.total === 0 && (
          <p className="mt-2 text-[11px] text-graphite-500">{t('empty.noRuns')}</p>
        )}

        {pendingSwitch !== null && (
          <div
            role="dialog"
            aria-modal="true"
            aria-label={t('workflow.unsavedTitle')}
            className="mt-2 rounded border border-warning/50 bg-amber-50/70 p-2.5 dark:bg-amber-950/20"
          >
            <p className="text-xs">{t('workflow.unsavedBody')}</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="danger"
                onClick={() => {
                  const next = pendingSwitch
                  setDirty(false)
                  setPendingSwitch(null)
                  setSearchParams(next ? { workflow: next } : {})
                }}
              >
                {t('workflow.discardAndSwitch')}
              </Button>
              <Button size="sm" onClick={() => setPendingSwitch(null)}>
                {t('workflow.stayHere')}
              </Button>
            </div>
          </div>
        )}
      </Card>

      <Tabs
        tabs={[
          { key: 'studio', label: t('workflow.canvas') },
          {
            key: 'catalogue',
            label: t('workflow.palette'),
            badge: nodeTypes.data && <Badge tone="neutral">{nodeTypes.data.total}</Badge>,
          },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'studio' && (
        <Async query={workflows}>
          {() =>
            selected ? (
              <ReactFlowProvider>
                <StudioBody workflow={selected} onDirtyChange={setDirty} />
              </ReactFlowProvider>
            ) : (
              <EmptyState
                message={t('workflow.noWorkflowSelected')}
                hint="A workflow run executes the published version of this graph on the server."
              />
            )
          }
        </Async>
      )}

      {tab === 'catalogue' && (
        <Card title={t('workflow.palette')} subtitle={t('workflow.paletteHint')}>
          <Async query={nodeTypes}>
            {(data) => (
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {data.items.map((spec) => (
                  <div key={spec.key} className="rounded border border-graphite-100 p-2 dark:border-graphite-800">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-medium">{spec.name}</span>
                      <Badge tone="info">{formatActionLevel(spec.action_level)}</Badge>
                    </div>
                    <p className="font-mono text-[10px] text-graphite-500">{spec.key}</p>
                    <p className="mt-1 text-[11px] text-graphite-600 dark:text-graphite-300">{spec.description}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <Badge tone="neutral">{spec.family}</Badge>
                      {spec.produces.map((port) => (
                        <Badge key={port} tone="ok">
                          {port}
                        </Badge>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Async>
        </Card>
      )}

      {create.isPending && <Loading />}
    </div>
  )
}
