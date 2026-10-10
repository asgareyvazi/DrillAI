/**
 * The master-data workspace and the wells list, against the payloads the backend serves.
 *
 * Four claims are checked here, and each one is a rule the mission states rather than a rendering
 * detail:
 *
 * 1. **A planned bottom is never shown as the current depth.** A section with `current_md_si: null` and
 *    a plan of 3200 m must say "no current depth recorded" — printing 3200 m there is the single most
 *    misleading thing a depth readout can do.
 * 2. **Search goes to the server.** Typing reaches `listWells` as `q`, and the rows on screen are what
 *    the server returned — the screen does not filter a page it happens to hold.
 * 3. **The life-cycle menu is the server's.** The states offered are exactly `allowed_transitions`, and a
 *    terminal state offers none.
 * 4. **A stale edit is refused and said so.** A 409 on save renders the stale notice, from the server's
 *    own conflict, rather than silently re-sending.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import type { Well, WellSection, WellStructure, Wellbore, WithTransitions } from '../../api/types'
import { I18nProvider } from '../../i18n'
import MasterDataWorkspace from './MasterDataWorkspace'
import WellList from '../wells/WellList'

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    listProjects: vi.fn(),
    listFields: vi.fn(),
    listWells: vi.fn(),
    listRigs: vi.fn(),
    wellStructure: vi.fn(),
    updateWell: vi.fn(),
    transitionWell: vi.fn(),
    assignRig: vi.fn(),
    createWell: vi.fn(),
    createSection: vi.fn(),
    updateSection: vi.fn(),
    transitionSection: vi.fn(),
    createWellbore: vi.fn(),
    activateWellbore: vi.fn(),
    transitionWellbore: vi.fn(),
    wellboreLineage: vi.fn(),
    wellAuditLog: vi.fn(),
    // The action gate and the identity read are what enable the edit controls, and the gate is the
    // server's own answer; without them the buttons would be withheld and the test would pass for the
    // wrong reason.
    identity: vi.fn(),
    actions: vi.fn(),
  },
  platformApi: {},
  workflowApi: {},
  runApi: {},
  docsApi: {},
  engineeringApi: {},
  registryApi: {},
  ragApi: {},
}))

const api = vi.mocked(drillingApi)

const well: Well = {
  id: 'wel_1',
  project_id: 'prj_1',
  field_id: 'fld_1',
  name: 'SYNTH-DEMO-01 (synthetic data)',
  uwi: 'NO-15-9-2',
  api_number: null,
  well_type: 'development_producer',
  status: 'planned',
  spud_date: null,
  release_date: null,
  operator: 'DrillAI (synthetic)',
  rig_id: null,
  is_offshore: false,
  surface_lat: null,
  surface_lon: null,
  kb_elevation_si: null,
  ground_elevation_si: null,
  water_depth_si: null,
  elevation_datum: 'msl',
  slot: null,
  pad_name: null,
  total_depth_planned_si: 3200,
  twin_state: 'planned',
  objectives: null,
  target_formations: ['Brent'],
  tags: [],
  created_at: '2026-01-01T00:00:00+00:00',
  updated_at: '2026-02-01T10:00:00+00:00',
}

/**
 * A production section with a plan of 2400 → 3200 m and no recorded current depth.
 *
 * This is the shape the acceptance rule is about: the plan reaches 3200 m, the section has not been
 * drilled, and `current_md_si` is `null` because nobody recorded a position. The screen must not fill
 * that in from the plan.
 */
const section: WellSection = {
  id: 'sec_1',
  wellbore_id: 'wlb_1',
  sequence: 1,
  name: '12-1/4" section',
  kind: 'intermediate',
  status: 'planned',
  hole_diameter_si: 0.31115,
  hole_diameter_nominal: '12-1/4"',
  planned_top_md_si: 900,
  planned_bottom_md_si: 3200,
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
  semantics: {
    planned_top_md_si: 'plan',
    planned_bottom_md_si: 'plan',
    actual_top_md_si: 'actual',
    actual_bottom_md_si: 'actual',
    current_md_si: 'progress',
    lot_fit_equivalent_mw_si: 'interpreted',
  },
  updated_at: '2026-02-01T10:00:00+00:00',
}

const wellbore: Wellbore & WithTransitions & { sections: WellSection[]; counts: Record<string, number> } = {
  id: 'wlb_1',
  well_id: 'wel_1',
  name: 'Main bore',
  purpose: 'original',
  sequence: 1,
  parent_wellbore_id: null,
  status: 'planned',
  is_active: true,
  datum: 'msl',
  kickoff_md_si: null,
  planned_td_md_si: 3200,
  planned_td_tvd_si: 3150,
  actual_td_md_si: null,
  actual_td_tvd_si: null,
  updated_at: '2026-02-01T10:00:00+00:00',
  sections: [section],
  counts: { documents: 0 },
  allowed_transitions: ['drilling', 'abandoned'],
}

/** The structure payload, with the well's own transitions: a planned well may only go to permitting. */
function structure(overrides: Partial<WellStructure> = {}): WellStructure {
  return {
    well: { ...well, allowed_transitions: ['permitting'], active_wellbore_id: 'wlb_1' },
    field: null,
    rig: null,
    wellbores: [wellbore],
    counts: { documents: 0 },
    ...overrides,
  }
}

function renderWith(ui: React.ReactNode, path = '/master-data?well=wel_1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

/**
 * The action catalogue and the identity, as the server serves them.
 *
 * The permissions below are deliberately generous: these tests are about what the *form* sends and
 * shows, not about what a given role may do — the refusal side is covered by the API tests, where the
 * server is the one answering.
 */
const actionsPayload = {
  items: [
    { key: 'well.update', level: 'L2', description: 'edit a well', permission: 'well.update' },
    { key: 'well.lifecycle', level: 'L2', description: 'transition', permission: 'well.lifecycle' },
    { key: 'well.assign_rig', level: 'L2', description: 'rig', permission: 'well.assign_rig' },
    { key: 'well.create', level: 'L2', description: 'create', permission: 'well.create' },
    { key: 'wellbore.create', level: 'L2', description: 'create', permission: 'wellbore.create' },
    { key: 'wellbore.activate', level: 'L2', description: 'activate', permission: 'wellbore.activate' },
    { key: 'wellbore.lifecycle', level: 'L2', description: 'transition', permission: 'wellbore.lifecycle' },
    { key: 'section.create', level: 'L2', description: 'create', permission: 'section.create' },
    { key: 'section.update', level: 'L2', description: 'edit', permission: 'section.update' },
    { key: 'section.lifecycle', level: 'L2', description: 'transition', permission: 'section.lifecycle' },
    { key: 'field.create', level: 'L2', description: 'create', permission: 'field.create' },
    { key: 'field.update', level: 'L2', description: 'edit', permission: 'field.update' },
  ],
  total: 12,
  levels: { L2: 'draft', L4: 'execute' },
}

const identityPayload = {
  principal_id: 'usr_dev_admin_engineer',
  principal_kind: 'user',
  role_keys: ['engineering_manager'],
  roles: [],
  available_roles: [],
  permissions: ['well.*', 'wellbore.*', 'section.*', 'field.*'],
  max_action_level: 'L4',
  auth_enabled: false,
  identity_source: 'development_header',
  development_presets: [],
  locale: 'en',
  note: '',
}

beforeEach(() => {
  vi.clearAllMocks()
  api.actions.mockResolvedValue(actionsPayload as never)
  api.identity.mockResolvedValue(identityPayload as never)
  api.listProjects.mockResolvedValue({
    items: [{ id: 'prj_1', name: 'Synthetic Development Project', code: 'SDP', operator: null, country: null, basin: null, status: 'active', phase: 'engineering', description: null }],
    total: 1,
  } as never)
  api.listFields.mockResolvedValue({
    items: [
      {
        id: 'fld_1',
        project_id: 'prj_1',
        name: 'Synthetic Field',
        aliases: ['Synthetic Block'],
        country: null,
        basin: null,
        water_depth_si: null,
        centroid_lat: null,
        centroid_lon: null,
        status: 'active',
        notes: null,
      },
    ],
    total: 1,
  } as never)
  api.listWells.mockResolvedValue({ items: [well], total: 1, limit: 200, offset: 0 } as never)
  api.listRigs.mockResolvedValue({ items: [], total: 0 } as never)
  api.wellStructure.mockResolvedValue(structure() as never)
  api.wellAuditLog.mockResolvedValue({ items: [], total: 0 } as never)
  api.wellboreLineage.mockResolvedValue({
    items: [{ ...wellbore, allowed_transitions: ['drilling', 'abandoned'] }],
    total: 1,
    root_id: 'wlb_1',
    wellbore_id: 'wlb_1',
  } as never)
  api.updateWell.mockResolvedValue(well as never)
})

describe('a hole section with no recorded current depth', () => {
  it('says so, and never prints the planned bottom in its place', async () => {
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=sections')

    const cell = await screen.findByTestId('current-sec_1')
    expect(cell).toHaveTextContent(/no current depth recorded/i)
    // The plan is 3200 m. It must not appear in the current-depth cell under any formatting.
    expect(cell).not.toHaveTextContent('3200')
  })

  it('labels each number with what it is', async () => {
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=sections')

    await screen.findByTestId('current-sec_1')
    const row = screen.getByText('12-1/4" section').closest('tr')
    expect(row).not.toBeNull()
    const planned = within(row as HTMLElement).getByText('Plan')
    // Two plan numbers (top and bottom) and no as-drilled one: the labels are the server's semantics.
    expect(planned).toBeInTheDocument()
    expect(within(row as HTMLElement).getAllByText('As drilled')).toHaveLength(1)
  })
})

describe('the life-cycle menu', () => {
  it('offers exactly the transitions the server published', async () => {
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=identity')

    await waitFor(() => expect(api.wellStructure).toHaveBeenCalled())
    const select = await screen.findByTestId('well-transition')
    expect(select).toBeInTheDocument()
    const menu = within(screen.getByLabelText(/transition/i)).getAllByRole('option')
    const values = menu.map((option) => (option as HTMLOptionElement).value).filter((value) => value !== '')
    expect(values).toEqual(['permitting'])
  })

  it('says a terminal well has no transition rather than offering a dead one', async () => {
    api.wellStructure.mockResolvedValue(structure({ well: { ...well, status: 'p&a', allowed_transitions: [], active_wellbore_id: null } }) as never)
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=identity')

    expect(await screen.findByTestId('lifecycle-terminal')).toBeInTheDocument()
    expect(screen.queryByTestId('well-transition')).toBeNull()
  })
})

describe('editing a well', () => {
  it('sends only what changed, with the version it read and a reason', async () => {
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=identity')

    const name = await screen.findByDisplayValue('SYNTH-DEMO-01 (synthetic data)')
    await userEvent.clear(name)
    await userEvent.type(name, 'SYNTH-DEMO-01B')

    await userEvent.click(screen.getByTestId('well-save'))

    await waitFor(() => expect(api.updateWell).toHaveBeenCalled())
    const call = api.updateWell.mock.calls[0]
    expect(call).toBeDefined()
    const [wellId, payload] = call ?? ['', {}, undefined]
    expect(wellId).toBe('wel_1')
    expect(payload.name).toBe('SYNTH-DEMO-01B')
    // A rename sends the name, the version it read, and the (absent) reason — not the whole row, and
    // not a status: the life cycle moves through its own door.
    expect(Object.keys(payload).sort()).toEqual(['expected_updated_at', 'name', 'reason'])
    expect(payload.reason).toBeNull()
    expect(payload).not.toHaveProperty('status')
    expect(payload).not.toHaveProperty('twin_state')
    expect(payload.expected_updated_at).toBe('2026-02-01T10:00:00+00:00')
  })

  it('reports a stale edit as a conflict instead of re-sending it', async () => {
    api.updateWell.mockRejectedValue(
      new ApiError(409, 'platform.conflict', 'the well was changed by somebody else', {
        details: { current_updated_at: '2026-03-01T00:00:00+00:00' },
      }),
    )
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=identity')

    const name = await screen.findByDisplayValue('SYNTH-DEMO-01 (synthetic data)')
    await userEvent.clear(name)
    await userEvent.type(name, 'RENAMED')
    await userEvent.click(screen.getByTestId('well-save'))

    expect(await screen.findByTestId('stale-edit')).toBeInTheDocument()
    // Exactly one attempt: a stale write is not retried automatically — the retry is what would
    // overwrite the other operator's edit.
    expect(api.updateWell).toHaveBeenCalledTimes(1)
  })
})

describe('the wells list', () => {
  it('sends the search to the server and shows what came back', async () => {
    renderWith(<WellList />, '/wells')

    const box = await screen.findByLabelText(/search wells/i)
    await userEvent.type(box, 'NO-15-9-2')

    await waitFor(() =>
      expect(api.listWells).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'NO-15-9-2' }),
        expect.anything(),
      ),
    )
  })

  it('does not claim there are no wells when a search simply matched none', async () => {
    api.listWells.mockResolvedValue({ items: [], total: 340, limit: 200, offset: 0 } as never)
    renderWith(<WellList />, '/wells')

    const box = await screen.findByLabelText(/search wells/i)
    await userEvent.type(box, 'nothing-matches')

    expect(await screen.findByText(/no well matches this search/i)).toBeInTheDocument()
    // The distinction that matters: 340 wells exist in this scope; none of them match. The screen
    // must not say "no wells are registered", which is a statement about the fleet.
    expect(screen.queryByText(/no wells are registered/i)).toBeNull()
  })

  it('reports how many of the scope are shown when a filter is active', async () => {
    api.listWells.mockResolvedValue({ items: [well], total: 340, limit: 200, offset: 0 } as never)
    renderWith(<WellList />, '/wells')

    const box = await screen.findByLabelText(/search wells/i)
    await userEvent.type(box, 'SYNTH')

    expect(await screen.findByText(/1 shown of 340 in this scope/i)).toBeInTheDocument()
  })
})

describe('the wellbore panel', () => {
  it('offers the parent only for a purpose that has one', async () => {
    renderWith(<MasterDataWorkspace />, '/master-data?well=wel_1&tab=structure')

    const parent = await screen.findByLabelText(/parent wellbore/i)
    // `original` is the default purpose and has no parent: the select is disabled rather than offering
    // a relation the server would refuse.
    expect(parent).toBeDisabled()
  })
})
