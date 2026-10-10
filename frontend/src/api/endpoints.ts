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
import { engineRunEnvelope } from './engineRun'
import type {
  AdvisorAnswer,
  AdvisorQuestionCatalogue,
  AgentCatalogue,
  AlertEvaluationReport,
  AlertEvidence,
  AlertRow,
  AlertRule,
  AlertRuleCreatePayload,
  AlertRuleUpdatePayload,
  AlertsPage,
  ApprovalRow,
  AuditTrail,
  ContextBundle,
  DdrProcessingReport,
  AuditLogEntry,
  DocumentRow,
  DocumentDetail,
  DocumentProvenance,
  IngestionJob,
  DrillingState,
  EngineListItem,
  EngineRunListItem,
  EvidenceItem,
  EvidenceSummary,
  Field,
  ImpactReport,
  NodeRunState,
  RunDetail,
  PlatformIdentity,
  NodeTypeCatalogue,
  NptSummary,
  OperationRow,
  EventRow,
  TimelinePage,
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
  SyntheticCommissionPayload,
  SyntheticCommissionResponse,
  TelemetryChannel,
  TelemetryChannelCreatePayload,
  TelemetryIngestReport,
  TelemetryPointsBatchPayload,
  TelemetryWindowResponse,
  WellLatestTelemetryResponse,
  WellLiveSnapshot,
  Rig,
  ReportKind,
  RunEvent,
  RunSummary,
  TwinState,
  UnitCatalogue,
  Well,
  WellSection,
  WellStructure,
  Wellbore,
  WellboreLineage,
  WithTransitions,
  Workflow,
  WorkflowDetail,
  WorkflowVersionSaved,
  WorkflowPublished,
  WorkflowVersionHistoryRow,
  WorkflowGraph,
  WorkflowValidation,
} from './types'

const enc = encodeURIComponent

/**
 * The idempotency header for a mutation.
 *
 * The key is generated once per form submission by the caller and reused across retries, so a create
 * whose response was lost can be safely sent again: the server returns the original result rather than
 * performing the write twice.
 */
function idempotencyHeader(key?: string): { idempotencyKey?: string } {
  return key ? { idempotencyKey: key } : {}
}

export interface ProjectCreatePayload {
  name: string
  code?: string | null
  operator?: string | null
  country?: string | null
  basin?: string | null
  phase?: string | null
  description?: string | null
}

export interface ProjectUpdatePayload extends Partial<ProjectCreatePayload> {
  expected_updated_at?: string | null
}

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

/**
 * What a create sends for a well.
 *
 * `status` and `twin_state` are absent on purpose: a well is created planned and moves by transition,
 * and twin state is written by the twin. The service refuses both, so offering them here would be a
 * contract this client cannot honour.
 */
export interface WellCreatePayload {
  project_id: string
  name: string
  field_id?: string | null
  uwi?: string | null
  api_number?: string | null
  well_type?: string
  operator?: string | null
  rig_id?: string | null
  is_offshore?: boolean
  surface_lat?: number | null
  surface_lon?: number | null
  kb_elevation_si?: number | null
  ground_elevation_si?: number | null
  water_depth_si?: number | null
  elevation_datum?: string
  slot?: string | null
  pad_name?: string | null
  total_depth_planned_si?: number | null
  spud_date?: string | null
  objectives?: string | null
  target_formations?: string[]
  tags?: string[]
}

/** A partial edit. `expected_updated_at` is the version the form was rendered from. */
export interface WellUpdatePayload extends Partial<Omit<WellCreatePayload, 'project_id'>> {
  /** A well is released when the rig moves off it; the API records it separately from the spud date. */
  release_date?: string | null
  reason?: string | null
  expected_updated_at?: string | null
}

export interface FieldCreatePayload {
  project_id: string
  name: string
  country?: string | null
  basin?: string | null
  water_depth_si?: number | null
  centroid_lat?: number | null
  centroid_lon?: number | null
  notes?: string | null
  aliases?: string[]
}

export interface FieldUpdatePayload extends Partial<Omit<FieldCreatePayload, 'project_id'>> {
  expected_updated_at?: string | null
}

export interface WellboreCreatePayload {
  name: string
  purpose?: string
  sequence?: number
  parent_wellbore_id?: string | null
  planned_td_md_si?: number | null
  planned_td_tvd_si?: number | null
  kickoff_md_si?: number | null
  datum?: string
}

export interface WellboreUpdatePayload {
  name?: string
  purpose?: string
  parent_wellbore_id?: string | null
  planned_td_md_si?: number | null
  planned_td_tvd_si?: number | null
  actual_td_md_si?: number | null
  actual_td_tvd_si?: number | null
  kickoff_md_si?: number | null
  datum?: string
  reason?: string | null
  expected_updated_at?: string | null
}

export interface SectionCreatePayload {
  sequence: number
  name: string
  kind?: string
  hole_diameter_nominal?: string | null
  hole_diameter_si?: number | null
  planned_top_md_si?: number | null
  planned_bottom_md_si?: number | null
  actual_top_md_si?: number | null
  actual_bottom_md_si?: number | null
  current_md_si?: number | null
}

/** A plan revision, or a recorded as-drilled measurement. `is_planned_only` is derived, not sent. */
export interface SectionUpdatePayload {
  name?: string
  kind?: string
  hole_diameter_si?: number | null
  hole_diameter_nominal?: string | null
  planned_top_md_si?: number | null
  planned_bottom_md_si?: number | null
  actual_top_md_si?: number | null
  actual_bottom_md_si?: number | null
  current_md_si?: number | null
  casing_od_si?: number | null
  casing_od_nominal?: string | null
  casing_weight_si?: number | null
  casing_grade?: string | null
  casing_connection?: string | null
  casing_top_md_si?: number | null
  casing_shoe_md_si?: number | null
  cement_top_md_si?: number | null
  cement_planned_top_md_si?: number | null
  mud_weight_si?: number | null
  mud_weight_min_si?: number | null
  mud_weight_max_si?: number | null
  pore_pressure_gradient_si?: number | null
  fracture_gradient_si?: number | null
  collapse_gradient_si?: number | null
  lot_fit_equivalent_mw_si?: number | null
  pressure_source?: string | null
  notes?: string | null
  expected_updated_at?: string | null
}

export const drillingApi = {
  // ------------------------------------------------------------------ assets
  listWells: (
    params: { project_id?: string; field_id?: string; q?: string; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) => api.get<Page<Well>>('/wells', { query: params, signal, validate: expect.paged() }),
  getWell: (wellId: string, signal?: AbortSignal) => api.get<Well & { wellbores: Wellbore[] }>(`/wells/${enc(wellId)}`, { signal }),
  /**
   * Create a well.
   *
   * The idempotency key is passed in by the caller and is stable for the life of one form submission:
   * a create that timed out on the way back must not become a second well when the operator retries.
   */
  createWell: (payload: WellCreatePayload, idempotencyKey?: string) =>
    api.post<Well & WithTransitions>('/wells', payload, idempotencyHeader(idempotencyKey)),
  updateWell: (wellId: string, payload: WellUpdatePayload, idempotencyKey?: string) =>
    api.patch<Well & WithTransitions>(`/wells/${enc(wellId)}`, payload, idempotencyHeader(idempotencyKey)),
  /**
   * Transition a well's lifecycle.
   *
   * The target is checked against the server's transition table — the screen offers only the states
   * the server listed in `allowed_transitions`, so this cannot become a client-side life-cycle model.
   */
  transitionWell: (wellId: string, body: { target: string; reason?: string | null }, idempotencyKey?: string) =>
    api.post<Well & WithTransitions>(`/wells/${enc(wellId)}/lifecycle`, body, idempotencyHeader(idempotencyKey)),
  assignRig: (
    wellId: string,
    body: { rig_id: string | null; reason?: string | null },
    idempotencyKey?: string,
  ) => api.post<Well>(`/wells/${enc(wellId)}/rig`, body, idempotencyHeader(idempotencyKey)),
  wellAuditLog: (wellId: string, params: { limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    api.get<Page<AuditLogEntry>>(`/wells/${enc(wellId)}/audit-log`, { query: params, signal, validate: expect.paged() }),
  wellStructure: (wellId: string, signal?: AbortSignal) =>
    api.get<WellStructure>(`/wells/${enc(wellId)}/structure`, { signal, validate: expect.object('well') }),
  listFields: (
    params: { project_id?: string; q?: string; limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) => api.get<Page<Field>>('/fields', { query: params, signal, validate: expect.paged() }),
  createField: (payload: FieldCreatePayload, idempotencyKey?: string) =>
    api.post<Field>('/fields', payload, idempotencyHeader(idempotencyKey)),
  updateField: (fieldId: string, payload: FieldUpdatePayload, idempotencyKey?: string) =>
    api.patch<Field>(`/fields/${enc(fieldId)}`, payload, idempotencyHeader(idempotencyKey)),
  listRigs: (signal?: AbortSignal) => api.get<Page<Rig>>('/rigs', { signal, validate: expect.paged() }),
  listWellbores: (wellId: string, signal?: AbortSignal) => api.get<Page<Wellbore>>(`/wells/${enc(wellId)}/wellbores`, { signal, validate: expect.paged() }),
  /** Create a wellbore in a well. A sidetrack must name its parent; the server checks the lineage. */
  createWellbore: (wellId: string, payload: WellboreCreatePayload, idempotencyKey?: string) =>
    api.post<Wellbore & WithTransitions>(`/wells/${enc(wellId)}/wellbores`, payload, idempotencyHeader(idempotencyKey)),
  getWellbore: (wellboreId: string, signal?: AbortSignal) =>
    api.get<Wellbore & WithTransitions>(`/wellbores/${enc(wellboreId)}`, { signal }),
  updateWellbore: (wellboreId: string, payload: WellboreUpdatePayload, idempotencyKey?: string) =>
    api.patch<Wellbore & WithTransitions>(
      `/wellbores/${enc(wellboreId)}`,
      payload,
      idempotencyHeader(idempotencyKey),
    ),
  /** Make this the hole being drilled. Activation is explicit; it is never a side effect of a create. */
  activateWellbore: (wellboreId: string, idempotencyKey?: string) =>
    api.post<Wellbore & WithTransitions>(
      `/wellbores/${enc(wellboreId)}/activate`,
      undefined,
      idempotencyHeader(idempotencyKey),
    ),
  transitionWellbore: (
    wellboreId: string,
    body: { target: string; reason?: string | null },
    idempotencyKey?: string,
  ) => api.post<Wellbore & WithTransitions>(`/wellbores/${enc(wellboreId)}/lifecycle`, body, idempotencyHeader(idempotencyKey)),
  wellboreLineage: (wellboreId: string, signal?: AbortSignal) =>
    api.get<WellboreLineage>(`/wellbores/${enc(wellboreId)}/lineage`, { signal, validate: expect.object('items') }),
  listSections: (wellboreId: string, signal?: AbortSignal) => api.get<Page<WellSection>>(`/wellbores/${enc(wellboreId)}/sections`, { signal, validate: expect.paged() }),
  createSection: (wellboreId: string, payload: SectionCreatePayload, idempotencyKey?: string) =>
    api.post<WellSection & WithTransitions>(
      `/wellbores/${enc(wellboreId)}/sections`,
      payload,
      idempotencyHeader(idempotencyKey),
    ),
  updateSection: (wellboreId: string, sectionId: string, payload: SectionUpdatePayload, idempotencyKey?: string) =>
    api.patch<WellSection & WithTransitions>(
      `/wellbores/${enc(wellboreId)}/sections/${enc(sectionId)}`,
      payload,
      idempotencyHeader(idempotencyKey),
    ),
  transitionSection: (
    wellboreId: string,
    sectionId: string,
    body: { target: string; reason?: string | null },
    idempotencyKey?: string,
  ) =>
    api.post<WellSection & WithTransitions>(
      `/wellbores/${enc(wellboreId)}/sections/${enc(sectionId)}/lifecycle`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  listProjects: (signal?: AbortSignal) => api.get<Page<Project>>('/projects', { signal, validate: expect.paged() }),
  createProject: (payload: ProjectCreatePayload, idempotencyKey?: string) =>
    api.post<Project>('/projects', payload, idempotencyHeader(idempotencyKey)),
  updateProject: (projectId: string, payload: ProjectUpdatePayload, idempotencyKey?: string) =>
    api.patch<Project>(`/projects/${enc(projectId)}`, payload, idempotencyHeader(idempotencyKey)),

  // ------------------------------------------------------------------ cockpit
  wellState: (wellId: string, signal?: AbortSignal) =>
    api.get<{ state: DrillingState }>(`/wells/${enc(wellId)}/state`, {
      signal,
      validate: expect.object('state'),
    }),
  wellTimeline: (
    wellId: string,
    params: {
      kinds?: string[]
      limit?: number
      /** The keyset position: pass back `next_cursor` from the previous page unchanged. */
      cursor?: string
      since?: string
      until?: string
    } = {},
    signal?: AbortSignal,
  ) =>
    api.get<TimelinePage>(`/wells/${enc(wellId)}/timeline`, {
      query: params,
      signal,
      validate: expect.object('entries'),
    }),
  wellNpt: (
    wellId: string,
    params: {
      basis?: string
      include_offsets?: boolean
      section_id?: string
      operation_id?: string
      since?: string
      until?: string
      case_limit?: number
    } = {},
    signal?: AbortSignal,
  ) => api.get<{ npt: NptSummary }>(`/wells/${enc(wellId)}/npt`, { query: params, signal }),
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

  // ------------------------------------------------------------------ operations & events
  listOperations: (
    params: {
      well_id?: string
      wellbore_id?: string
      section_id?: string
      operation_class?: string
      status?: string
      kind?: string
      source_kind?: string
      document_id?: string
      limit?: number
      offset?: number
    } = {},
    signal?: AbortSignal,
  ) => api.get<Page<OperationRow>>('/operations', { query: params, signal, validate: expect.paged() }),
  getOperation: (operationId: string, signal?: AbortSignal) =>
    api.get<OperationRow>(`/operations/${enc(operationId)}`, { signal }),
  updateOperation: (
    operationId: string,
    body: { expected_updated_at: string; reason: string; changes: Record<string, unknown> },
    idempotencyKey?: string,
  ) =>
    api.patch<OperationRow & WithTransitions>(
      `/operations/${enc(operationId)}`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  transitionOperation: (
    operationId: string,
    body: { status: string; reason?: string; expected_updated_at?: string },
    idempotencyKey?: string,
  ) =>
    api.post<OperationRow & WithTransitions>(
      `/operations/${enc(operationId)}/transition`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  linkOperationDocument: (operationId: string, body: { document_id: string }, idempotencyKey?: string) =>
    api.post<OperationRow>(
      `/operations/${enc(operationId)}/document`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  listEvents: (
    params: {
      well_id?: string
      wellbore_id?: string
      operation_id?: string
      kind?: string
      status?: string
      severity?: string
      npt_category?: string
      source_kind?: string
      is_npt?: boolean
      document_id?: string
      limit?: number
      offset?: number
    } = {},
    signal?: AbortSignal,
  ) => api.get<Page<EventRow>>('/events', { query: params, signal, validate: expect.paged() }),
  getEvent: (eventId: string, signal?: AbortSignal) => api.get<EventRow>(`/events/${enc(eventId)}`, { signal }),
  updateEvent: (
    eventId: string,
    body: { expected_updated_at: string; reason: string; changes: Record<string, unknown> },
    idempotencyKey?: string,
  ) =>
    api.patch<EventRow & WithTransitions>(`/events/${enc(eventId)}`, body, idempotencyHeader(idempotencyKey)),
  transitionEvent: (
    eventId: string,
    body: { status: string; reason?: string; expected_updated_at?: string },
    idempotencyKey?: string,
  ) =>
    api.post<EventRow & WithTransitions>(
      `/events/${enc(eventId)}/transition`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  linkEventDocument: (eventId: string, body: { document_id: string }, idempotencyKey?: string) =>
    api.post<EventRow>(`/events/${enc(eventId)}/document`, body, idempotencyHeader(idempotencyKey)),

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
  // The live run's body is adapted to the envelope the workspace renders — and checked while it is
  // adapted, so a rename on the server becomes a reported malformed response rather than a blank
  // panel. See `engineRun.ts` for why the two names exist.
  runEngine: (
    key: string,
    body: { inputs: Record<string, unknown>; well_id?: string; wellbore_id?: string; section_id?: string },
  ) =>
    api
      .post<unknown>(`/registry/engines/${enc(key)}/run`, body)
      .then((payload) => engineRunEnvelope(payload)),

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

  // ------------------------------------------------------------------ telemetry & live operations
  listTimeseries: (
    params: {
      well_id?: string
      wellbore_id?: string
      operation_id?: string
      channel_key?: string
      dimension?: string
      is_realtime?: boolean
      source?: string
      limit?: number
      offset?: number
    } = {},
    signal?: AbortSignal,
  ) => api.get<Page<TelemetryChannel>>('/timeseries', { query: params, signal, validate: expect.paged() }),
  createTimeseries: (body: TelemetryChannelCreatePayload, idempotencyKey?: string) =>
    api.post<TelemetryChannel>('/timeseries', body, idempotencyHeader(idempotencyKey)),
  getTimeseries: (seriesId: string, signal?: AbortSignal) =>
    api.get<TelemetryChannel>(`/timeseries/${enc(seriesId)}`, { signal }),
  getTimeseriesPoints: (
    seriesId: string,
    params: {
      start?: string
      end?: string
      limit?: number
      quality?: string
      downsample?: boolean
    } = {},
    signal?: AbortSignal,
  ) =>
    api.get<TelemetryWindowResponse>(`/timeseries/${enc(seriesId)}/points`, {
      query: params,
      signal,
      validate: expect.paged(),
    }),
  appendTimeseriesPoints: (
    seriesId: string,
    body: TelemetryPointsBatchPayload,
    idempotencyKey?: string,
  ) =>
    api.post<TelemetryIngestReport>(
      `/timeseries/${enc(seriesId)}/points`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  wellLatestTelemetry: (
    wellId: string,
    params: {
      wellbore_id?: string
      operation_id?: string
      channel_key?: string[]
      limit?: number
    } = {},
    signal?: AbortSignal,
  ) =>
    api.get<WellLatestTelemetryResponse>(`/wells/${enc(wellId)}/timeseries/latest`, {
      query: params,
      signal,
      validate: expect.paged(),
    }),
  commissionSyntheticTelemetry: (
    wellId: string,
    body: SyntheticCommissionPayload,
    idempotencyKey?: string,
  ) =>
    api.post<SyntheticCommissionResponse>(
      `/wells/${enc(wellId)}/timeseries/commission-synthetic`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  wellLiveSnapshot: (wellId: string, signal?: AbortSignal) =>
    api.get<WellLiveSnapshot>(`/wells/${enc(wellId)}/live/snapshot`, { signal }),

  // ------------------------------------------------------------------ alert rules & alerts
  listAlertRules: (
    params: {
      well_id?: string
      channel_key?: string
      enabled?: boolean
      limit?: number
      offset?: number
    } = {},
    signal?: AbortSignal,
  ) => api.get<Page<AlertRule>>('/alert-rules', { query: params, signal, validate: expect.paged() }),
  createAlertRule: (body: AlertRuleCreatePayload, idempotencyKey?: string) =>
    api.post<AlertRule>('/alert-rules', body, idempotencyHeader(idempotencyKey)),
  getAlertRule: (ruleId: string, signal?: AbortSignal) =>
    api.get<AlertRule>(`/alert-rules/${enc(ruleId)}`, { signal }),
  updateAlertRule: (ruleId: string, body: AlertRuleUpdatePayload, idempotencyKey?: string) =>
    api.patch<AlertRule>(`/alert-rules/${enc(ruleId)}`, body, idempotencyHeader(idempotencyKey)),
  listAlerts: (
    params: {
      well_id?: string
      wellbore_id?: string
      status?: string
      severity?: string
      rule_ref?: string
      series_id?: string
      since?: string
      until?: string
      limit?: number
      offset?: number
    } = {},
    signal?: AbortSignal,
  ) => api.get<AlertsPage>('/alerts', { query: params, signal, validate: expect.paged() }),
  getAlert: (alertId: string, signal?: AbortSignal) =>
    api.get<AlertRow>(`/alerts/${enc(alertId)}`, { signal }),
  alertEvidence: (
    alertId: string,
    params: { points?: number } = {},
    signal?: AbortSignal,
  ) =>
    api.get<AlertEvidence>(`/alerts/${enc(alertId)}/evidence`, {
      query: params,
      signal,
      validate: expect.object('alert'),
    }),
  acknowledgeAlert: (
    alertId: string,
    body: { expected_updated_at: string; reason?: string | null },
    idempotencyKey?: string,
  ) =>
    api.post<AlertRow>(
      `/alerts/${enc(alertId)}/acknowledge`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  clearAlert: (
    alertId: string,
    body: { expected_updated_at: string; reason: string; observed_value?: number | null },
    idempotencyKey?: string,
  ) =>
    api.post<AlertRow>(
      `/alerts/${enc(alertId)}/clear`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  cancelAlert: (
    alertId: string,
    body: { expected_updated_at: string; reason: string },
    idempotencyKey?: string,
  ) =>
    api.post<AlertRow>(
      `/alerts/${enc(alertId)}/cancel`,
      body,
      idempotencyHeader(idempotencyKey),
    ),
  evaluateWellAlerts: (
    wellId: string,
    params: { channel_id?: string[]; mode?: 'manual' | 'recovery' } = {},
  ) =>
    api.post<AlertEvaluationReport>(`/wells/${enc(wellId)}/alerts/evaluate`, undefined, {
      query: params,
    }),

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
