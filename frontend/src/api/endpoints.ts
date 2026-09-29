/**
 * Every backend call the UI makes, in one place.
 *
 * Components never build a URL or unwrap an envelope themselves. If an endpoint is missing here it
 * is not used by the UI — which is how we keep the frontend from inventing a contract.
 *
 * **Reads take a trailing `signal`.** React Query hands every query function a cancellation signal
 * that fires when the key changes, when the query is superseded by a newer one, or when the screen
 * unmounts. Passing it through to `fetch` is what makes a run switch cancel the previous run's
 * request instead of leaving it in flight, and it is why an aborted read is silent rather than an
 * error on screen. Mutations deliberately do not take one: a decision or a run that has been sent
 * must not be cancelled half-way by a re-render.
 *
 * The reads whose shape the UI itself depends on also carry a `validate` guard, so a response of the
 * wrong shape is reported as a protocol failure where it happens instead of crashing a component
 * somewhere else later.
 */

import { api, expect } from './client'
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
  listWells: (params: { project_id?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    api.get<Page<Well>>('/wells', { query: params, signal, validate: expect.paged() }),
  getWell: (wellId: string, signal?: AbortSignal) => api.get<Well & { wellbores: Wellbore[] }>(`/wells/${enc(wellId)}`, { signal }),
  listWellbores: (wellId: string, signal?: AbortSignal) => api.get<Page<Wellbore>>(`/wells/${enc(wellId)}/wellbores`, { signal, validate: expect.paged() }),
  listSections: (wellboreId: string, signal?: AbortSignal) => api.get<Page<WellSection>>(`/wellbores/${enc(wellboreId)}/sections`, { signal, validate: expect.paged() }),
  listProjects: (signal?: AbortSignal) => api.get<Page<Project>>('/projects', { signal, validate: expect.paged() }),

  // ------------------------------------------------------------------ cockpit
  wellState: (wellId: string, signal?: AbortSignal) =>
    api.get<{ state: DrillingState }>(`/wells/${enc(wellId)}/state`, {
      signal,
      validate: expect.object('state'),
    }),
  wellTimeline: (wellId: string, params: { kinds?: string[]; limit?: number } = {}, signal?: AbortSignal) =>
    api.get<{ entries: TimelineEntry[]; count: number; kinds_available: string[] }>(
      `/wells/${enc(wellId)}/timeline`, 
      { query: params, signal }),
  wellNpt: (wellId: string, params: { basis?: string; include_offsets?: boolean } = {}, signal?: AbortSignal) =>
    api.get<{ npt: NptSummary }>(`/wells/${enc(wellId)}/npt`, { query: params, signal }),
  wellKpis: (wellId: string, signal?: AbortSignal) =>
    api.get<{ kpis: Array<Record<string, unknown>>; present_count: number; total: number }>(
      `/wells/${enc(wellId)}/kpis`, { signal }),
  wellTwin: (wellId: string, signal?: AbortSignal) => api.get<TwinState>(`/wells/${enc(wellId)}/twin`, { signal }),
  wellAudit: (wellId: string, signal?: AbortSignal) => api.get<AuditTrail>(`/wells/${enc(wellId)}/audit`, { signal }),
  wellContext: (wellId: string, params: { purpose?: string } = {}, signal?: AbortSignal) =>
    api.get<{ bundle: ContextBundle }>(`/wells/${enc(wellId)}/context`, { query: params, signal }),
  wellRecommendations: (wellId: string, signal?: AbortSignal) =>
    api.get<Page<RecommendationRow>>(`/wells/${enc(wellId)}/recommendations`, { signal, validate: expect.paged() }),
  wellEngineRuns: (wellId: string, params: { engine_key?: string; limit?: number } = {}, signal?: AbortSignal) =>
    api.get<Page<EngineRunListItem>>(`/wells/${enc(wellId)}/engine-runs`, { query: params, signal, validate: expect.paged() }),

  // ------------------------------------------------------------------ documents & evidence
  listDocuments: (params: { well_id?: string; doc_type?: string } = {}, signal?: AbortSignal) =>
    api.get<Page<DocumentRow>>('/documents', { query: params, signal, validate: expect.paged() }),
  getDocument: (documentId: string, signal?: AbortSignal) => api.get<DocumentDetail>(`/documents/${enc(documentId)}`, { signal }),
  documentProvenance: (documentId: string, signal?: AbortSignal) =>
    api.get<DocumentProvenance>(`/documents/${enc(documentId)}/provenance`, { signal }),
  uploadDocument: async (
    file: File,
    fields: { well_id?: string; wellbore_id?: string; section_id?: string; doc_type?: string; title?: string },
  ) => {
    const form = new FormData()
    form.append('file', file)
    for (const [key, value] of Object.entries(fields)) if (value) form.append(key, value)
    // Through the client, not `fetch` by hand: an upload then carries the same request id, the same
    // timeout and the same typed error as every other call. Doing it manually — as this did — meant a
    // failed upload surfaced as `Error("404: Not Found")` with no status and no code for a screen to
    // branch on, and it was invisible to every error test in the suite.
    return api.upload<{
      document: DocumentRow
      /** The ingestion job, or `null` when the upload produced no job. */
      job: IngestionJob | null
      record_ids: string[]
      evidence_link_ids: string[]
    }>('/documents', form, { validate: expect.object('document') })
  },
  processDocument: (documentId: string, body: { document_id?: string; dry_run?: boolean } = {}) =>
    api.post<{ processing: DdrProcessingReport }>(`/documents/${enc(documentId)}/process`, body),
  listEvidence: (params: { well_id?: string; document_id?: string; limit?: number } = {}, signal?: AbortSignal) =>
    api.get<Page<EvidenceItem>>('/evidence', { query: params, signal, validate: expect.paged() }),
  evidenceSummary: (params: { well_id?: string } = {}, signal?: AbortSignal) =>
    api.get<EvidenceSummary>('/evidence/summary', { query: params, signal }),
  documentFileUrl: (documentId: string) => `/api/v1/documents/${enc(documentId)}/file`,

  // ------------------------------------------------------------------ engines
  listEngines: (signal?: AbortSignal) => api.get<Page<EngineListItem>>('/registry/engines', { signal, validate: expect.paged() }),
  getEngine: (key: string, signal?: AbortSignal) => api.get<EngineListItem>(`/registry/engines/${enc(key)}`, { signal }),
  runEngine: (
    key: string,
    body: { inputs: Record<string, unknown>; well_id?: string; wellbore_id?: string; section_id?: string },
  ) => api.post<EngineRunEnvelope>(`/registry/engines/${enc(key)}/run`, body),

  // ------------------------------------------------------------------ optimisation
  optimisationObjectives: (signal?: AbortSignal) => api.get<OptimisationObjectives>('/engineering/optimisation/objectives', { signal }),
  optimise: (wellId: string, body: OptimisePayload) =>
    api.post<{ optimisation: OptimisationResult }>(`/wells/${enc(wellId)}/engineering/optimise`, body),
  optimisationExplanation: (runId: string, signal?: AbortSignal) =>
    api.get<{ explanation: OptimisationExplanation }>(`/engineering/optimisation/${enc(runId)}`, { signal }),
  dependencies: (signal?: AbortSignal) => api.get<{ graph: import('./types').DependencyGraph }>('/engineering/dependencies', { signal }),
  impact: (wellId: string, ports: string[], signal?: AbortSignal) =>
    api.get<{ impact: ImpactReport }>(`/wells/${enc(wellId)}/engineering/impact`, { query: { ports }, signal }),

  // ------------------------------------------------------------------ advisor & reports
  advisorQuestions: (signal?: AbortSignal) => api.get<AdvisorQuestionCatalogue>('/advisor/questions', { signal }),
  askAdvisor: (wellId: string, body: { question: string; use_llm?: boolean }) =>
    api.post<{ answer: AdvisorAnswer }>(`/wells/${enc(wellId)}/advisor`, body),
  reportKinds: (signal?: AbortSignal) => api.get<{ kinds: ReportKind[]; note: string }>('/reports/kinds', { signal }),
  buildReport: (wellId: string, kind: string, signal?: AbortSignal) =>
    api.get<{ report: Report }>(`/wells/${enc(wellId)}/reports/${enc(kind)}`, { signal }),

  // ------------------------------------------------------------------ workflows
  listWorkflows: (signal?: AbortSignal) => api.get<Page<Workflow>>('/workflows', { signal, validate: expect.paged() }),
  getWorkflow: (workflowId: string, params: { version?: number; include_graph?: boolean } = {}, signal?: AbortSignal) =>
    api.get<WorkflowDetail>(`/workflows/${enc(workflowId)}`, { query: params, signal }),
  identity: (signal?: AbortSignal) => api.get<PlatformIdentity>('/platform/identity', { signal }),
  workflowVersions: (workflowId: string, params: { limit?: number } = {}, signal?: AbortSignal) =>
    api.get<{ items: WorkflowVersionHistoryRow[]; total: number }>(`/workflows/${enc(workflowId)}/versions`, {
      query: params, signal, validate: expect.paged() }),
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
  listRuns: (params: { workflow_id?: string; well_id?: string; status?: string; limit?: number } = {}, signal?: AbortSignal) =>
    api.get<Page<RunSummary>>('/runs', { query: params, signal, validate: expect.paged() }),
  getRun: (runId: string, signal?: AbortSignal) =>
    api.get<RunDetail>(`/runs/${enc(runId)}`, { signal, validate: expect.object('run') }),
  runNodes: (runId: string, signal?: AbortSignal) => api.get<Page<NodeRunState>>(`/runs/${enc(runId)}/nodes`, { signal, validate: expect.paged() }),
  runEvents: (runId: string, params: { after_seq?: number } = {}, signal?: AbortSignal) =>
    api.get<Page<RunEvent> & { after_seq: number }>(`/runs/${enc(runId)}/events`, { query: params, signal, validate: expect.paged() }),
  // Resuming takes the approval id as a query parameter and returns the resumed run row.
  resumeRun: (runId: string, approvalId?: string) =>
    api.post<RunSummary>(`/runs/${enc(runId)}/resume`, undefined, {
      query: approvalId ? { approval_id: approvalId } : {},
    }),
  nodeTypes: (signal?: AbortSignal) => api.get<NodeTypeCatalogue>('/registry/node-types', { signal }),
  /**
   * Approval requests.
   *
   * `status: 'any'` returns decided ones too, and `run_id` scopes the read to one run — that is how
   * the run monitor shows the decision a run went through *after* it was taken, which is the part of
   * the record an auditor actually needs.
   */
  listApprovals: (
    params: { status?: string; run_id?: string; well_id?: string; limit?: number } = { status: 'pending' },
    signal?: AbortSignal,
  ) =>
    api.get<Page<ApprovalRow>>('/approvals', { query: params, signal, validate: expect.paged() }),
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
  capabilities: (signal?: AbortSignal) => api.get<PlatformCapabilities>('/platform/capabilities', { signal }),
  agents: (signal?: AbortSignal) => api.get<AgentCatalogue>('/platform/agents', { signal }),
  providers: (signal?: AbortSignal) => api.get<ProviderCatalogue>('/platform/providers', { signal }),
  integrations: (signal?: AbortSignal) => api.get<Record<string, unknown>>('/platform/integrations', { signal }),
  actions: (signal?: AbortSignal) => api.get<PlatformActions>('/registry/actions', { signal }),
  tools: (signal?: AbortSignal) => api.get<PlatformTools>('/registry/tools', { signal }),
  extractors: (signal?: AbortSignal) => api.get<PlatformExtractors>('/registry/extractors', { signal }),
  units: (signal?: AbortSignal) => api.get<UnitCatalogue>('/registry/units', { signal }),
  version: (signal?: AbortSignal) => api.get<Record<string, unknown>>('/version', { signal }),
  healthReady: (signal?: AbortSignal) => api.get<Record<string, unknown>>('/health/ready', { signal }),
}
