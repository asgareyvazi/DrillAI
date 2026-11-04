/**
 * The optimisation workspace, when the section read fails.
 *
 * `section_id` in the request comes from that read. With the read failed it used to be silently
 * `null`: the optimiser then ranked candidates for whatever scope the absence implied, and the page
 * presented the result as the answer to the question the engineer had asked. These tests pin the
 * replacement — the request's real scope is stated before the run, and only that read is retried.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { drillingApi } from '../../api/endpoints'
import type { CandidateView, EngineValueUnitContract, OptimisationExplanation } from '../../api/types'
import { I18nProvider } from '../../i18n'
import OptimisationWorkspace from './OptimisationWorkspace'

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    optimisationObjectives: vi.fn(),
    listWellbores: vi.fn(),
    listSections: vi.fn(),
    optimise: vi.fn(),
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

const objectives = {
  computable: [
    { key: 'ecd_margin_si', name: 'ECD margin', label: 'ECD margin', engine_key: 'hydraulics', direction: 'maximise' },
  ],
  not_evaluated: {},
}

const densityUnit: EngineValueUnitContract = {
  status: 'declared',
  value_unit: 'kg/m3',
  registry_unit: 'kg/m3',
  dimension: 'density',
  canonical_unit: 'kg/m3',
  unit_system_basis: 'engine_contract',
  semantic: null,
  source: 'engine_contract',
  description: null,
  description_status: 'not_declared',
  unknown_reason: null,
  requested_display_unit: null,
  precision: 3,
  display_units: {
    si: { unit_symbol: 'kg/m³', registry_symbol: 'kg/m3', factor: 1, offset: 0, supported: true, behavior: 'linear' },
    metric_engineering: { unit_symbol: 'g/cm³', registry_symbol: 'g/cm3', factor: 0.001, offset: 0, supported: true, behavior: 'linear' },
    field_us: { unit_symbol: 'ppg', registry_symbol: 'ppg', factor: 1 / 119.8264273, offset: 0, supported: true, behavior: 'linear' },
  },
}

function candidate(overrides: Partial<CandidateView> = {}): CandidateView {
  return {
    id: 'can_feasible',
    label: 'candidate-feasible',
    values: { mud_weight_si: 1250 },
    value_units: { mud_weight_si: densityUnit },
    objectives: { ecd_margin_si: 25 },
    objective_units: { ecd_margin_si: densityUnit },
    rank: 1,
    is_pareto: true,
    is_baseline: false,
    score: 1,
    is_feasible: true,
    constraint_results: [{ name: 'ecd_window', limit: 1300, actual: 1275, margin: 25 }],
    constraint_units: {
      'constraint_results[0].limit': densityUnit,
      'constraint_results[0].actual': densityUnit,
      'constraint_results[0].margin': densityUnit,
    },
    rejected_because: [],
    explanation: null,
    feasibility_margin: 25,
    predicted_metrics: {},
    engine_run_ids: [],
    evidence: {},
    warnings: [],
    ...overrides,
  }
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MemoryRouter initialEntries={['/wells/wel_1/optimisation']}>
          <Routes>
            <Route path="/wells/:wellId/optimisation" element={<OptimisationWorkspace />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.optimisationObjectives.mockResolvedValue(objectives as never)
  api.listWellbores.mockResolvedValue({ items: [{ id: 'wlb_1' }], total: 1, limit: 100, offset: 0 } as never)
})

describe('the optimisation workspace scope', () => {
  it('says the request would be sent without a section when that read failed', async () => {
    api.listSections.mockRejectedValue(new ApiError(500, 'platform.internal_error', 'boom'))
    renderWorkspace()

    const failure = await screen.findByTestId('optimise-scope-failure')
    expect(failure).toHaveTextContent(/section scope could not be read/i)
    expect(failure).toHaveTextContent(/without one/i)
    expect(failure).not.toHaveTextContent(/unreachable/i)
  })

  it('re-reads the section list when the operator retries, and clears the notice', async () => {
    api.listSections.mockRejectedValueOnce(new ApiError(0, 'network.unreachable', 'down'))
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    renderWorkspace()

    const failure = await screen.findByTestId('optimise-scope-failure')
    await userEvent.click(within(failure).getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(screen.queryByTestId('optimise-scope-failure')).toBeNull())
    expect(api.listSections).toHaveBeenCalledTimes(2)
  })

  it('says nothing when the section list was read', async () => {
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    renderWorkspace()

    await waitFor(() => expect(api.listSections).toHaveBeenCalled())
    expect(screen.queryByTestId('optimise-scope-failure')).toBeNull()
  })

  it('renders unit-declared constraints for recommended and infeasible candidates', async () => {
    api.listSections.mockResolvedValue({ items: [{ id: 'sec_1' }], total: 1, limit: 100, offset: 0 } as never)
    const recommended = candidate()
    const infeasible = candidate({
      id: 'can_infeasible',
      label: 'candidate-infeasible',
      values: { mud_weight_si: 1325 },
      objectives: { ecd_margin_si: -25 },
      rank: 2,
      is_pareto: false,
      is_feasible: false,
      constraint_results: [{ name: 'ecd_window', limit: 1300, actual: 1325, margin: -25 }],
      rejected_because: ['ecd_window_exceeded'],
    })
    const explanation: OptimisationExplanation = {
      optimization_run_id: 'opt_1',
      recommendation_id: 'rec_1',
      title: 'Unit-aware optimisation',
      status: 'succeeded',
      problem_key: 'drilling',
      created_at: '2026-11-04T10:00:00Z',
      objectives: [{ key: 'ecd_margin_si', sense: 'maximize', unit: 'kg/m3' }],
      constraints: [{ name: 'ecd_window', unit: 'kg/m3' }],
      decision_variables: [],
      engine_keys: ['hydraulics.laminar'],
      candidates_evaluated: 2,
      feasible_count: 1,
      pareto_count: 1,
      value_units: { mud_weight_si: densityUnit },
      objective_units: { ecd_margin_si: densityUnit },
      objective_contracts: {
        ecd_margin_si: {
          source: 'hydraulics.laminar',
          sense: 'maximize',
          unit: 'kg/m3',
          description: 'ECD margin',
          unit_contract: densityUnit,
        },
      },
      recommended,
      baseline: null,
      candidates: [recommended, infeasible],
      pareto_frontier: [recommended],
      available_options: [recommended],
      feasible_candidates: [recommended],
      why_not: [],
      objective_sources: { ecd_margin_si: 'hydraulics.laminar' },
      trade_offs: [],
      sensitivities: [],
      feasible_ranges: {},
      feasible_range_units: {},
      pareto_unit_fields: {},
      unit_contract: null,
      infeasible: [infeasible],
      engine_run_ids: [],
      engine_versions: { 'hydraulics.laminar': '1.0.0' },
      not_evaluated: {},
      notes: [],
      provenance: {},
    }
    api.optimise.mockResolvedValue({ optimisation: { explanation } } as never)
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(await screen.findByRole('button', { name: 'Optimise' }))
    expect(await screen.findByText('Constraint checks')).toBeInTheDocument()

    const recommendedConstraints = screen.getAllByTestId('unit-aware-result')[0]!
    await user.click(within(recommendedConstraints).getByText('Constraint results'))
    await user.click(within(recommendedConstraints).getByText('Constraint results 1'))
    expect((await within(recommendedConstraints).findAllByText('kg/m³')).length).toBe(3)

    const rejectedSummary = await screen.findByText('candidate-infeasible · ecd_window_exceeded')
    const rejectedCard = rejectedSummary.closest('details')
    expect(rejectedCard).not.toBeNull()
    await user.click(rejectedSummary)
    await user.click(within(rejectedCard as HTMLElement).getByText('Constraint results'))
    await user.click(within(rejectedCard as HTMLElement).getByText('Constraint results 1'))
    expect((await within(rejectedCard as HTMLElement).findAllByText('kg/m³')).length).toBe(5)
  })
})
