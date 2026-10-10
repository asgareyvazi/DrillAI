/**
 * The mapping between the platform's graph contract and the editor's canvas.
 *
 * This lives outside the React component for one reason: saving a graph must never lose information
 * the editor does not display. A `GraphNode` carries `inputs`, `on_error`, `retry`, `timeout_seconds`,
 * `is_breakpoint`, `notes` and an action-level override; an `GraphEdge` carries a `condition`, a
 * `label`, handles and `is_loop_back`. The canvas shows a fraction of that, so a mapping that rebuilt
 * each node from the handful of fields it renders would silently delete the rest on the next save —
 * exactly the kind of quiet data loss a workflow editor must not have.
 *
 * The rule implemented here: **the editor copies forward everything it did not itself change.**
 */

import type { Edge, Node } from '@xyflow/react'
import type { WorkflowGraph, WorkflowGraphNode } from '../api/types'

/** What the canvas stores per node. The node's own unmodelled fields live in `source`. */
export interface StudioNodeData extends Record<string, unknown> {
  label: string
  nodeType: string
  family: string
  actionLevel: string
  config: Record<string, unknown>
  /** The graph node this canvas node came from, so untouched fields survive a save. */
  source?: WorkflowGraphNode
}

/**
 * Canvas-side handle names.
 *
 * A node has one input and one output handle. The names are the canvas's own wiring, not part of the
 * stored contract: `GraphEdge.source_handle`/`target_handle` are opaque metadata the runtime never
 * reads, and the definitions in the repository carry `null`. Mapping a null handle onto the canvas's
 * handle and back keeps the round trip lossless without inventing handle names in saved graphs.
 */
export const INPUT_HANDLE = 'in'
export const OUTPUT_HANDLE = 'out'

export interface StudioEdgeData extends Record<string, unknown> {
  /** The graph edge this canvas edge came from, so untouched fields survive a save. */
  source?: WorkflowGraph['edges'][number]
}

const DEFAULT_POSITION = (index: number) => ({
  x: 80 + (index % 4) * 240,
  y: 80 + Math.floor(index / 4) * 140,
})

export function graphToFlow(graph: WorkflowGraph): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: graph.nodes.map((node, index) => ({
      id: node.id,
      type: 'studio',
      position: node.position ?? DEFAULT_POSITION(index),
      data: {
        label: node.name ?? node.type,
        nodeType: node.type,
        // The registry fills family and action level in as soon as the catalogue arrives; a saved
        // graph does not carry them, so these are placeholders, never a claim about the node.
        family: 'logic',
        actionLevel: node.action_level ?? 'L0',
        config: node.config ?? {},
        source: node,
      } satisfies StudioNodeData,
    })),
    edges: graph.edges.map((edge, index) => ({
      id: edge.id ?? `e${index}`,
      source: edge.source,
      target: edge.target,
      // React Flow cannot draw or connect an edge without handles to anchor it to. The handle names
      // are the canvas's own; the stored values stay in `data.source` and are written back untouched.
      sourceHandle: OUTPUT_HANDLE,
      targetHandle: INPUT_HANDLE,
      label: edge.condition ?? edge.label ?? undefined,
      data: { source: edge } satisfies StudioEdgeData,
    })),
  }
}

/**
 * Rebuild the graph payload from the canvas.
 *
 * Nodes and edges that came from the server are copied forward field by field, with only the values
 * the editor owns (position, name, config, and the node set itself) replaced. Nodes created in the
 * editor get the contract's own defaults.
 */
export function flowToGraph(
  nodes: Node[],
  edges: Edge[],
  variables: Record<string, unknown>,
  base?: WorkflowGraph | null,
): WorkflowGraph {
  return {
    nodes: nodes.map((node) => {
      const data = node.data as StudioNodeData
      const original = data.source
      const rebuilt: WorkflowGraphNode = {
        ...(original ?? {}),
        id: node.id,
        type: data.nodeType,
        name: data.label,
        config: data.config ?? {},
        position: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
      }
      return rebuilt
    }),
    edges: edges.map((edge) => {
      const data = edge.data as StudioEdgeData | undefined
      const original = data?.source
      return {
        ...(original ?? {}),
        id: edge.id,
        source: edge.source,
        target: edge.target,
        // An explicit `label` on the canvas is the condition; only set it when the editor changed it.
        ...(edge.label !== undefined && edge.label !== (original?.condition ?? original?.label)
          ? { label: String(edge.label) }
          : {}),
      }
    }),
    variables,
    // Graph-level fields the editor does not edit are preserved rather than dropped.
    ...(base?.settings ? { settings: base.settings } : {}),
    ...(base?.description ? { description: base.description } : {}),
    ...(base?.version ? { version: base.version } : {}),
  }
}

/** A stable string used both for dirty detection and for showing the user what will be saved. */
export function graphFingerprint(nodes: Node[], edges: Edge[], variables: Record<string, unknown>, base?: WorkflowGraph | null): string {
  const graph = flowToGraph(nodes, edges, variables, base)
  return JSON.stringify({
    nodes: [...graph.nodes].sort((a, b) => a.id.localeCompare(b.id)),
    edges: [...graph.edges].sort((a, b) => `${a.source}->${a.target}`.localeCompare(`${b.source}->${b.target}`)),
    variables: Object.keys(graph.variables ?? {})
      .sort()
      .map((key) => [key, (graph.variables ?? {})[key]]),
  })
}

export function isGraphDirty(
  nodes: Node[],
  edges: Edge[],
  variables: Record<string, unknown>,
  loaded: WorkflowGraph | null,
): boolean {
  if (!loaded) return nodes.length > 0 || edges.length > 0
  const baseline = graphFingerprint(
    graphToFlow(loaded).nodes,
    graphToFlow(loaded).edges,
    loaded.variables ?? {},
    loaded,
  )
  return graphFingerprint(nodes, edges, variables, loaded) !== baseline
}
