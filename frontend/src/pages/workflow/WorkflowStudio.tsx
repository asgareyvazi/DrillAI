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
  MiniMap,
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
import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type { NodeTypeSpec, Workflow, WorkflowGraph, WorkflowValidation } from '../../api/types'
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
import { formatActionLevel } from '../../lib/format'

interface StudioNodeData extends Record<string, unknown> {
  label: string
  nodeType: string
  family: string
  actionLevel: string
  config: Record<string, unknown>
  notes?: string | null
}

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

function graphToFlow(graph: WorkflowGraph): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: graph.nodes.map((node, index) => ({
      id: node.id,
      type: 'studio',
      position: node.position ?? { x: 80 + (index % 4) * 240, y: 80 + Math.floor(index / 4) * 140 },
      data: {
        label: node.name ?? node.type,
        nodeType: node.type,
        // The registry decorates family and action level after load; a saved graph does not carry
        // them, so the placeholder is replaced as soon as the catalogue arrives.
        family: 'logic',
        actionLevel: 'L0',
        config: node.config ?? {},
        notes: node.notes ?? null,
      } satisfies StudioNodeData,
    })),
    edges: graph.edges.map((edge, index) => ({
      id: edge.id ?? `e${index}`,
      source: edge.source,
      target: edge.target,
      label: edge.condition ?? undefined,
    })),
  }
}

function flowToGraph(nodes: Node[], edges: Edge[], variables: Record<string, unknown>): WorkflowGraph {
  return {
    nodes: nodes.map((node) => {
      const data = node.data as StudioNodeData
      return {
        id: node.id,
        type: data.nodeType,
        name: data.label,
        config: data.config ?? {},
        position: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
      }
    }),
    edges: edges.map((edge) => ({ id: edge.id, source: edge.source, target: edge.target })),
    variables,
  }
}

function StudioBody({ workflow }: { workflow: Workflow }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [nodes, setNodes] = useState<Node[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [variables, setVariables] = useState<Record<string, unknown>>({})
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [validation, setValidation] = useState<WorkflowValidation | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const nodeTypesCatalogue = useQuery({
    queryKey: ['node-types'],
    queryFn: () => drillingApi.nodeTypes(),
  })
  const graph = useQuery({
    queryKey: ['workflow-graph', workflow.id],
    queryFn: () => drillingApi.getWorkflow(workflow.id),
  })

  // The saved graph is loaded once into editor state; afterwards the editor is the source of truth
  // until the user saves or re-validates.
  const loadedGraph = graph.data?.graph ?? null
  const [initialised, setInitialised] = useState(false)
  if (!initialised && loadedGraph) {
    const flow = graphToFlow(loadedGraph)
    setNodes(flow.nodes)
    setEdges(flow.edges)
    setVariables(loadedGraph.variables ?? {})
    setInitialised(true)
  }

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
    setEdges((current) => addEdge(connection, current))
  }, [])

  const save = useMutation({
    mutationFn: () =>
      drillingApi.saveWorkflowGraph(workflow.id, {
        graph: flowToGraph(nodes, edges, variables),
        change_reason: 'edited in Workflow Studio',
      }),
    onSuccess: () => {
      setMessage('Graph saved as a new version.')
      queryClient.invalidateQueries({ queryKey: ['workflow-graph', workflow.id] })
      queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  const validate = useMutation({
    mutationFn: () => drillingApi.validateWorkflow(flowToGraph(nodes, edges, variables)),
    onSuccess: (data) => setValidation(data.validation),
  })

  const startRun = useMutation({
    mutationFn: () =>
      drillingApi.startRun(workflow.id, {
        well_id: (workflow as Record<string, unknown>).well_id as string | undefined,
      }),
    onSuccess: (run) => navigate(`/runs?run=${run.id}`),
    onError: (error) => setMessage(error instanceof Error ? error.message : String(error)),
  })

  const selectedNode = nodes.find((node) => node.id === selectedId) ?? null
  const selectedSpec = selectedNode ? specByKey[(selectedNode.data as StudioNodeData).nodeType] : undefined

  function addNode(spec: NodeTypeSpec) {
    const id = `${spec.key.replace(/\./g, '_')}_${nodes.length + 1}`
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

      <Card
        title={t('workflow.canvas')}
        subtitle={`${nodes.length} nodes · ${edges.length} edges`}
        dense
        actions={
          <>
            <Button size="sm" onClick={() => validate.mutate()} disabled={validate.isPending}>
              {t('workflow.validate')}
            </Button>
            <Button size="sm" variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
              {t('workflow.save')}
            </Button>
            <Button size="sm" onClick={() => startRun.mutate()} disabled={startRun.isPending}>
              {t('workflow.run')}
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
            onNodeClick={(_, node) => setSelectedId(node.id)}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
        {message && <p className="p-2 text-[11px] text-graphite-600 dark:text-graphite-300">{message}</p>}
        {save.error && <ErrorState error={save.error} />}
        {startRun.error && <ErrorState error={startRun.error} />}
      </Card>

      <div className="space-y-3">
        <Card title={t('workflow.nodeConfig')} dense>
          {!selectedNode && <EmptyState message={t('workflow.selectNode')} />}
          {selectedNode && selectedSpec && (
            <div className="space-y-2">
              <Field label="Node id">
                <input
                  value={selectedNode.id}
                  readOnly
                  className="w-full rounded border border-graphite-200 bg-graphite-50 px-2 py-1 font-mono text-xs dark:border-graphite-800 dark:bg-graphite-950"
                />
              </Field>
              <Field label="Node type">
                <input
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
                  required inputs: {selectedSpec.required_inputs.join(', ')}
                </p>
              )}
              <Field label="Label">
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
              <Field label="Configuration (JSON)" hint="Validated by the node type's own schema on the server.">
                <textarea
                  rows={8}
                  spellCheck={false}
                  value={JSON.stringify((selectedNode.data as StudioNodeData).config ?? {}, null, 2)}
                  onChange={(event) => {
                    try {
                      const parsed = JSON.parse(event.target.value) as Record<string, unknown>
                      setNodes((current) =>
                        current.map((node) =>
                          node.id === selectedNode.id ? { ...node, data: { ...node.data, config: parsed } } : node,
                        ),
                      )
                    } catch {
                      // Keep the user's text while it is not yet valid JSON: rewriting their input
                      // mid-keystroke is worse than refusing to parse it.
                    }
                  }}
                  className="w-full rounded border border-graphite-300 bg-graphite-50 p-2 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-950"
                />
              </Field>
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

        <Card title={t('workflow.issues')} dense>
          {!validation && <EmptyState message="Validate to see graph issues." />}
          {validation && (
            <div className="space-y-2">
              <Badge tone={validation.is_valid ? 'ok' : 'danger'}>
                {validation.is_valid ? 'valid' : `${validation.errors.length} error(s)`}
              </Badge>
              {validation.issues.length === 0 ? (
                <EmptyState message="No issues were reported." />
              ) : (
                <ul className="space-y-1 text-[11px]">
                  {validation.issues.map((issue, index) => (
                    <li key={index} className="rounded border border-graphite-100 p-1.5 dark:border-graphite-800">
                      <Badge tone={issue.severity === 'error' ? 'danger' : 'warning'}>{issue.severity}</Badge>{' '}
                      <span className="font-mono">{issue.code}</span>
                      <span className="block">{issue.message}</span>
                      {issue.node_id && <span className="font-mono text-graphite-500">node {issue.node_id}</span>}
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

  const workflows = useQuery({ queryKey: ['workflows'], queryFn: () => drillingApi.listWorkflows() })
  const nodeTypes = useQuery({ queryKey: ['node-types'], queryFn: () => drillingApi.nodeTypes() })

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
              value={selectedId ?? ''}
              onChange={(event) => setSearchParams({ workflow: event.target.value })}
              className="w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900"
            >
              <option value="">— select a workflow —</option>
              {(workflows.data?.items ?? []).map((workflow) => (
                <option key={workflow.id} value={workflow.id}>
                  {workflow.name} ({workflow.status} v{workflow.version})
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
                <StudioBody workflow={selected} />
              </ReactFlowProvider>
            ) : (
              <EmptyState
                message="Select or create a workflow to edit its graph."
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
