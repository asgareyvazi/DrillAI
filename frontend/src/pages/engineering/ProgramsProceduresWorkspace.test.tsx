/** UI/API contract tests for scoped program and controlled-procedure authoring and review. */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { type ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  EngineeringProductApproval,
  EngineeringProductArtifact,
  EngineeringProductKind,
  EngineeringProductValidation,
  EngineeringProductVersion,
  UnitCatalogue,
  Well,
  Wellbore,
  WellSection,
} from '../../api/types'
import { I18nProvider } from '../../i18n'
import ProgramsProceduresWorkspace from './ProgramsProceduresWorkspace'

const apiMock = vi.hoisted(() => ({
  listEngineeringProducts: vi.fn(),
  createEngineeringProduct: vi.fn(),
  getEngineeringProduct: vi.fn(),
  listEngineeringProductVersions: vi.fn(),
  getEngineeringProductVersion: vi.fn(),
  reviseEngineeringProduct: vi.fn(),
  validateEngineeringProduct: vi.fn(),
  submitEngineeringProduct: vi.fn(),
  reviewEngineeringProduct: vi.fn(),
  decideEngineeringProduct: vi.fn(),
  exportEngineeringProduct: vi.fn(),
  getWell: vi.fn(),
  listWellbores: vi.fn(),
  listSections: vi.fn(),
  units: vi.fn(),
  askAdvisor: vi.fn(),
}))

vi.mock('../../api/endpoints', () => ({
  drillingApi: apiMock,
}))

const well = {
  id: 'wel_ui_1',
  project_id: 'prj_ui_1',
  field_id: null,
  name: 'Synthetic UI Well One',
  uwi: null,
  api_number: null,
  well_type: 'development_producer',
  status: 'planned',
  spud_date: null,
  release_date: null,
  operator: 'Synthetic test fixture',
  rig_id: null,
  is_offshore: false,
  surface_lat: null,
  surface_lon: null,
  kb_elevation_si: null,
  ground_elevation_si: null,
  water_depth_si: null,
  elevation_datum: 'rkb',
  slot: null,
  pad_name: null,
  total_depth_planned_si: null,
  twin_state: 'synthetic',
  objectives: null,
  target_formations: [],
  tags: ['synthetic', 'not_field_data'],
} satisfies Well

const wellbore = {
  id: 'wb_ui_1',
  well_id: well.id,
  name: 'Synthetic main bore',
  purpose: 'original',
  sequence: 1,
  parent_wellbore_id: null,
  status: 'planned',
  is_active: true,
  datum: 'rkb',
  kickoff_md_si: null,
  planned_td_md_si: null,
  planned_td_tvd_si: null,
  actual_td_md_si: null,
  actual_td_tvd_si: null,
} satisfies Wellbore

const section = {
  id: 'sec_ui_1',
  wellbore_id: wellbore.id,
  sequence: 1,
  name: 'Synthetic intermediate section',
  kind: 'intermediate',
  status: 'planned',
  hole_diameter_si: null,
  hole_diameter_nominal: null,
  planned_top_md_si: null,
  planned_bottom_md_si: null,
  actual_top_md_si: null,
  actual_bottom_md_si: null,
  current_md_si: null,
  casing_od_si: null,
  casing_od_nominal: null,
  casing_weight_si: null,
  casing_grade: null,
  casing_connection: null,
  casing_top_md_si: null,
  casing_shoe_md_si: null,
  cement_top_md_si: null,
  cement_planned_top_md_si: null,
  mud_weight_si: null,
  mud_weight_min_si: null,
  mud_weight_max_si: null,
  pore_pressure_gradient_si: null,
  fracture_gradient_si: null,
  collapse_gradient_si: null,
  lot_fit_equivalent_mw_si: null,
  pressure_source: null,
  is_planned_only: true,
  notes: null,
  semantics: {},
} satisfies WellSection

const unitCatalogue = {
  items: [
    { symbol: 'm', name: 'metre', dimension: 'length', factor: 1, offset: 0, kind: 'linear', aliases: [], notes: '', custom_conversion: false },
    { symbol: 'ft', name: 'foot', dimension: 'length', factor: 0.3048, offset: 0, kind: 'linear', aliases: [], notes: '', custom_conversion: false },
    { symbol: 'in', name: 'inch', dimension: 'length', factor: 0.0254, offset: 0, kind: 'linear', aliases: [], notes: '', custom_conversion: false },
    { symbol: 'kg/m3', name: 'kilogram per cubic metre', dimension: 'density', factor: 1, offset: 0, kind: 'linear', aliases: [], notes: '', custom_conversion: false },
    { symbol: 'ppg', name: 'pound per US gallon', dimension: 'density', factor: 119.8264273, offset: 0, kind: 'linear', aliases: [], notes: '', custom_conversion: false },
    { symbol: 'h', name: 'hour', dimension: 'time', factor: 3600, offset: 0, kind: 'linear', aliases: [], notes: '', custom_conversion: false },
  ],
  total: 6,
  dimensions: [
    { dimension: 'density', canonical_unit: 'kg/m3' },
    { dimension: 'length', canonical_unit: 'm' },
    { dimension: 'time', canonical_unit: 's' },
  ],
} satisfies UnitCatalogue

type Store = {
  artifact: EngineeringProductArtifact
  versions: EngineeringProductVersion[]
  approval: EngineeringProductApproval | null
}

let store: Store | null = null

function unknownLength(reason: string) {
  return { state: 'unknown', dimension: 'length', value: null, unit: null, source_kind: null, source_ref: null, reason, observations: [] }
}

function initialProgramPayload() {
  return {
    purpose: 'Synthetic UI workflow only; not a field program.',
    objectives: [{ sequence: 1, statement: 'Retain the explicitly authored synthetic objective.', target_md: null, source_references: [] }],
    design_basis: [],
    sections: [{
      sequence: 1,
      name: 'Synthetic UI section',
      section_id: null,
      hole_diameter: unknownLength('No test fixture value was supplied.'),
      planned_md_from: unknownLength('No test fixture top depth was supplied.'),
      planned_md_to: unknownLength('No test fixture bottom depth was supplied.'),
      mud_density_min: null,
      mud_density_max: null,
      operating_parameters: [],
      planned_operations: [],
      source_references: [],
    }],
    risks: [],
    assumptions: [],
  }
}

function newVersion(
  id: string,
  version: number,
  state: EngineeringProductVersion['state'] = 'draft',
  payload: Record<string, unknown> = initialProgramPayload(),
): EngineeringProductVersion {
  return {
    id,
    artifact_id: 'art_ui_1',
    version,
    state,
    schema_key: 'drilling_program',
    schema_version: 1,
    payload,
    authored_by: 'usr_author',
    authored_at: '2026-10-20T09:00:00Z',
    reviewed_by: null,
    reviewed_at: null,
    approved_by: null,
    approved_at: null,
    approval_request_id: null,
    change_reason: version === 1 ? 'Initial synthetic draft' : 'Synthetic test revision',
    supersedes_version_id: version > 1 ? 'ver_ui_1' : null,
    content_hash: `synthetic-content-hash-v${version}`,
    validation_summary: { source_lineage: [], linked_program: null, validation: null },
    engine_run_ids: [],
    created_at: '2026-10-20T09:00:00Z',
  }
}

function makeStore(kind: EngineeringProductKind = 'program'): Store {
  const version = newVersion(
    'ver_ui_1',
    1,
    'draft',
    kind === 'procedure' ? {
      purpose: 'Synthetic test-only controlled procedure.',
      scope_statement: 'Test scope only; not operational instructions.',
      prerequisites: [],
      steps: [],
      stop_work_conditions: [],
      source_references: [],
    } : initialProgramPayload(),
  )
  const artifact: EngineeringProductArtifact = {
    id: 'art_ui_1',
    org_id: 'org_ui_1',
    kind,
    title: kind === 'program' ? 'Synthetic program for UI test' : 'Synthetic procedure for UI test',
    code: 'SYN-UI-1',
    status: 'draft',
    current_version: 1,
    owner: 'usr_author',
    project_id: well.project_id,
    well_id: well.id,
    wellbore_id: wellbore.id,
    section_id: null,
    operation_id: null,
    domain_pack: 'drilling_engineering',
    created_at: '2026-10-20T09:00:00Z',
    updated_at: '2026-10-20T09:00:00Z',
  }
  return { artifact, versions: [version], approval: null }
}

function currentVersion(): EngineeringProductVersion {
  if (!store) throw new Error('test store not configured')
  return store.versions.find((item) => item.version === store?.artifact.current_version)!
}

function approvalRequest(): EngineeringProductApproval {
  if (!store) throw new Error('test store not configured')
  if (!store.approval) {
    store.approval = {
      id: 'apr_ui_1',
      kind: 'engineering_artifact',
      subject_kind: 'engineering_artifact_version',
      subject_id: currentVersion().id,
      status: 'pending',
      title: `Review ${store.artifact.title}`,
      required_role: 'drilling_supervisor',
      requested_by: 'usr_author',
      requested_at: '2026-10-20T09:01:00Z',
      request_payload: { artifact_id: store.artifact.id, artifact_version_id: currentVersion().id },
      decided_by: null,
      decided_at: null,
      decision_note: null,
    }
  }
  return store.approval
}

function renderWorkspace(path = `/wells/${well.id}/engineering-products`) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <I18nProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/wells/:wellId/engineering-products" element={children} />
            </Routes>
          </MemoryRouter>
        </I18nProvider>
      </QueryClientProvider>
    )
  }
  return { ...render(<ProgramsProceduresWorkspace />, { wrapper: Wrapper }), client }
}

function configureCommonMocks() {
  apiMock.getWell.mockResolvedValue(well as never)
  apiMock.listWellbores.mockResolvedValue({ items: [wellbore], total: 1, limit: 100, offset: 0 } as never)
  apiMock.listSections.mockResolvedValue({ items: [section], total: 1, limit: 100, offset: 0 } as never)
  apiMock.units.mockResolvedValue(unitCatalogue as never)
  apiMock.askAdvisor.mockResolvedValue({ answer: {
    facts: [{
      key: 'synthetic_fact',
      statement: 'Measured value remains source-reported.',
      value: 12.5,
      unit: 'ppg',
      source: 'extracted_record:value',
      source_id: 'rec-ui-1',
      evidence_ref: 'ev-ui-1',
      provenance: {},
      citations: [{ evidence_id: 'ev-ui-1', quote_verified: true }],
      grounding: {},
    }],
    calculations: [{ engine_key: 'well_control.kill_sheet', engine_version: 'test-only', validation_status: 'NEEDS_FIELD_VALIDATION', field_validation_required: true }],
    evidence: [{
      id: 'ev-ui-1',
      kind: 'document_excerpt',
      document_id: 'doc-ui-1',
      document_title: 'Synthetic source fixture',
      page_number: 1,
      excerpt: 'Synthetic source excerpt only.',
      method: 'exact_quote',
      confidence: 1,
      quote_verified: true,
      locator: { page_number: 1 },
    }],
    inference: [{ statement: 'No field conclusion is made.', citations: [{ evidence_id: 'ev-ui-1' }] }],
    recommendation: [{ statement: 'Use a qualified reviewer.', approval_state: 'requires_human_review' }],
    unknown: [{ key: 'field_acceptance', value: null, reason: 'No qualified field evidence was supplied.' }],
    sources: ['doc-ui-1'],
    limitations: ['Synthetic UI fixture only.'],
    narrative: null,
  } } as never)
  apiMock.listEngineeringProducts.mockImplementation(async (wellId: string, kind?: EngineeringProductKind) => {
    if (!store || store.artifact.well_id !== wellId || (kind && kind !== store.artifact.kind)) return { items: [], total: 0 }
    return { items: [{ artifact: { ...store.artifact }, current_version: { ...currentVersion() } }], total: 1 }
  })
  apiMock.listEngineeringProductVersions.mockImplementation(async (artifactId: string) => {
    if (!store || store.artifact.id !== artifactId) return { items: [], total: 0 }
    return { items: [...store.versions].sort((a, b) => b.version - a.version).map((item) => ({ ...item })), total: store.versions.length }
  })
  apiMock.getEngineeringProductVersion.mockImplementation(async (artifactId: string, versionId: string) => {
    if (!store || store.artifact.id !== artifactId) throw new Error('not found')
    const version = store.versions.find((item) => item.id === versionId)
    if (!version) throw new Error('version not found')
    return {
      artifact: { ...store.artifact },
      version: { ...version },
      linked_program: null,
      linked_program_current_state: null,
      approval_request: store.approval ? { ...store.approval, request_payload: { ...store.approval.request_payload } } : null,
    }
  })
  apiMock.getEngineeringProduct.mockRejectedValue(new Error('not used'))
  apiMock.createEngineeringProduct.mockImplementation(async (wellId: string, input: { kind: EngineeringProductKind; title: string; code?: string | null; payload: Record<string, unknown> }) => {
    const next = makeStore(input.kind)
    next.artifact.well_id = wellId
    next.artifact.title = input.title
    next.artifact.code = input.code ?? null
    next.artifact.status = 'draft'
    const firstVersion = next.versions[0]
    if (!firstVersion) throw new Error('synthetic create did not make its first revision')
    next.versions[0] = { ...firstVersion, payload: input.payload }
    const createdVersion = next.versions[0]!
    store = next
    return { artifact: { ...next.artifact }, version: { ...createdVersion } }
  })
  apiMock.validateEngineeringProduct.mockImplementation(async () => {
    if (!store) throw new Error('no store')
    const previous = currentVersion()
    const validation: EngineeringProductValidation = {
      state: 'warnings',
      validated_at: '2026-10-20T09:02:00Z',
      validated_by: 'usr_author',
      content_hash: previous.content_hash ?? 'synthetic-content-hash',
      ruleset: 'drillai.program-structure.v1',
      engine_finding_count: 0,
      engine_run_snapshots: [],
      attached_engine_findings_fingerprint: 'synthetic-fingerprint',
      linked_program_state: null,
      field_acceptance: 'not_established',
      findings: [{ code: 'registered_engine_result_missing', severity: 'warning', message: 'Synthetic test has no registered-engine result.', path: null, details: {} }],
      finding_ids: [],
    }
    const updated = { ...previous, validation_summary: { ...previous.validation_summary, validation } }
    store.versions = store.versions.map((item) => item.id === previous.id ? updated : item)
    return { artifact: { ...store.artifact }, version: updated, validation }
  })
  apiMock.submitEngineeringProduct.mockImplementation(async () => {
    if (!store) throw new Error('no store')
    const version = { ...currentVersion(), state: 'in_review' as const, reviewed_by: null, reviewed_at: null }
    store.versions = store.versions.map((item) => item.id === version.id ? version : item)
    store.artifact = { ...store.artifact, status: 'in_review' }
    const approval = approvalRequest()
    return { artifact: { ...store.artifact }, version, approval_request: approval }
  })
  apiMock.reviewEngineeringProduct.mockImplementation(async (_artifactId: string, _versionId: string, notes: string) => {
    if (!store) throw new Error('no store')
    const version = { ...currentVersion(), reviewed_by: 'usr_independent_reviewer', reviewed_at: '2026-10-20T09:03:00Z' }
    store.versions = store.versions.map((item) => item.id === version.id ? version : item)
    const approval = approvalRequest()
    store.approval = { ...approval, request_payload: { ...approval.request_payload, review: { reviewed_by: version.reviewed_by, reviewed_at: version.reviewed_at, notes } } }
    return { artifact: { ...store.artifact }, version, approval_request: store.approval }
  })
  apiMock.decideEngineeringProduct.mockImplementation(async (_artifactId: string, _versionId: string, input: { decision: 'approved' | 'rejected'; note: string }) => {
    if (!store) throw new Error('no store')
    const state: EngineeringProductVersion['state'] = input.decision === 'approved' ? 'approved' : 'rejected'
    const version = { ...currentVersion(), state, approved_by: state === 'approved' ? 'usr_decider' : null, approved_at: state === 'approved' ? '2026-10-20T09:04:00Z' : null }
    store.versions = store.versions.map((item) => item.id === version.id ? version : item)
    store.artifact = { ...store.artifact, status: state }
    const approval = approvalRequest()
    store.approval = { ...approval, status: state, decided_by: 'usr_decider', decided_at: '2026-10-20T09:04:00Z', decision_note: input.note }
    return { artifact: { ...store.artifact }, version, approval_request: store.approval }
  })
  apiMock.reviseEngineeringProduct.mockImplementation(async (_artifactId: string, input: { expected_current_version: number; payload: Record<string, unknown>; change_reason: string }) => {
    if (!store) throw new Error('no store')
    const prior = currentVersion()
    const next = {
      ...newVersion('ver_ui_2', input.expected_current_version + 1, 'draft', input.payload),
      change_reason: input.change_reason,
      supersedes_version_id: prior.id,
    }
    store.artifact = { ...store.artifact, status: 'draft', current_version: next.version }
    store.versions = [...store.versions, next]
    store.approval = null
    return { artifact: { ...store.artifact }, version: next }
  })
  apiMock.exportEngineeringProduct.mockResolvedValue(new Blob(['synthetic export'], { type: 'text/markdown' }))
}

beforeEach(() => {
  window.localStorage.setItem('drillai.locale', 'en')
  store = null
  vi.clearAllMocks()
  configureCommonMocks()
})

afterEach(() => {
  window.localStorage.removeItem('drillai.locale')
})

// Fixed stage IDs record only synthetic test timing, not user-entered content.
async function timedProgramInteraction<T>(stage: number, action: () => Promise<T>): Promise<T> {
  const start = performance.now()
  console.log(`::notice title=Program test timing::stage=${stage} state=start`)
  try {
    return await action()
  } finally {
    console.log(`::notice title=Program test timing::stage=${stage} elapsed_ms=${Math.round(performance.now() - start)}`)
  }
}

describe('ProgramsProceduresWorkspace', () => {
  it('authors a structured, wellbore-scoped drilling program and keeps required values explicitly unknown', async () => {
    const setupStart = performance.now()
    const user = userEvent.setup()
    console.log(`::notice title=Program test timing::stage=0 setup_ms=${Math.round(performance.now() - setupStart)}`)
    const renderStart = performance.now()
    renderWorkspace()
    console.log(`::notice title=Program test timing::stage=0 render_ms=${Math.round(performance.now() - renderStart)}`)
    await timedProgramInteraction(1, async () => user.click(await screen.findByRole('button', { name: 'Create product' })))
    await timedProgramInteraction(2, () => user.type(screen.getByTestId('product-title'), 'Synthetic program from UI'))
    await timedProgramInteraction(3, () => user.type(screen.getByTestId('product-code'), 'SYN-UI-PROGRAM-1'))
    await timedProgramInteraction(4, () => user.selectOptions(screen.getByTestId('product-wellbore'), wellbore.id))
    await timedProgramInteraction(5, () => user.type(screen.getByLabelText('Purpose'), 'Synthetic UI fixture; not a field design.'))

    await timedProgramInteraction(6, () => user.click(screen.getByRole('button', { name: 'Add objective' })))
    await timedProgramInteraction(7, () => user.type(screen.getByLabelText('Objective statement'), 'Retain this explicitly authored synthetic objective.'))
    await timedProgramInteraction(8, () => user.click(screen.getByRole('button', { name: 'Add section' })))
    await timedProgramInteraction(9, () => user.type(screen.getByLabelText('Program section name'), 'Synthetic section from the browser'))
    await timedProgramInteraction(10, () => user.selectOptions(screen.getByLabelText(/Linked master-data well section/), section.id))
    const unknownReasons = screen.getAllByLabelText('Why is this value unknown?')
    expect(unknownReasons).toHaveLength(3)
    await timedProgramInteraction(11, () => user.type(unknownReasons[0]!, 'No source value was supplied for this synthetic test.'))
    await timedProgramInteraction(12, () => user.type(unknownReasons[1]!, 'No top depth was supplied for this synthetic test.'))
    await timedProgramInteraction(13, () => user.type(unknownReasons[2]!, 'No bottom depth was supplied for this synthetic test.'))

    await timedProgramInteraction(14, () => user.click(screen.getByTestId('product-save')))
    await timedProgramInteraction(15, () => waitFor(() => expect(apiMock.createEngineeringProduct).toHaveBeenCalledTimes(1)))
    const [wellId, body] = apiMock.createEngineeringProduct.mock.calls[0] as [string, Record<string, unknown>]
    expect(wellId).toBe(well.id)
    expect(body).toMatchObject({
      kind: 'program',
      title: 'Synthetic program from UI',
      code: 'SYN-UI-PROGRAM-1',
      project_id: well.project_id,
      wellbore_id: wellbore.id,
      linked_program_version_id: null,
    })
    const payload = body.payload as Record<string, unknown>
    expect(payload).not.toHaveProperty('kind')
    expect(payload).toMatchObject({
      purpose: 'Synthetic UI fixture; not a field design.',
      objectives: [{ sequence: 1, statement: 'Retain this explicitly authored synthetic objective.' }],
      sections: [{
        sequence: 1,
        name: 'Synthetic section from the browser',
        section_id: section.id,
        hole_diameter: { state: 'unknown', value: null, unit: null, reason: 'No source value was supplied for this synthetic test.' },
        planned_md_from: { state: 'unknown', value: null, unit: null, reason: 'No top depth was supplied for this synthetic test.' },
        planned_md_to: { state: 'unknown', value: null, unit: null, reason: 'No bottom depth was supplied for this synthetic test.' },
      }],
    })
  // Native Windows full-suite timing: 15 real interactions reached save at ~5 seconds,
  // while independent complete executions finished correctly. This test-specific budget
  // leaves headroom for the measured multi-field journey; no global timeout or retry changes.
  }, 12_000)
  it('authors controlled prerequisites, ordered steps, verification and stop-work response without turning the procedure into a free-text canvas', async () => {
    const user = userEvent.setup()
    renderWorkspace()
    await user.click(await screen.findByRole('button', { name: 'Controlled procedure' }))
    await user.click(screen.getByRole('button', { name: 'Create product' }))
    await user.type(screen.getByTestId('product-title'), 'Synthetic controlled procedure from UI')
    await user.selectOptions(screen.getByTestId('product-wellbore'), wellbore.id)
    await user.type(screen.getByLabelText('Purpose'), 'Synthetic test scope; no field instructions.')
    await user.type(screen.getByLabelText(/Scope statement/), 'Test-only scope; not an operating procedure.')

    await user.click(screen.getByRole('button', { name: 'Add prerequisite' }))
    await user.type(screen.getByLabelText('Requirement'), 'The exact synthetic revision is present.')
    await user.type(screen.getByLabelText('How it is verified'), 'Compare the stored revision ID and content hash.')
    await user.click(screen.getByRole('button', { name: 'Add step' }))
    await user.type(screen.getByLabelText('Step title'), 'Verify test-only status')
    await user.type(screen.getByLabelText('Responsible role'), 'synthetic_test_reviewer')
    await user.type(screen.getByLabelText('Instruction'), 'Inspect the test record only; do not perform field work.')
    await user.type(screen.getByLabelText(/Preconditions/), 'The record is marked synthetic.')
    await user.type(screen.getByLabelText(/Hazards/), 'The record could be mistaken for a field instruction.')
    await user.type(screen.getByLabelText(/Control measures/), 'Keep the not-accepted warning visible.')
    await user.type(screen.getByLabelText(/Verification criteria/), 'The revision ID and hash are present.')
    await user.type(screen.getByLabelText('Response if criteria are not met'), 'Stop this test and record the mismatch.')
    await user.click(screen.getByRole('button', { name: 'Add stop-work condition' }))
    await user.type(screen.getByLabelText('Stop-work trigger'), 'A reader treats the test record as operational authorization.')
    await user.type(screen.getByLabelText('Immediate response'), 'Stop and clarify that no operational authority is granted.')
    await user.type(screen.getByLabelText('Human escalation role'), 'synthetic_test_reviewer')

    await user.click(screen.getByTestId('product-save'))
    await waitFor(() => expect(apiMock.createEngineeringProduct).toHaveBeenCalledTimes(1))
    const [, body] = apiMock.createEngineeringProduct.mock.calls[0] as [string, Record<string, unknown>]
    expect(body).toMatchObject({ kind: 'procedure', title: 'Synthetic controlled procedure from UI', wellbore_id: wellbore.id })
    const payload = body.payload as Record<string, unknown>
    expect(payload).not.toHaveProperty('kind')
    expect(payload).toMatchObject({
      scope_statement: 'Test-only scope; not an operating procedure.',
      prerequisites: [{ sequence: 1, requirement: 'The exact synthetic revision is present.', verification: 'Compare the stored revision ID and content hash.' }],
      steps: [{
        sequence: 1,
        title: 'Verify test-only status',
        responsible_role: 'synthetic_test_reviewer',
        verification_criteria: ['The revision ID and hash are present.'],
        response_if_not_met: 'Stop this test and record the mismatch.',
      }],
      stop_work_conditions: [{
        sequence: 1,
        trigger: 'A reader treats the test record as operational authorization.',
        escalation_role: 'synthetic_test_reviewer',
      }],
    })
  }, 20_000)

  it('requires current validation, records independent review, decides an exact revision, creates a compare-and-swap revision and exports Markdown', async () => {
    const user = userEvent.setup()
    store = makeStore()
    const { client } = renderWorkspace()
    await user.click(await screen.findByRole('button', { name: 'Read grounded well context' }))
    expect(await screen.findByText('Synthetic source excerpt only.')).toBeInTheDocument()
    expect(screen.getByText('Measured value remains source-reported.')).toBeInTheDocument()
    expect(screen.getByText(/NEEDS_FIELD_VALIDATION/)).toBeInTheDocument()
    expect(screen.getByText(/No field conclusion is made/)).toBeInTheDocument()
    expect(screen.getByText(/Use a qualified reviewer/)).toBeInTheDocument()
    expect(apiMock.askAdvisor).toHaveBeenCalledWith(well.id, { question: 'engineering_checks', use_llm: false })
    await user.click(await screen.findByRole('button', { name: /Synthetic program for UI test/ }))

    const validate = await screen.findByRole('button', { name: 'Validate this exact draft' })
    const submit = screen.getByRole('button', { name: 'Submit for independent review' })
    expect(submit).toBeDisabled()
    await user.click(validate)
    await waitFor(() => expect(apiMock.validateEngineeringProduct).toHaveBeenCalledWith(store?.artifact.id, 'ver_ui_1'))
    await waitFor(() => expect(submit).toBeEnabled())

    await user.click(submit)
    await waitFor(() => expect(apiMock.submitEngineeringProduct).toHaveBeenCalledWith(store?.artifact.id, 'ver_ui_1'))
    await screen.findByText('Independent review and decision')
    // Await the mutation's invalidation/refetch before typing: otherwise the review form can be
    // replaced mid-keystroke on slower hosts, corrupting the exact notes being submitted.
    await waitFor(() => expect(client.isMutating()).toBe(0))
    await waitFor(() => expect(client.isFetching()).toBe(0))
    await user.type(screen.getByLabelText('Independent review notes'), 'Synthetic reviewer checked the exact version and hash.')
    await user.click(screen.getByRole('button', { name: 'Record independent review' }))
    await waitFor(() => expect(apiMock.reviewEngineeringProduct).toHaveBeenCalledWith(store?.artifact.id, 'ver_ui_1', 'Synthetic reviewer checked the exact version and hash.'))
    expect(await screen.findByText(/Reviewed by: usr_independent_reviewer/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('Decision note (required)'), 'Approved only for this synthetic workflow test; not field acceptance.')
    await user.click(screen.getByRole('button', { name: 'Approve this exact revision' }))
    await waitFor(() => expect(apiMock.decideEngineeringProduct).toHaveBeenCalledWith(store?.artifact.id, 'ver_ui_1', {
      approval_id: 'apr_ui_1',
      decision: 'approved',
      note: 'Approved only for this synthetic workflow test; not field acceptance.',
    }))

    await user.click(await screen.findByRole('button', { name: 'Create revision' }))
    await user.clear(screen.getByTestId('product-change-reason'))
    await user.type(screen.getByTestId('product-change-reason'), 'Synthetic correction for the immutable-history UI test.')
    await user.clear(screen.getByLabelText('Purpose'))
    await user.type(screen.getByLabelText('Purpose'), 'Revised synthetic test purpose; no field design.')
    await user.click(screen.getByTestId('product-save'))
    await waitFor(() => expect(apiMock.reviseEngineeringProduct).toHaveBeenCalledTimes(1))
    expect(apiMock.reviseEngineeringProduct).toHaveBeenCalledWith('art_ui_1', expect.objectContaining({
      expected_current_version: 1,
      change_reason: 'Synthetic correction for the immutable-history UI test.',
      linked_program_version_id: null,
    }), expect.any(String))
    await screen.findByRole('button', { name: 'v2 · draft' })

    const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL')
    const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')
    const createObjectURL = vi.fn(() => 'blob:synthetic-product-export')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe('program-revision-2.md')
    })
    try {
      await user.click(screen.getByRole('button', { name: 'Export Markdown' }))
      await waitFor(() => expect(apiMock.exportEngineeringProduct).toHaveBeenCalledWith('art_ui_1', 'ver_ui_2', 'markdown'))
      expect(createObjectURL).toHaveBeenCalled()
      expect(click).toHaveBeenCalled()
    } finally {
      click.mockRestore()
      if (originalCreateObjectURL) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL)
      else Reflect.deleteProperty(URL, 'createObjectURL')
      if (originalRevokeObjectURL) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL)
      else Reflect.deleteProperty(URL, 'revokeObjectURL')
    }
  }, 20_000)

  it('renders the product-authoring route in Persian and applies right-to-left document direction', async () => {
    window.localStorage.setItem('drillai.locale', 'fa')
    renderWorkspace()
    expect(await screen.findByRole('heading', { name: 'برنامه‌های حفاری و رویه‌های کنترل‌شده' })).toBeInTheDocument()
    await waitFor(() => expect(document.documentElement.dir).toBe('rtl'))
    expect(screen.getByRole('button', { name: 'ایجاد محصول' })).toBeInTheDocument()
  })
})
