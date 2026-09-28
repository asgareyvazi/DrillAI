/**
 * Every backend call the UI makes, in one place.
 *
 * Components never build a URL or unwrap an envelope themselves. If an endpoint is missing here it
 * is not used by the UI — which is how we keep the frontend from inventing a contract.
 */

import { api } from './client'
import type {
  AdvisorAnswer,
  AdvisorQuestionCatalogue,
  AgentCatalogue,
  ApprovalRow,
  AuditTrail,
  ContextBundle,
  DdrProcessingReport,
  DocumentRow,
  DocumentDetail,
  DocumentProvenance,
  IngestionJob,
  DrillingState,
  EngineListItem,
  EngineRunEnvelope,
  EngineRunListItem,
  EvidenceItem,
  EvidenceSummary,
  ImpactReport,
  NodeRunState,
  RunDetail,
  PlatformIdentity,
  NodeTypeCatalogue,
  NptSummary,
  OptimisationExplanation,
  OptimisationObjectives,
  OptimisationResult,
  Page,
  PlatformActions,
  PlatformCapabilities,
  PlatformExtractors,
  PlatformTools,
  Project,
  ProviderCatalogue,
  RecommendationRow,
  Report,
  ReportKind,
  RunEvent,
  RunSummary,
  TimelineEntry,
  TwinState,
  UnitCatalogue,
  Well,
  WellSection,
  Wellbore,
  Workflow,
  WorkflowDetail,
  WorkflowVersionSaved,
  WorkflowPublished,
  WorkflowVersionHistoryRow,
  WorkflowGraph,
  WorkflowValidation,
} from './types'

const enc = encodeURIComponent

export interface OptimisePayload {
  parameters: Array<Record<string, unknown>>
  hydraulics_inputs: Record<string, unknown>
  torque_drag_inputs?: Record<string, unknown> | null
  limits: Record<string, number>
  objectives: Array<{ key: string; sense?: string }>
  section_id?: string | null
  samples: number
  title?: string | null
  persist_recommendation?: boolean
}

export const drillingApi = {
  // ------------------------------------------------------------------ assets
  listWells: (params: { project_id?: string; limit?: number; offset?: number } = {}) =>
    api.get<Page<Well>>('/wells', { query: params }),
  getWell: (wellId: string) => api.get<Well & { wellbores: Wellbore[] }>(`/wells/${enc(wellId)}`),
  listWellbores: (wellId: string) => api.get<Page<Wellbore>>(`/wells/${enc(wellId)}/wellbores`),
  listSections: (wellboreId: string) => api.get<Page<WellSection>>(`/wellbores/${enc(wellboreId)}/sections`),
  listProjects: () => api.get<Page<Project>>('/projects'),

  // ------------------------------------------------------------------ cockpit
  wellState: (wellId: string) => api.get<{ state: DrillingState }>(`/wells/${enc(wellId)}/state`),
  wellTimeline: (wellId: string, params: { kinds?: string[]; limit?: number } = {}) =>
    api.get<{ entries: TimelineEntry[]; count: number; kinds_available: string[] }>(
      `/wells/${enc(wellId)}/timeline`,
      { query: params },
    ),
  wellNpt: (wellId: string, params: { basis?: string; include_offsets?: boolean } = {}) =>
    api.get<{ npt: NptSummary }>(`/wells/${enc(wellId)}/npt`, { query: params }),
  wellKpis: (wellId: string) =>
    api.get<{ kpis: Array<Record<string, unknown>>; present_count: number; total: number }>(
      `/wells/${enc(wellId)}/kpis`,
    ),
  wellTwin: (wellId: string) => api.get<TwinState>(`/wells/${enc(wellId)}/twin`),
  wellAudit: (wellId: string) => api.get<AuditTrail>(`/wells/${enc(wellId)}/audit`),
  wellContext: (wellId: string, params: { purpose?: string } = {}) =>
    api.get<{ bundle: ContextBundle }>(`/wells/${enc(wellId)}/context`, { query: params }),
  wellRecommendations: (wellId: string) =>
    api.get<Page<RecommendationRow>>(`/wells/${enc(wellId)}/recommendations`),
  wellEngineRuns: (wellId: string, params: { engine_key?: string; limit?: number } = {}) =>
    api.get<Page<EngineRunListItem>>(`/wells/${enc(wellId)}/engine-runs`, { query: params }),

  // ------------------------------------------------------------------ documents & evidence
  listDocuments: (params: { well_id?: string; doc_type?: string } = {}) =>
    api.get<Page<DocumentRow>>('/documents', { query: params }),
  getDocument: (documentId: string) => api.get<DocumentDetail>(`/documents/${enc(documentId)}`),
  documentProvenance: (documentId: string) =>
    api.get<DocumentProvenance>(`/documents/${enc(documentId)}/provenance`),
  uploadDocument: async (
    file: File,
    fields: { well_id?: string; wellbore_id?: string; section_id?: string; doc_type?: string; title?: string },
  ) => {
    const form = new FormData()
    form.append('file', file)
    for (const [key, value] of Object.entries(fields)) if (value) form.append(key, value)
    const response = await fetch(`${import.meta.env.VITE_API_BASE ?? '/api/v1'}/documents`, {
      method: 'POST',
      body: form,
      credentials: 'include',
    })
    if (!response.ok) {
      const text = await response.text().catch(() => '')
      throw new Error(text || `upload failed with ${response.status}`)
    }
    return (await response.json()) as {
      document: DocumentRow
      /** The ingestion job, or `null` when the upload produced no job. */
      job: IngestionJob | null
      record_ids: string[]
      evidence_link_ids: string[]
    }
  },
  processDocument: (documentId: string, body: { document_id?: string; dry_run?: boolean } = {}) =>
    api.post<{ processing: DdrProcessingReport }>(`/documents/${enc(documentId)}/process`, body),
  listEvidence: (params: { well_id?: string; document_id?: string; limit?: number } = {}) =>
    api.get<Page<EvidenceItem>>('/evidence', { query: params }),
  evidenceSummary: (params: { well_id?: string } = {}) =>
    api.get<EvidenceSummary>('/evidence/summary', { query: params }),
  documentFileUrl: (documentId: string) => `/api/v1/documents/${enc(documentId)}/file`,

  // ------------------------------------------------------------------ engines
  listEngines: () => api.get<Page<EngineListItem>>('/registry/engines'),
  getEngine: (key: string) => api.get<EngineListItem>(`/registry/engines/${enc(key)}`),
  runEngine: (
    key: string,
    body: { inputs: Record<string, unknown>; well_id?: string; wellbore_id?: string; section_id?: string },
  ) => api.post<EngineRunEnvelope>(`/registry/engines/${enc(key)}/run`, body),

  // ------------------------------------------------------------------ optimisation
  optimisationObjectives: () => api.get<OptimisationObjectives>('/engineering/optimisation/objectives'),
  optimise: (wellId: string, body: OptimisePayload) =>
    api.post<{ optimisation: OptimisationResult }>(`/wells/${enc(wellId)}/engineering/optimise`, body),
  optimisationExplanation: (runId: string) =>
    api.get<{ explanation: OptimisationExplanation }>(`/engineering/optimisation/${enc(runId)}`),
  dependencies: () => api.get<{ graph: import('./types').DependencyGraph }>('/engineering/dependencies'),
  impact: (wellId: string, ports: string[]) =>
    api.get<{ impact: ImpactReport }>(`/wells/${enc(wellId)}/engineering/impact`, { query: { ports } }),

  // ------------------------------------------------------------------ advisor & reports
  advisorQuestions: () => api.get<AdvisorQuestionCatalogue>('/advisor/questions'),
  askAdvisor: (wellId: string, body: { question: string; use_llm?: boolean }) =>
    api.post<{ answer: AdvisorAnswer }>(`/wells/${enc(wellId)}/advisor`, body),
  reportKinds: () => api.get<{ kinds: ReportKind[]; note: string }>('/reports/kinds'),
  buildReport: (wellId: string, kind: string) =>
    api.get<{ report: Report }>(`/wells/${enc(wellId)}/reports/${enc(kind)}`),

  // ------------------------------------------------------------------ workflows
  listWorkflows: () => api.get<Page<Workflow>>('/workflows'),
  getWorkflow: (workflowId: string, params: { version?: number; include_graph?: boolean } = {}) =>
    api.get<WorkflowDetail>(`/workflows/${enc(workflowId)}`, { query: params }),
  identity: () => api.get<PlatformIdentity>('/platform/identity'),
  workflowVersions: (workflowId: string, params: { limit?: number } = {}) =>
    api.get<{ items: WorkflowVersionHistoryRow[]; total: number }>(`/workflows/${enc(workflowId)}/versions`, {
      query: params,
    }),
  createWorkflow: (body: {
    key: string
    name: string
    description?: string
    graph?: WorkflowGraph
    tags?: string[]
  }) => api.post<Workflow>('/workflows', body),
  saveWorkflowGraph: (
    workflowId: string,
    body: { graph: WorkflowGraph; notes?: string; change_reason?: string; publish?: boolean },
  ) => api.put<WorkflowVersionSaved>(`/workflows/${enc(workflowId)}/graph`, body),
  validateWorkflow: (graph: WorkflowGraph) =>
    api.post<{ validation: WorkflowValidation; summary: Record<string, unknown> }>(
      '/workflows/validate',
      graph as unknown as Record<string, unknown>,
    ),
  // The version is a query parameter, not a body: publishing is a statement about an existing row.
  publishWorkflow: (workflowId: string, version?: number) =>
    api.post<WorkflowPublished>(`/workflows/${enc(workflowId)}/publish`, undefined, {
      query: version === undefined ? {} : { version },
    }),
  // The run-start endpoint returns the run row directly, not wrapped in an envelope.
  startRun: (
    workflowId: string,
    body: {
      well_id?: string
      project_id?: string
      wellbore_id?: string
      section_id?: string
      operation_id?: string
      inputs?: Record<string, unknown>
      version?: number
      is_dry_run?: boolean
      trigger_type?: string
    },
  ) => api.post<RunSummary>(`/workflows/${enc(workflowId)}/runs`, body),
  listRuns: (params: { workflow_id?: string; well_id?: string; status?: string; limit?: number } = {}) =>
    api.get<Page<RunSummary>>('/runs', { query: params }),
  getRun: (runId: string) => api.get<RunDetail>(`/runs/${enc(runId)}`),
  runNodes: (runId: string) => api.get<Page<NodeRunState>>(`/runs/${enc(runId)}/nodes`),
  runEvents: (runId: string, params: { after_seq?: number } = {}) =>
    api.get<Page<RunEvent> & { after_seq: number }>(`/runs/${enc(runId)}/events`, { query: params }),
  // Resuming takes the approval id as a query parameter and returns the resumed run row.
  resumeRun: (runId: string, approvalId?: string) =>
    api.post<RunSummary>(`/runs/${enc(runId)}/resume`, undefined, {
      query: approvalId ? { approval_id: approvalId } : {},
    }),
  nodeTypes: () => api.get<NodeTypeCatalogue>('/registry/node-types'),
  /**
   * Approval requests.
   *
   * `status: 'any'` returns decided ones too, and `run_id` scopes the read to one run — that is how
   * the run monitor shows the decision a run went through *after* it was taken, which is the part of
   * the record an auditor actually needs.
   */
  listApprovals: (
    params: { status?: string; run_id?: string; well_id?: string; limit?: number } = { status: 'pending' },
  ) => api.get<Page<ApprovalRow>>('/approvals', { query: params }),
  /**
   * Record a human decision.
   *
   * The note goes out as `note` and comes back as `decision_note` — the server's names on each side
   * of the wire. Sending `comment` (as this client used to) is not an error, it is a silently dropped
   * field: the decision was recorded with no justification and nothing complained.
   */
  decideApproval: (
    approvalId: string,
    body: {
      decision: 'approved' | 'rejected'
      note?: string
      /** Conditions the decision is granted under, one statement per entry. */
      conditions?: string[]
      resume?: boolean
    },
  ) =>
    api.post<{ approval: ApprovalRow; resumed_run: RunSummary | null }>(
      `/approvals/${enc(approvalId)}/decide`,
      body,
    ),

  // ------------------------------------------------------------------ platform & registry
  capabilities: () => api.get<PlatformCapabilities>('/platform/capabilities'),
  agents: () => api.get<AgentCatalogue>('/platform/agents'),
  providers: () => api.get<ProviderCatalogue>('/platform/providers'),
  integrations: () => api.get<Record<string, unknown>>('/platform/integrations'),
  actions: () => api.get<PlatformActions>('/registry/actions'),
  tools: () => api.get<PlatformTools>('/registry/tools'),
  extractors: () => api.get<PlatformExtractors>('/registry/extractors'),
  units: () => api.get<UnitCatalogue>('/registry/units'),
  version: () => api.get<Record<string, unknown>>('/version'),
  healthReady: () => api.get<Record<string, unknown>>('/health/ready'),
}
