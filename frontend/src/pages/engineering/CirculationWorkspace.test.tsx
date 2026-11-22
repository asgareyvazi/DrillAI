import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { drillingApi } from '../../api/endpoints'
import { I18nProvider } from '../../i18n'
import CirculationWorkspace, { requiredNumber } from './CirculationWorkspace'

vi.mock('../../api/endpoints', () => ({ drillingApi: { listWellbores: vi.fn(), wellEngineRuns: vi.fn(), runEngine: vi.fn() } }))
vi.mock('../../hooks/useActionGate', () => ({ useActionGate: () => ({ state: 'ready', suggestDisabled: false, reason: '' }) }))
const api = vi.mocked(drillingApi)
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><I18nProvider><MemoryRouter initialEntries={['/wells/w1/circulation']}><Routes><Route path="/wells/:wellId/circulation" element={<CirculationWorkspace />} /></Routes></MemoryRouter></I18nProvider></QueryClientProvider>)
}
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }) }
function geometry() {
  change('Top MD 1 (m)', '0'); change('Bottom MD 1 (m)', '1000'); change('OD / bore 1 (m)', '.216')
  fireEvent.click(screen.getByRole('button', { name: 'Add section' }))
  change('Kind 2', 'drillpipe'); change('Top MD 2 (m)', '0'); change('Bottom MD 2 (m)', '1000')
  change('OD / bore 2 (m)', '.1'); change('ID 2 (m)', '.08'); change('Current bit MD (m)', '1000')
}

describe('structured circulation entry', () => {
  beforeEach(() => {
    window.localStorage.removeItem('drillai.locale')
    api.listWellbores.mockResolvedValue({ items: [{ id: 'wb1', name: 'Main bore' }] } as never)
    api.wellEngineRuns.mockResolvedValue({ items: [], total: 0 } as never)
    api.runEngine.mockResolvedValue({ engine_run_id: 'run1', engine: { key: 'wellbore.capacity', version: '1.4.0', validation_status: 'formula_derived' }, outputs: { total_volume_si: 1 }, units: { output_fields: {} }, warnings: [], assumptions: [], limitations: [], constraint_violations: [], inputs_hash: 'input', outputs_hash: 'output' } as never)
  })
  it('does not coerce a missing or non-finite number into a measurement', () => {
    expect(() => requiredNumber('')).toThrow(); expect(() => requiredNumber('Infinity')).toThrow(); expect(requiredNumber('0')).toBe(0)
  })
  it('submits explicitly entered geometry to the selected wellbore and clears stale output on edit', async () => {
    renderPage(); await screen.findByRole('option', { name: 'Main bore' }); change('Wellbore', 'wb1'); geometry()
    fireEvent.click(screen.getByRole('button', { name: 'Calculate and save run' }))
    await waitFor(() => expect(api.runEngine).toHaveBeenCalledWith('wellbore.capacity', { well_id: 'w1', wellbore_id: 'wb1', inputs: { current_depth_si: 1000, elements: [
      { kind: 'hole', from_depth_si: 0, to_depth_si: 1000, od_si: .216 },
      { kind: 'drillpipe', from_depth_si: 0, to_depth_si: 1000, od_si: .1, id_si: .08 },
    ] } }))
    expect(await screen.findByText(/input SHA: input/)).toBeInTheDocument()
    change('Current bit MD (m)', '800'); expect(screen.queryByText(/input SHA: input/)).not.toBeInTheDocument()
  })
  it('submits explicit hydraulic inputs, reference and nozzle coefficient', async () => {
    renderPage(); await screen.findByRole('option', { name: 'Main bore' }); change('Wellbore', 'wb1'); geometry(); change('Calculation', 'hydraulics')
    change('Flow (m³/s)', '.01'); change('Bit TVD from same origin (m)', '900'); change('Density (kg/m³)', '1200')
    change('Plastic viscosity (Pa·s)', '.02'); change('Yield point (Pa)', '5'); change('TFA (m²; optional)', '.0003')
    change('Discharge coefficient (required with TFA)', '.95'); change('Common MD/TVD origin ID', 'floor-A')
    change('Vertical datum', 'MSL'); change('Coordinate reference', 'grid-A'); change('Survey revision reference', 'rev-1'); change('Source / provenance', 'operator declaration')
    fireEvent.click(screen.getByRole('button', { name: 'Calculate and save run' }))
    await waitFor(() => expect(api.runEngine).toHaveBeenCalledWith('hydraulics.laminar', expect.objectContaining({
      well_id: 'w1', wellbore_id: 'wb1', inputs: expect.objectContaining({ bit_depth_si: 1000, bit_tvd_si: 900,
        discharge_coefficient: .95, depth_reference: { md_origin: 'floor-A', tvd_origin: 'floor-A', vertical_datum: 'MSL', coordinate_reference: 'grid-A', survey_reference: 'rev-1', source_reference: 'operator declaration', depth_positive: 'down' } }),
    })))
  })
  it('does not submit hydraulics without a declared depth reference', async () => {
    renderPage(); await screen.findByRole('option', { name: 'Main bore' }); change('Wellbore', 'wb1'); geometry(); change('Calculation', 'hydraulics')
    fireEvent.click(screen.getByRole('button', { name: 'Calculate and save run' }))
    expect(screen.getByRole('alert')).toHaveTextContent('All depth reference fields are required.')
    expect(api.runEngine).not.toHaveBeenCalled()
  })
})
