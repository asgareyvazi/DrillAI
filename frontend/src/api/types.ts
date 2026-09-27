/**
 * API response types — mirrors of the backend contract.
 *
 * Hand-maintained mirrors of the FastAPI responses, checked against the running server rather than
 * guessed. List endpoints return `{items, total, …}` envelopes; single-resource endpoints return the
 * resource. Where the backend reports "not computed" the contract carries the *reason* as data
 * (for example `not_evaluated`), so no component can silently substitute a default for a number the
 * platform never calculated.
 */

// --------------------------------------------------------------------------- shared

export type ActionLevel = 'L0' | 'L1' | 'L2' | 'L3' | 'L4' | 'L5'

export interface Page<T> {
  items: T[]
  total: number
  limit?: number
  offset?: number
}

export interface FieldProvenance {
  /** Where a value came from: table/field, engine output, document region, or user entry. */
  source: string
  /** Optional human label for the source. */
  source_kind?: string
  observed_at?: string | null
  confidence?: number | null
  evidence_ref?: string | null
}

// --------------------------------------------------------------------------- assets

export interface Well {
  id: string
  project_id: string | null
  name: string
  uwi: string | null
  well_type: string
  status: string
  spud_date: string | null
  operator: string | null
  is_offshore: boolean
  kb_elevation_si: number | null
  total_depth_planned_si: number | null
  twin_state: string
  objectives: string | null
  target_formations: Array<Record<string, unknown>>
  tags: string[]
}

export interface Wellbore {
  id: string
  well_id: string
  name: string
  purpose?: string | null
  sequence: number
  status: string
  planned_td_md_si: number | null
  planned_td_tvd_si: number | null
  is_active?: boolean
}

export interface WellSection {
  id: string
  wellbore_id: string
  sequence: number
  name: string
  kind: string
  status: string
  hole_diameter_si: number | null
  hole_diameter_nominal: string | null
  planned_top_md_si: number | null
  planned_bottom_md_si: number | null
  actual_top_md_si: number | null
  actual_bottom_md_si: number | null
  current_md_si: number | null
  casing_od_nominal: string | null
  casing_shoe_md_si: number | null
  mud_weight_si: number | null
  pore_pressure_gradient_si: number | null
  fracture_gradient_si: number | null
  is_planned_only: boolean
}

export interface Project {
  id: string
  name: string
  code: string | null
  operator: string | null
  country: string | null
  basin: string | null
  status: string
  phase: string
  description: string | null
  well_count?: number
}

// --------------------------------------------------------------------------- drilling state

/** A value read from the well's records, with where it came from. Verified against the API. */
export interface MeasuredValue {
  key: string
  label: string
  value: number | string
  unit: string | null
  source: string
  source_id: string | null
  observed_at: string | null
  quality: string | null
  evidence_ref: string | null
  note: string | null
}

export interface WellProgress {
  wellbore_id: string | null
  wellbore_name: string | null
  current_md_si: number | null
  /** Which recorded field the current depth came from — never assumed. */
  current_md_source: string | null
  planned_td_md_si: number | null
  planned_bottom_md_si: number | null
  actual_bottom_md_si: number | null
  depth_variance_si: number | null
  percent_planned_depth: number | null
  percent_basis: string | null
  current_section: WellSection | null
  section_count: number
  sections_started: number
  source: string
  note: string | null
}

export interface OperationSummary {
  id: string
  name: string
  code: string | null
  kind: string | null
  phase: string | null
  status: string
  operation_class: string | null
  sequence: number | null
  planned_start: string | null
  planned_end: string | null
  actual_start: string | null
  actual_end: string | null
  planned_duration_hours: number | null
  actual_duration_hours: number | null
  duration_variance_hours: number | null
  npt_hours: number | null
  depth_from_md_si: number | null
  depth_to_md_si: number | null
  is_productive: boolean | null
  data_quality: string | null
  source: string | null
}

export interface OperationWindow {
  current: OperationSummary | null
  previous: OperationSummary | null
  next: OperationSummary | null
  next_basis: string
  next_note: string | null
  current_basis: string | null
  recent: OperationSummary[]
}

/**
 * One row of the NPT Pareto (verified against `GET /wells/{id}/npt`).
 *
 * `operator_controllable` is tri-state: `true`, `false`, or `null` when the loss was recorded but its
 * controllability was never established. The UI must not fold `null` into either other bucket.
 */
export interface NptCategoryRow {
  key: string
  label: string
  hours: number
  occurrences: number
  percent_of_total: number
  cumulative_percent: number
  operator_controllable: boolean | null
}

export interface NptCase {
  id: string
  source: string
  title: string
  category: string
  subcategory: string | null
  code: string | null
  cause: string | null
  hours: number
  started_at: string | null
  ended_at: string | null
  depth_md_si: number | null
  section_id: string | null
  operation_id: string | null
  operator_controllable: boolean | null
  confidence: number | null
  classification_source: string
  evidence_ref: string | null
  document_id: string | null
  note: string | null
}

export interface NptSummary {
  total_hours: number
  basis: string
  hours_from_events: number
  hours_from_operations: number
  event_count: number
  measured_hours_total: number
  percent_of_well_time: number | null
  by_hours: number
  controllable_hours: number
  uncontrollable_hours: number
  unknown_controllability_hours: number
  cases: NptCase[]
  by_category: NptCategoryRow[]
  by_code: NptCategoryRow[]
  by_section: NptCategoryRow[]
  by_operation: NptCategoryRow[]
  by_month: Array<{ month: string; hours: number }>
  offset_comparison: Record<string, unknown> | null
  notes: string[]
}

/** The headline NPT carried inside the well-state bundle — deliberately smaller than the summary. */
export interface NptHeadline {
  total_hours: number
  hours_from_operations: number
  event_count: number
  percent_of_well_time: number | null
  measured_hours_total: number
  note: string | null
}

export interface RiskItem {
  key: string
  label: string
  severity: string
  detail: string
  basis: string | null
  evidence_ref: string | null
}

/** Something the platform knows it does not have, and how to supply it. */
export interface MissingItem {
  key: string
  description: string
  why_it_matters: string
  how_to_supply: string
}

export interface TwinAspectSummary {
  id: string
  well_id: string
  wellbore_id: string | null
  section_id: string | null
  aspect: string
  state_kind: string
  schema_key: string
  schema_version: number
  payload: Record<string, unknown>
  summary: string | null
  confidence: number | null
  data_quality: string | null
  computed_at: string
  computed_by: string
  engine_run_id: string | null
  source_refs: string[]
  evidence_refs: string[]
  assumptions: string[]
  is_current: boolean
  content_hash: string
  valid_from: string
  valid_to: string | null
  supersedes_id: string | null
}

export interface DrillingState {
  well: Well
  progress: WellProgress
  operation: OperationWindow
  measured: MeasuredValue[]
  npt: NptHeadline
  twin: { aspects: string[]; aspect_count: number }
  risks: RiskItem[]
  documents: { documents: number; awaiting_ocr: number }
  counts: Record<string, number>
  missing: MissingItem[]
  generated_at?: string
}

export interface TwinState {
  well_id: string
  twin_state: string
  aspects: TwinAspectSummary[]
  current_state: Record<string, Record<string, TwinAspectSummary>>
}

export interface TimelineEntry {
  kind: string
  id: string
  at: string | null
  end_at: string | null
  title: string
  summary: string | null
  status: string | null
  category: string | null
  operation_id: string | null
  section_id: string | null
  depth_md_si: number | null
  duration_hours: number | null
  severity: string | null
  npt: boolean
  npt_hours: number | null
  evidence_refs: string[]
  document_id: string | null
  links: Record<string, string>
  attributes?: Record<string, unknown>
}

// --------------------------------------------------------------------------- documents & evidence

export interface DocumentRow {
  id: string
  project_id: string | null
  well_id: string | null
  wellbore_id: string | null
  section_id: string | null
  doc_type: string
  title: string
  document_number: string | null
  revision: string | null
  issue_date: string | null
  period_start: string | null
  period_end: string | null
  language: string | null
  page_count: number
  status: string
  extraction_summary: {
    pages: number
    regions: number
    chunks: number
    records: number
    evidence_links: number
    extractors: Record<string, string>
    doc_type: string
  }
  has_tables: boolean
  has_figures: boolean
  is_demo_fixture: boolean
  created_at: string
}

export interface EvidenceItem {
  id: string
  subject_kind: string
  subject_id: string
  evidence_kind: string
  evidence_id: string
  document_id: string
  region_id: string | null
  page_number: number | null
  well_id: string | null
  locator: Record<string, unknown>
  excerpt: string | null
  confidence: number | null
  relevance: number | null
  weight: number | null
  method: string | null
  quote_verified: boolean
  created_at: string
}

export interface EvidenceSummary {
  link_count: number
  verified_quotes: number
  documents_referenced: number
  mean_confidence: number | null
  by_kind: Record<string, number>
  limitations: string[]
}

export interface PromotionPlanRow {
  kind: string
  target: string
  count: number
  records: Array<Record<string, unknown>>
  skipped?: Array<Record<string, unknown>>
  reason?: string | null
}

export interface DdrProcessingReport {
  document_id: string
  well_id: string | null
  dry_run: boolean
  report_date: string | null
  doc_type: string
  operations_created: number
  events_created: number
  survey_stations: number
  npt_events: number
  npt_hours_classified: number
  records_promoted: number
  records_needing_review: number
  twin_aspects_updated: string[]
  not_promoted: PromotionPlanRow[]
  warnings: string[]
}

// --------------------------------------------------------------------------- engines

export interface EngineListItem {
  key: string
  name: string
  version: string
  domain_pack: string
  category: string
  summary: string
  consumes: string[]
  produces: string[]
  parameters: Record<string, unknown>
  assumptions: string[]
  limitations: string[]
  references: string[]
  tags: string[]
  deterministic: boolean
  action_level: ActionLevel
  requires_well_context: boolean
  validation_status: string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
}

export interface EngineRunListItem {
  id: string
  engine_key: string
  engine_version: string
  status: string
  subject_kind: string
  subject_id: string
  well_id: string | null
  wellbore_id: string | null
  section_id: string | null
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  inputs_hash: string
  outputs_hash: string
  assumptions: string[]
  limitations: string[]
  warnings: string[]
  constraint_violations: Array<Record<string, unknown>>
  is_feasible: boolean | null
  input_source_kind: string
  input_source_id: string | null
  triggered_by: string
  workflow_run_id: string | null
  node_run_id: string | null
  duration_ms: number | null
  created_at: string
}

export interface EngineRunEnvelope {
  engine_run_id: string
  engine: { key: string; version: string; validation_status: string }
  outputs: Record<string, unknown>
  warnings: string[]
  constraint_violations: Array<Record<string, unknown>>
  is_feasible: boolean
  assumptions: string[]
  limitations: string[]
  inputs_hash: string
  outputs_hash: string
}

// --------------------------------------------------------------------------- optimisation

export interface CandidateView {
  candidate_id: string
  values: Record<string, number>
  objectives: Record<string, number>
  rank: number | null
  on_frontier: boolean
  score: number | null
  feasible: boolean
  violations: Array<Record<string, unknown>>
  rejected_because: string | null
}

export interface ObjectiveComparison {
  candidate: number | null
  recommended: number | null
  sense: string
  unit?: string | null
  delta?: number | null
}

export interface WhyNotEntry {
  candidate: string
  candidate_id: string
  kind: string
  reason: string
  comparison: Record<string, ObjectiveComparison>
  violations?: Array<Record<string, unknown>>
}

export interface ObjectiveDefinition {
  key: string
  label: string
  sense: string
  source: string
  unit?: string | null
  description?: string
}

export interface OptimisationExplanation {
  optimization_run_id: string
  recommendation_id: string | null
  title: string | null
  problem_key: string
  created_at: string
  candidates_evaluated: number
  feasible_count: number
  pareto_count: number
  recommended: CandidateView | null
  pareto_frontier: CandidateView[]
  available_options: CandidateView[]
  why_not: WhyNotEntry[]
  objective_sources: Record<string, Record<string, unknown>>
  trade_offs: Array<Record<string, unknown>>
  sensitivities: Array<Record<string, unknown>>
  infeasible: Array<Record<string, unknown>>
  engine_run_ids: string[]
  engine_versions: Record<string, string>
  not_evaluated: Record<string, string>
  notes: string[]
}

export interface OptimisationObjectives {
  computable: ObjectiveDefinition[]
  not_evaluated: Record<string, string>
  note: string
}

export interface OptimisationResult {
  optimization_run_id: string
  recommendation_id: string | null
  explanation: OptimisationExplanation
  not_evaluated: Record<string, string>
}

// --------------------------------------------------------------------------- recommendations

export interface RecommendationRow {
  id: string
  domain: string
  kind: string
  title: string
  statement: string
  parameters: Array<Record<string, unknown>>
  rationale: string | null
  why_not: Array<Record<string, unknown>>
  assumptions: string[]
  constraints_applied: Array<Record<string, unknown>>
  alternatives: Array<Record<string, unknown>>
  sensitivities: Record<string, unknown>
  uncertainty: Record<string, unknown>
  confidence: number | null
  confidence_basis: string | null
  data_quality: string | null
  status: string
  action_level: ActionLevel
  engine_run_ids: string[]
  workflow_run_id: string | null
  offset_well_ids: string[]
  depth_from_md_si: number | null
  depth_to_md_si: number | null
  created_by: string
  created_by_kind: string
  created_at: string
}

// --------------------------------------------------------------------------- advisor & reports

export interface AdvisorAnswer {
  well_id: string
  question: string
  question_text: string
  generated_at: string
  facts: Array<Record<string, unknown>>
  calculations: Array<Record<string, unknown>>
  evidence: Array<Record<string, unknown>>
  inference: Array<Record<string, unknown>>
  recommendation: Array<Record<string, unknown>>
  unknown: Array<Record<string, unknown>>
  narrative: string | null
  narrative_model: string | null
  sources: string[]
}

export interface AdvisorQuestion {
  key: string
  description: string
}

export interface AdvisorQuestionCatalogue {
  questions: AdvisorQuestion[]
  contract: Record<string, string>
}

export interface ReportSection {
  key: string
  title: string
  kind: 'facts' | 'calculations' | 'evidence' | 'recommendations' | 'assumptions'
  items: Array<Record<string, unknown>>
  empty_reason: string | null
}

export interface Report {
  kind: string
  title: string
  well_id: string
  period_from: string | null
  period_to: string | null
  generated_at: string
  sections: ReportSection[]
  sources: string[]
  limitations: string[]
}

export interface ReportKind {
  key: string
  description: string
}

// --------------------------------------------------------------------------- dependencies

export interface DependencyNode {
  key: string
  kind: string
  label: string
  action_level?: string | null
  validation_status?: string | null
}

export interface DependencyEdge {
  from: string
  to: string
  port: string
}

export interface DependencyGraph {
  nodes: DependencyNode[]
  edges: DependencyEdge[]
  ports: string[]
  generated_from: string
}

export interface ImpactEntry {
  engine_key: string
  engine_name: string
  version: string
  stale: boolean
  reason: string
  ran_before: boolean
  last_run_at: string | null
  outputs_hash: string | null
  affected_outputs: string[]
  recommended_inputs: string[]
}

export interface ImpactReport {
  well_id: string
  changed_ports: string[]
  unknown_ports: string[]
  affected: ImpactEntry[]
  recommendations_stale: boolean
  notes: string[]
}

// --------------------------------------------------------------------------- workflows

export interface Workflow {
  id: string
  key?: string | null
  name: string
  description: string | null
  status: string
  version: number
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

export interface WorkflowGraphNode {
  id: string
  type: string
  name?: string | null
  config?: Record<string, unknown>
  inputs?: Record<string, unknown>
  on_error?: string
  retry?: Record<string, unknown>
  timeout_seconds?: number | null
  is_breakpoint?: boolean
  notes?: string | null
  position?: { x: number; y: number } | null
  action_level?: string | null
}

export interface WorkflowGraph {
  nodes: WorkflowGraphNode[]
  edges: Array<{
    id?: string | null
    source: string
    target: string
    source_handle?: string | null
    target_handle?: string | null
    condition?: string | null
    label?: string | null
    is_loop_back?: boolean
  }>
  variables?: Record<string, unknown>
  settings?: Record<string, unknown>
  version?: number
  description?: string | null
}

export interface WorkflowValidateIssue {
  code: string
  message: string
  severity: 'error' | 'warning'
  node_id: string | null
  edge_id?: string | null
  details?: Record<string, unknown>
}

export interface WorkflowValidation {
  is_valid: boolean
  issues: WorkflowValidateIssue[]
  errors: WorkflowValidateIssue[]
  warnings: WorkflowValidateIssue[]
  order?: string[]
}

export interface NodeTypeSpec {
  key: string
  name: string
  family: string
  description: string
  version: string
  action_level: ActionLevel
  terminal: boolean
  required_inputs: string[]
  produces: string[]
  tags: string[]
  config_schema: Record<string, unknown> | null
}

export interface NodeTypeCatalogue {
  items: NodeTypeSpec[]
  families: Record<string, string[]>
  total: number
}

export interface RunSummary {
  id: string
  workflow_id: string
  workflow_version_id?: string | null
  workflow_key?: string | null
  version: number
  status: string
  trigger_type: string
  project_id: string | null
  well_id: string | null
  wellbore_id?: string | null
  operation_id?: string | null
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  variables: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  step_count: number
  error: string | null
  error_node_id: string | null
  cursor_node_id: string | null
  pending_approval_id: string | null
  is_dry_run: boolean
  initiated_by: string | null
  [key: string]: unknown
}

export interface NodeRunState {
  id: string
  run_id?: string
  node_id: string
  node_type: string
  name?: string | null
  status: string
  attempt?: number
  started_at: string | null
  finished_at: string | null
  outputs?: Record<string, unknown>
  error?: Record<string, unknown> | null
  engine_run_id?: string | null
  llm_call_id?: string | null
  tool_call_id?: string | null
  approval_id?: string | null
  [key: string]: unknown
}

export interface RunEvent {
  id: string
  run_id: string
  node_id: string | null
  type: string
  message: string | null
  level: string
  seq: number
  occurred_at: string | null
  created_at?: string | null
  payload?: Record<string, unknown>
  [key: string]: unknown
}

export interface RunStepResult {
  run_id: string
  status: string
  executed?: string[]
  awaiting_approval?: string | null
  finished?: boolean
  paused?: boolean
  [key: string]: unknown
}

export interface ApprovalRow {
  id: string
  run_id: string
  node_id: string
  title: string | null
  status: string
  action_level: ActionLevel
  requested_at: string
  requested_by?: string | null
  decided_by?: string | null
  decided_at?: string | null
  comment?: string | null
  payload?: Record<string, unknown>
  [key: string]: unknown
}

// --------------------------------------------------------------------------- platform

export interface PlatformCapabilities {
  engines: { total: number; by_category: Record<string, number> }
  node_types: number
  tools: number
  actions: number
  context_sections: number
  extractors: number
  agents: number
  skills: number
  task_profiles: string[]
  integrations: Record<string, string | boolean>
  authorization: Record<string, unknown>
  [key: string]: unknown
}

export interface PlatformAgent {
  key: string
  name: string
  description: string
  version: string
  role_hint: string
  skills: string[]
  tools: string[]
  engines: string[]
  context_purpose: string
  context_sections: string[]
  llm_profile: string
  required_permission: string
  max_action_level: ActionLevel
  guardrails: string[]
  prompt_template_key: string | null
  limits: string[]
  tags: string[]
}

export interface PlatformSkill {
  key: string
  name: string
  description: string
  version: string
  kind: string
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  tools: string[]
  engines: string[]
  context_sections: string[]
  required_permission: string
  action_level: ActionLevel
  limits: string[]
  tags: string[]
}

export interface AgentCatalogue {
  agents: PlatformAgent[]
  skills: PlatformSkill[]
  max_agent_autonomy: string
}

export interface ProviderCatalogue {
  items: Array<{ name: string; privacy_class: string; models: Array<Record<string, unknown>> }>
  total: number
  configured_provider: string | null
}

export interface IntegrationState {
  [key: string]: unknown
}

export interface PlatformActions {
  items: Array<{ key: string; level: ActionLevel; description: string; permission: string }>
  total: number
  levels: Record<string, string>
}

export interface PlatformTools {
  items: Array<{
    key: string
    name: string
    description: string
    version: string
    permission: string
    action_level: ActionLevel
    side_effects: string[]
    idempotent: boolean
    requires_approval: boolean
    tags: string[]
    inputs: Record<string, unknown>
    outputs: Record<string, unknown> | null
  }>
  total: number
}

export interface PlatformExtractors {
  items: Array<{ key: string; name: string; version: string; doc_types: string[] }>
  total: number
}

export interface UnitRow {
  symbol: string
  name: string
  dimension: string
  factor: number
  offset: number
  kind: string
  aliases: string[]
  notes: string
  custom_conversion: boolean
}

export interface UnitCatalogue {
  items: UnitRow[]
  total: number
  dimensions: Array<{ dimension: string; canonical_unit: string }>
}

export interface AuditTrail {
  well_id: string
  engine_runs: Array<{
    id: string
    engine_key: string
    engine_version: string
    status: string
    is_feasible: boolean | null
    inputs_hash: string
    outputs_hash: string
    triggered_by: string
    created_at: string
  }>
  workflow_runs: Array<Record<string, unknown>>
  node_runs: Array<Record<string, unknown>>
  tool_calls: Array<Record<string, unknown>>
  llm_calls: Array<Record<string, unknown>>
  counts: Record<string, number>
}

export interface ContextSectionSummary {
  key: string
  title: string
  items: Array<Record<string, unknown>>
  empty_reason: string | null
  truncated: boolean
  omitted_items: number
  source_kind: string
  source_ids: string[]
  notes: string[]
}

export interface ContextBundle {
  bundle_id: string
  scope: Record<string, unknown>
  purpose: string
  unit_system: string
  locale: string
  built_at: string
  sections: ContextSectionSummary[]
  token_estimate: number
  token_budget: number
  truncated: boolean
  assumptions?: string[]
}
