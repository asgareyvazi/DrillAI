/**
 * Reports Workspace against the report payload contract served by ReportingService.
 *
 * The UI must render the backend's real section metadata and export the same structured report as
 * JSON; it must not assume document rendering or fields the API does not return.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Report } from '../../api/types'
import { I18nProvider } from '../../i18n'
import { useSession } from '../../stores/session'
import ReportsWorkspace from './ReportsWorkspace'

const apiMock = vi.hoisted(() => ({
  reportKinds: vi.fn(),
  buildReport: vi.fn(),
  exportReport: vi.fn(),
}))

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    reportKinds: apiMock.reportKinds,
    buildReport: apiMock.buildReport,
    exportReport: apiMock.exportReport,
  },
}))

const densityUnit = {
  status: 'declared' as const,
  value_unit: 'kg/m3',
  registry_unit: 'kg/m3',
  dimension: 'density',
  canonical_unit: 'kg/m3',
  unit_system_basis: 'engine_contract',
  semantic: null,
  source: 'engine_contract',
  description: null,
  description_status: 'not_declared' as const,
  unknown_reason: null,
  requested_display_unit: null,
  precision: 3,
  display_units: {
    si: { unit_symbol: 'kg/m³', registry_symbol: 'kg/m3', factor: 1, offset: 0, supported: true, behavior: 'linear' },
    metric_engineering: { unit_symbol: 'g/cm³', registry_symbol: 'g/cm3', factor: 0.001, offset: 0, supported: true, behavior: 'linear' },
    field_us: { unit_symbol: 'ppg', registry_symbol: 'ppg', factor: 1 / 119.8264273, offset: 0, supported: true, behavior: 'linear' },
  },
}

const report = {
  kind: 'daily_drilling',
  title: 'Daily drilling — NF-12',
  well_id: 'wel_capture',
  generated_at: '2026-11-01T12:00:00+00:00',
  period_from: null,
  period_to: null,
  metadata: { well: { id: 'wel_capture', name: 'NF-12' }, generated_by: 'drillai.drilling.reporting' },
  sections: [
    {
      key: 'facts',
      title: 'Recorded drilling state',
      kind: 'facts',
      items: [{ key: 'current_md', label: 'Current measured depth', value: 3200, unit: 'm' }],
      note: null,
      count: 1,
    },
    {
      key: 'calculations',
      title: 'Engineering calculations',
      kind: 'calculations',
      items: [
        {
          engine_key: 'hydraulics.laminar',
          engine_version: '1.0.0',
          status: 'succeeded',
          inputs: {},
          outputs: { mud_weight_si: 1198.264273 },
          units: {
            contract_version: 'drillai.engine-units.v1',
            unit_system: 'SI',
            unit_system_basis: 'per_numeric_field',
            input_fields: {},
            output_fields: { mud_weight_si: densityUnit },
            violation_fields: {},
          },
          constraint_violations: [],
          inputs_hash: 'input-hash',
          outputs_hash: 'output-hash',
        },
      ],
      note: 'Only persisted calculations are included.',
      count: 1,
    },
    {
      key: 'assumptions',
      title: 'Assumptions and caveats',
      kind: 'assumptions',
      items: [],
      note: 'NPT time fallback is based on ingestion time when the source time is absent.',
      count: 0,
    },
  ],
  section_kinds: ['assumptions', 'calculations', 'facts'],
  is_renderable: true,
  rendering_note: 'This is a structured payload; Word/PDF rendering is a separate presentation layer.',
} satisfies Report

function renderWorkspace() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/wells/wel_capture/reports']}>
            <Routes>
              <Route path="/wells/:wellId/reports" element={children} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<ReportsWorkspace />, { wrapper: Wrapper })
}

beforeEach(() => {
  apiMock.reportKinds.mockReset().mockResolvedValue({
    kinds: [{ key: 'daily_drilling', description: 'The last reporting period' }],
    note: 'Structured report payload only; document rendering is not implemented.',
  })
  apiMock.buildReport.mockReset().mockResolvedValue({ report })
  apiMock.exportReport.mockReset().mockResolvedValue(
    new Blob(['{"report":{"kind":"daily_drilling"}}'], { type: 'application/json' }),
  )
})

afterEach(() => act(() => useSession.setState({ unitSystem: 'si' })))

describe('ReportsWorkspace', () => {
  it('renders the backend report contract and exports the exact authorized JSON response bytes', async () => {
    const user = userEvent.setup()
    const exportedBytes = '{"report":{"kind":"daily_drilling"}}'
    const exportBlob = new Blob([exportedBytes], { type: 'application/json' })
    apiMock.exportReport.mockResolvedValue(exportBlob)
    const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL')
    const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')
    const createObjectURL = vi.fn(() => 'blob:drillai-report')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe('drillai-daily_drilling-wel_capture.json')
    })

    try {
      act(() => useSession.setState({ unitSystem: 'oilfield' }))
      renderWorkspace()
      await user.click(await screen.findByRole('button', { name: 'Build' }))
      expect(await screen.findByText('Recorded drilling state')).toBeInTheDocument()
      expect(screen.getByText('Current measured depth')).toBeInTheDocument()
      expect(screen.getByText('This is a structured payload; Word/PDF rendering is a separate presentation layer.')).toBeInTheDocument()
      expect(screen.getByText('Report metadata')).toBeInTheDocument()
      await user.click(screen.getByText(/hydraulics.laminar@1.0.0/))
      expect(await screen.findByText('ppg')).toBeInTheDocument()
      expect(screen.getByText('10')).toBeInTheDocument()

      await user.click(screen.getByTestId('report-export-json'))
      await waitFor(() => expect(apiMock.exportReport).toHaveBeenCalledWith('wel_capture', 'daily_drilling'))
      expect(createObjectURL).toHaveBeenCalledWith(exportBlob)
      expect(exportBlob.type).toBe('application/json')
      expect(click).toHaveBeenCalledOnce()
    } finally {
      if (originalCreateObjectURL) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL)
      else Reflect.deleteProperty(URL, 'createObjectURL')
      if (originalRevokeObjectURL) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL)
      else Reflect.deleteProperty(URL, 'revokeObjectURL')
    }
  })
})
