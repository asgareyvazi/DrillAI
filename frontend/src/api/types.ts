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

/**
 * The canonical well vocabularies, as the server defines them.
 *
 * These are unions rather than `string` because the vocabulary is a *contract*: `development` is a
 * word the platform deliberately does not use (`assets/vocabulary.py`), and a well type that is not
 * one of these is a server bug or a schema drift, not a value a screen should render.
 */
export type WellType =
  | 'exploration'
  | 'appraisal'
  | 'development_producer'
  | 'development_injector'
  | 'observation'
  | 'water_source'
  | 'disposal'
  | 'sidetrack'
  | 'reentry'

export type WellStatus =
  | 'planned'
  | 'permitting'
  | 'drilling'
  | 'completing'
  | 'producing'
  | 'shut_in'
  | 'intervention'
  | 'suspended'
  | 'abandoned'
  | 'p&a'

export type WellborePurpose =
  | 'original'
  | 'sidetrack'
  | 'bypass'
  | 'reentry'
  | 'reamed'
  | 'pilot'
  | 'contingency'

export type WellboreStatus = 'planned' | 'drilling' | 'suspended' | 'abandoned'
export type SectionStatus = 'planned' | 'drilling' | 'drilled' | 'abandoned'
export type SectionKind =
  | 'conductor'
  | 'surface'
  | 'intermediate'
  | 'production'
  | 'liner'
  | 'tieback'
  | 'open_hole'
  | 'rathole'

export type ElevationDatum = 'rkb' | 'msl' | 'gl' | 'cf' | 'derrick_floor'

/** What a recorded section number *is*. The server classifies each one; the UI never guesses. */
export type SectionNumberSemantics = 'plan' | 'actual' | 'computed' | 'interpreted' | 'progress'

/**
 * One well, as the master-data contract exposes it.
 *
 * `field_id` and `rig_id` are the two references a well carries; the location and datum block is
 * present because a depth without a datum is not a depth. The free-form `attributes` bag the row also
 * has is deliberately absent: nothing permanent lives there, and offering it would be a second,
 * unvalidated way to hold master data the columns already model.
 */
export interface Well {
  id: string
  project_id: string | null
  field_id: string | null
  name: string
  uwi: string | null
  api_number: string | null
  well_type: WellType
  status: WellStatus
  spud_date: string | null
  release_date: string | null
  operator: string | null
  rig_id: string | null
  is_offshore: boolean
  surface_lat: number | null
  surface_lon: number | null
  kb_elevation_si: number | null
  ground_elevation_si: number | null
  water_depth_si: number | null
  elevation_datum: ElevationDatum
  slot: string | null
  pad_name: string | null
  total_depth_planned_si: number | null
  twin_state: string
  objectives: string | null
  target_formations: string[]
  tags: string[]
  created_at?: string
  /**
   * The version a client read. It is sent back on an edit so the server can refuse a write that would
   * overwrite somebody else's; without it there is nothing for a stale write to be stale against.
   */
  updated_at?: string
}

export interface Wellbore {
  id: string
  well_id: string
  name: string
  purpose: WellborePurpose
  sequence: number
  /** The hole this one was drilled from. Lineage is structured; it is never inferred from a name. */
  parent_wellbore_id: string | null
  status: WellboreStatus
  is_active: boolean
  datum: ElevationDatum
  kickoff_md_si: number | null
  planned_td_md_si: number | null
  planned_td_tvd_si: number | null
  actual_td_md_si: number | null
  actual_td_tvd_si: number | null
  created_at?: string
  updated_at?: string
}

export interface WellSection {
  id: string
  wellbore_id: string
  sequence: number
  name: string
  kind: SectionKind
  status: SectionStatus
  hole_diameter_si: number | null
  hole_diameter_nominal: string | null
  planned_top_md_si: number | null
  planned_bottom_md_si: number | null
  actual_top_md_si: number | null
  actual_bottom_md_si: number | null
  /**
   * Where the hole is *now* — and `null` when nobody has recorded one. A planned bottom is never
   * substituted for it: an un-drilled section has no current depth, and saying otherwise is the most
   * misleading thing a depth readout can do.
   */
  current_md_si: number | null
  casing_od_si: number | null
  casing_od_nominal: string | null
  casing_weight_si: number | null
  casing_grade: string | null
  casing_connection: string | null
  casing_top_md_si: number | null
  casing_shoe_md_si: number | null
  cement_top_md_si: number | null
  cement_planned_top_md_si: number | null
  mud_weight_si: number | null
  mud_weight_min_si: number | null
  mud_weight_max_si: number | null
  pore_pressure_gradient_si: number | null
  fracture_gradient_si: number | null
  collapse_gradient_si: number | null
  lot_fit_equivalent_mw_si: number | null
  pressure_source: string | null
  is_planned_only: boolean
  notes: string | null
  /** Which recorded number is a plan, which is measured, which is interpreted. Sent by the server. */
  semantics: Partial<Record<string, SectionNumberSemantics>>
  created_at?: string
  updated_at?: string
}

/** A field: master data one level above the well, and one below the project. */
export interface Field {
  id: string
  project_id: string
  name: string
  aliases: string[]
  country: string | null
  basin: string | null
  water_depth_si: number | null
  centroid_lat: number | null
  centroid_lon: number | null
  status: string
  notes: string | null
  created_at?: string
  updated_at?: string
}

/** A rig, as reference data: enough to choose one and to see whether it is already working. */
export interface Rig {
  id: string
  name: string
  contractor: string | null
  rig_type: string | null
  status: string
  country: string | null
  current_well_id: string | null
}

/** One governance-ledger entry: who did what, to which record, and what changed. */
export interface AuditLogEntry {
  id: string
  occurred_at: string | null
  actor_kind: string
  actor_id: string | null
  actor_display: string | null
  action: string
  action_level: string
  resource_kind: string
  resource_id: string | null
  outcome: string
  permission_decision: Record<string, unknown>
  details: Record<string, unknown>
  before: Record<string, unknown>
  after: Record<string, unknown>
  request_id: string | null
}

/**
 * The states a resource may legally move to next, as the server computes them.
 *
 * The menu a screen renders comes from this list rather than from a second copy of the transition
 * table in the frontend — the drift that produces a button the API refuses.
 */
export interface WithTransitions {
  allowed_transitions?: string[]
}

export interface WellboreLineage {
  items: Array<Wellbore & WithTransitions>
  total: number
  root_id: string
  wellbore_id: string
}

export interface WellStructure {
  well: Well & WithTransitions & { active_wellbore_id: string | null }
  field: Field | null
  rig: Rig | null
  wellbores: Array<
    Wellbore & WithTransitions & { sections: WellSection[]; counts: Record<string, number> }
  >
  /** How much context hangs off the well in total, per kind. */
  counts: Record<string, number>
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
  /** A scan or image-only PDF: extraction produced nothing until OCR runs. */
  ocr_required: boolean
  /**
   * The stored bytes this document was read from. Two documents may share one artifact — the same
   * report filed against two wells — which is why identity is per document, not per file.
   */
  raw_artifact_id: string | null
  /** Content-derived identity: sha256 of the bytes, scope, type and revision, in that order. */
  logical_key: string | null
  is_demo_fixture: boolean
  created_at: string
}

/** One row an extractor produced, with the method and version that produced it. */
export interface ExtractionRecord {
  id: string
  document_id: string
  /** The extractor's record kind, e.g. `operation_row`, `report_header`, `mud_property`. */
  record_type: string
  payload: Record<string, unknown>
  payload_schema_key: string | null
  payload_schema_version: number | null
  page_number: number | null
  region_id: string | null
  depth_md_si: number | null
  depth_tvd_si: number | null
  observed_at: string | null
  /** How the row was read (`regex_header`, `table_row`, `label_value`, …) and by which version. */
  method: string | null
  method_version: string | null
  confidence: number | null
  /** `extracted` (a machine wrote it), `rule_validated`, `human_validated`, `rejected`, `superseded`. */
  validation_state: string
  /** True when the extractor could not tell which region the value came from; `region_id` is null. */
  region_unknown: boolean
  /** Which named rule accepted the row, when a rule did. Null for machine-only extraction. */
  validation_rule: string | null
  quality_flags: string[]
  unit_context: Record<string, string>
  promoted_to_kind: string | null
  promoted_to_id: string | null
  is_demo_fixture: boolean
}

export interface DocumentChunk {
  id: string
  chunk_index: number
  kind: string
  page_number: number | null
  /** The region of the page this chunk was cut from — the link that makes its text traceable. */
  region_id: string | null
  section_id: string | null
  depth_from_si: number | null
  depth_to_si: number | null
  text: string
  token_estimate: number | null
}

/** One ingestion attempt: what ran, what it produced, and whether it succeeded. */
export interface IngestionJob {
  id: string
  document_id: string
  raw_artifact_id: string | null
  status: string
  trigger: string
  pipeline: Record<string, unknown>
  stats: DocumentRow['extraction_summary']
  extractor_versions: Record<string, string>
  attempts: number
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  error: string | null
  warnings: string[]
  trace_id: string | null
}

export interface DocumentDetail {
  document: DocumentRow
  ingestion_jobs: IngestionJob[]
  region_count: number
  chunks: DocumentChunk[]
  records: ExtractionRecord[]
  evidence_links: EvidenceItem[]
}

/** region → extraction → record, as the backend builds it. */
export interface DocumentProvenanceLink {
  record: ExtractionRecord
  region: {
    id: string
    page_number: number | null
    region_kind: string
    bbox: Record<string, number> | null
    text_excerpt?: string | null
  } | null
  document: { id: string; title: string } | Record<string, unknown>
}

export interface DocumentProvenance {
  document_id: string
  extraction_summary: DocumentRow['extraction_summary']
  chain: DocumentProvenanceLink[]
  region_count: number
  record_count: number
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
  /**
   * Mechanical, not semantic: the excerpt was found in the text it claims to come from. The check
   * that produced it — `region_text`, `page_text`, `mismatch` or `unavailable` — is reported
   * alongside, because "not verified" and "could not be checked" are different failures.
   */
  quote_verified: boolean
  quote_check: string | null
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

/**
 * A workflow definition, exactly as `workflow_out` serialises it.
 *
 * Note what is *not* here: a `well_id`. A definition is org/project scoped; the well belongs to the
 * run. Reading `workflow.well_id` gave `undefined` and produced unscoped runs, so the studio now asks
 * for the run's context explicitly instead of guessing it from the definition.
 */
export interface Workflow {
  id: string
  key: string
  name: string
  description: string | null
  category: string | null
  domain_pack: string
  project_id: string | null
  status: string
  current_version: number
  published_version_id: string | null
  is_template: boolean
  is_system_default: boolean
  is_editable: boolean
  forked_from_id: string | null
  owner: string | null
  tags: string[]
  permissions: string[]
  action_level: string
  created_at: string | null
  updated_at: string | null
}

/** The version metadata `GET /workflows/{id}` returns alongside the graph. */
export interface WorkflowVersionSummary {
  id: string
  version: number
  graph_hash: string
  node_count: number
  edge_count: number
  published_at: string | null
  validation: Record<string, unknown> | null
  notes: string | null
}

/** `GET /workflows/{id}` — the definition, the version it resolved to, and that version's graph. */
export interface WorkflowDetail {
  workflow: Workflow
  version: WorkflowVersionSummary
  graph?: WorkflowGraph | null
  summary?: Record<string, unknown>
}

/** `PUT /workflows/{id}/graph` — what the server actually created. */
export interface WorkflowVersionSaved {
  id: string
  version: number
  graph_hash: string
  node_count: number
  edge_count: number
  validation: Record<string, unknown> | null
  published_at: string | null
}

/** `POST /workflows/{id}/publish`. */
export interface WorkflowPublished {
  id: string
  workflow_id: string
  version: number
  graph_hash: string
  published_at: string | null
}

/** One row of `GET /workflows/{id}/versions`. */
/**
 * One row of a definition's version history, field for field.
 *
 * The list deliberately does not carry the graph: a history is a list of identities (hash, counts,
 * who, when, whether it is the published one), while the graph of a chosen version is fetched on
 * demand. It carries the validation error count so a history can show which versions were saved in a
 * state the server would refuse to publish.
 */
export interface WorkflowVersionHistoryRow {
  id: string
  version: number
  graph_hash: string
  node_count: number
  edge_count: number
  published_at: string | null
  created_at: string | null
  created_by: string | null
  change_reason: string | null
  notes: string | null
  validation_errors: number
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

/**
 * The server's validation report, field for field.
 *
 * `is_valid` is a computed field on the backend model, so it is present on every report — the
 * editor's `POST /workflows/validate` answer and the report stored on a version row alike. The
 * error and warning counts are *not* in the payload: a reader that needs them filters `issues` by
 * `severity` rather than expecting the server to have split the list.
 */
export interface WorkflowValidation {
  is_valid: boolean
  issues: WorkflowValidateIssue[]
  node_count: number
  edge_count: number
  entry_nodes: string[]
  exit_nodes: string[]
  node_types: Record<string, number>
  families: Record<string, number>
}

export interface IdentityRole {
  key: string
  name: string
  description: string
  max_action_level: ActionLevel
}

/**
 * Who the caller is, as the server resolved it.
 *
 * `permissions` are the caller's own patterns and `available_roles` describes the catalogue; the UI
 * reads them to avoid offering actions that would be refused, and for nothing else — every request
 * is still authorized server-side. No credential material is ever returned by this endpoint.
 */
export interface PlatformIdentity {
  principal_id: string
  principal_kind: string
  org_id: string | null
  role_keys: string[]
  roles: IdentityRole[]
  available_roles: Array<IdentityRole & { permissions: string[] }>
  permissions: string[]
  max_action_level: ActionLevel
  auth_enabled: boolean
  identity_source: 'bearer_token' | 'development_header'
  development_presets: string[]
  locale: string
  note: string
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

/**
 * A run, as the server serializes it.
 *
 * Every field here is one the API actually sends. The scope columns (`project_id`, `well_id`,
 * `wellbore_id`, `section_id`, `operation_id`) are what the run was started against, and `context` is
 * the scope record written at start time — a monitor shows both so a reader can tell a run that had
 * no well from a run whose well was not displayed.
 */
export interface RunSummary {
  id: string
  workflow_id: string
  workflow_version_id: string | null
  workflow_key: string | null
  version: number | null
  status: string
  trigger_type: string
  project_id: string | null
  well_id: string | null
  wellbore_id: string | null
  section_id: string | null
  operation_id: string | null
  context: Record<string, unknown>
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
  approved_by: string | null
  metrics: Record<string, unknown>
}

/**
 * One execution of one node, as `GET /runs/{id}/nodes` and the run envelope return it.
 *
 * The label the server sends is `node_name`; an earlier version of this interface read `name`, which
 * silently fell back to the node id for every row.
 */
export interface NodeRunState {
  id: string
  run_id: string
  node_id: string
  node_type: string
  node_name: string | null
  status: string
  attempt?: number
  max_attempts?: number
  duration_ms?: number | null
  resolution?: string | null
  started_at: string | null
  finished_at: string | null
  outputs?: Record<string, unknown>
  error?: Record<string, unknown> | null
  engine_run_id?: string | null
  llm_call_id?: string | null
  tool_call_id?: string | null
  approval_id?: string | null
}

/**
 * A run with everything recorded alongside it.
 *
 * `GET /runs/{id}` answers with an envelope, not a bare run row: the run, the definition it executed,
 * the node executions, the artifacts, the event log, the approval it may be waiting for, and whether
 * it can be resumed. Reading that envelope as if it were the run itself is why the interface showed
 * missing fields.
 */
export interface RunDetail {
  run: RunSummary
  workflow: { id: string; key: string; name: string; status: string } | null
  node_runs: NodeRunState[]
  artifacts: Array<Record<string, unknown>>
  events?: RunEvent[]
  pending_approval?: ApprovalRow | null
  resumable: boolean
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

/**
 * An approval, field for field as the server serializes it.
 *
 * The request and the response do **not** use the same name for the same idea: a decision is sent as
 * `note` and comes back as `decision_note`. Both spellings are the server's, so both are here, and
 * nothing in the interface may invent a third one — `comment` and `payload` were exactly that, and
 * they read fields this API has never sent.
 *
 * `conditions` are the terms a decision was granted under, `risk_notes` is what the requester
 * declared, `request_payload` is the request itself and `proposed_action` the recorded intent:
 * together, what an approver decides on.
 */
export interface ApprovalRow {
  id: string
  kind: string
  subject_kind: string | null
  subject_id: string | null
  run_id: string | null
  node_run_id: string | null
  node_id: string | null
  project_id: string | null
  well_id: string | null
  title: string
  description: string | null
  action_level: ActionLevel
  proposed_action: string | null
  request_payload: Record<string, unknown>
  risk_notes: string | null
  evidence_refs: string[]
  conditions: string[]
  required_role: string | null
  requested_by: string | null
  requested_at: string
  expires_at: string | null
  status: string
  decided_by: string | null
  decided_at: string | null
  decision_note: string | null
  /** Present on list rows only: whether the approval is past its expiry and still pending. */
  overdue?: boolean
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
