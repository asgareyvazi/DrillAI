/**
 * Graph mapping: the contract the editor must not quietly break.
 *
 * The editor shows a fraction of what a graph node carries. The tests here are about the rest: a save
 * from the canvas must republish `inputs`, `on_error`, `retry`, `timeout_seconds`, `is_breakpoint`,
 * `notes`, `action_level`, edge `condition`/`label`/`is_loop_back` and graph `settings` unchanged,
 * because an editor that silently deletes half of a definition is worse than one that cannot edit it.
 */

import { describe, expect, it } from 'vitest'
import type { WorkflowGraph } from '../api/types'
import { flowToGraph, graphFingerprint, graphToFlow, isGraphDirty } from './workflowGraph'

const graph: WorkflowGraph = {
  nodes: [
    {
      id: 'load_context',
      type: 'data.load_context',
      name: 'Load engineering context',
      config: { purpose: 'workflow' },
      inputs: { well_id: '${run.well_id}' },
      on_error: 'retry',
      retry: { max_attempts: 3, backoff_seconds: 5 },
      timeout_seconds: 90,
      is_breakpoint: true,
      notes: 'kept from the server',
      action_level: 'L1',
      position: { x: 10, y: 20 },
    },
    { id: 'report', type: 'output.report', name: 'Report', config: {}, position: { x: 300, y: 20 } },
  ],
  edges: [
    {
      id: 'e1',
      source: 'load_context',
      target: 'report',
      condition: '${npt.total_hours} > 0',
      label: 'when NPT recorded',
      is_loop_back: true,
      source_handle: 'out',
    },
  ],
  variables: { threshold: 5 },
  settings: { max_steps: 200 },
  description: 'seeded definition',
  version: 3,
}

describe('graphToFlow', () => {
  it('keeps the server node as the source of every field the canvas does not show', () => {
    const { nodes, edges } = graphToFlow(graph)
    const data = nodes[0]?.data as { source?: Record<string, unknown> }
    expect(data.source).toMatchObject({
      inputs: { well_id: '${run.well_id}' },
      on_error: 'retry',
      retry: { max_attempts: 3, backoff_seconds: 5 },
      timeout_seconds: 90,
      is_breakpoint: true,
      notes: 'kept from the server',
      action_level: 'L1',
    })
    expect(edges[0]?.data).toMatchObject({ source: { condition: '${npt.total_hours} > 0', is_loop_back: true } })
  })

  it('falls back to a readable label when the node has no name', () => {
    const { nodes } = graphToFlow({ nodes: [{ id: 'n1', type: 'logic.branch' }], edges: [] })
    expect((nodes[0]?.data as { label: string }).label).toBe('logic.branch')
  })
})

describe('flowToGraph', () => {
  it('round-trips a graph without losing a single unmodelled field', () => {
    const flow = graphToFlow(graph)
    const rebuilt = flowToGraph(flow.nodes, flow.edges, graph.variables ?? {}, graph)

    expect(rebuilt.nodes[0]).toMatchObject({
      id: 'load_context',
      type: 'data.load_context',
      name: 'Load engineering context',
      config: { purpose: 'workflow' },
      inputs: { well_id: '${run.well_id}' },
      on_error: 'retry',
      retry: { max_attempts: 3, backoff_seconds: 5 },
      timeout_seconds: 90,
      is_breakpoint: true,
      notes: 'kept from the server',
      action_level: 'L1',
    })
    expect(rebuilt.edges[0]).toMatchObject({
      id: 'e1',
      source: 'load_context',
      target: 'report',
      condition: '${npt.total_hours} > 0',
      is_loop_back: true,
      source_handle: 'out',
    })
    expect(rebuilt.settings).toEqual({ max_steps: 200 })
    expect(rebuilt.description).toBe('seeded definition')
  })

  it('applies an edited label and an edited position and nothing else', () => {
    const flow = graphToFlow(graph)
    const edited = flow.nodes.map((node) =>
      node.id === 'load_context'
        ? { ...node, position: { x: 500, y: 600 }, data: { ...node.data, label: 'Renamed' } }
        : node,
    )
    const rebuilt = flowToGraph(edited, flow.edges, graph.variables ?? {}, graph)

    expect(rebuilt.nodes[0]).toMatchObject({ name: 'Renamed', position: { x: 500, y: 600 }, on_error: 'retry' })
    // The other node is untouched, so its stored position survives too.
    expect(rebuilt.nodes[1]?.position).toEqual({ x: 300, y: 20 })
  })

  it('creates a node the editor added with the contract defaults only', () => {
    const flow = graphToFlow(graph)
    const added = [
      ...flow.nodes,
      { id: 'new1', type: 'studio', position: { x: 5, y: 6 }, data: { label: 'New', nodeType: 'logic.branch', config: {} } },
    ]
    const rebuilt = flowToGraph(added, flow.edges, {}, graph)
    const created = rebuilt.nodes.find((node) => node.id === 'new1')
    expect(created).toEqual({
      id: 'new1',
      type: 'logic.branch',
      name: 'New',
      config: {},
      position: { x: 5, y: 6 },
    })
  })

  it('drops a node the editor deleted and keeps its sibling', () => {
    const flow = graphToFlow(graph)
    const rebuilt = flowToGraph(flow.nodes.slice(1), [], {}, graph)
    expect(rebuilt.nodes.map((node) => node.id)).toEqual(['report'])
  })
})

describe('isGraphDirty', () => {
  it('is false straight after load', () => {
    const flow = graphToFlow(graph)
    expect(isGraphDirty(flow.nodes, flow.edges, graph.variables ?? {}, graph)).toBe(false)
  })

  it('becomes true when a node moves, a label changes or an edge is added', () => {
    const flow = graphToFlow(graph)
    const moved = flow.nodes.map((node) => (node.id === 'load_context' ? { ...node, position: { x: 1, y: 1 } } : node))
    expect(isGraphDirty(moved, flow.edges, graph.variables ?? {}, graph)).toBe(true)

    const relabelled = flow.nodes.map((node) => ({ ...node, data: { ...node.data, label: 'x' } }))
    expect(isGraphDirty(relabelled, flow.edges, graph.variables ?? {}, graph)).toBe(true)

    const conectado = [...flow.edges, { id: 'e2', source: 'report', target: 'load_context' }]
    expect(isGraphDirty(flow.nodes, conectado, graph.variables ?? {}, graph)).toBe(true)
  })

  it('returns to false when the node is put back where it was', () => {
    const flow = graphToFlow(graph)
    const move = (to: { x: number; y: number }) =>
      flow.nodes.map((node) => (node.id === 'load_context' ? { ...node, position: to } : node))

    expect(isGraphDirty(move({ x: 1, y: 1 }), flow.edges, graph.variables ?? {}, graph)).toBe(true)
    expect(isGraphDirty(move({ x: 10, y: 20 }), flow.edges, graph.variables ?? {}, graph)).toBe(false)
  })

  it('stays dirty after a save until the editor is re-seeded from the server response', () => {
    // Saving produces a new version; the baseline only becomes that version when the server's own
    // graph is loaded back into the editor. Until then the local draft is still ahead of the record.
    const flow = graphToFlow(graph)
    const moved = flow.nodes.map((node) => (node.id === 'load_context' ? { ...node, position: { x: 1, y: 1 } } : node))
    const saved = flowToGraph(moved, flow.edges, graph.variables ?? {}, graph)
    const reloaded = graphToFlow(saved)
    expect(isGraphDirty(reloaded.nodes, reloaded.edges, graph.variables ?? {}, saved)).toBe(false)
    expect(isGraphDirty(reloaded.nodes, reloaded.edges, graph.variables ?? {}, graph)).toBe(true)
  })

  it('treats an empty editor over a real graph as dirty, and an empty graph as clean', () => {
    expect(isGraphDirty([], [], {}, graph)).toBe(true)
    expect(isGraphDirty([], [], {}, { nodes: [], edges: [] })).toBe(false)
  })
})

describe('graphFingerprint', () => {
  it('ignores node ordering, because ordering on the canvas is not a definition change', () => {
    const flow = graphToFlow(graph)
    const reordered = [...flow.nodes].reverse()
    expect(graphFingerprint(reordered, flow.edges, graph.variables ?? {}, graph)).toBe(
      graphFingerprint(flow.nodes, flow.edges, graph.variables ?? {}, graph),
    )
  })
})
