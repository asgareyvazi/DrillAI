import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { EngineValueUnitContract } from '../../api/types'
import { I18nProvider } from '../../i18n'
import { useSession } from '../../stores/session'
import { UnitAwareResult } from './UnitAwareResult'

function descriptor(
  valueUnit: string,
  dimension: string,
  fieldUnit: string,
  fieldFactor: number,
): EngineValueUnitContract {
  return {
    status: 'declared',
    value_unit: valueUnit,
    registry_unit: valueUnit,
    dimension,
    canonical_unit: valueUnit,
    unit_system_basis: 'engine_contract',
    semantic: null,
    source: 'engine_contract',
    description: null,
    description_status: 'not_declared',
    unknown_reason: null,
    requested_display_unit: null,
    precision: 3,
    display_units: {
      si: {
        unit_symbol: valueUnit,
        registry_symbol: valueUnit,
        factor: 1,
        offset: 0,
        supported: true,
        behavior: 'linear',
      },
      metric_engineering: {
        unit_symbol: valueUnit,
        registry_symbol: valueUnit,
        factor: 1,
        offset: 0,
        supported: true,
        behavior: 'linear',
      },
      field_us: {
        unit_symbol: fieldUnit,
        registry_symbol: fieldUnit,
        factor: fieldFactor,
        offset: 0,
        supported: true,
        behavior: 'linear',
      },
    },
  }
}

function renderResult(value: unknown, unitFields: Record<string, EngineValueUnitContract>) {
  return render(
    <I18nProvider>
      <UnitAwareResult value={value} unitFields={unitFields} />
    </I18nProvider>,
  )
}

afterEach(() => {
  act(() => useSession.setState({ unitSystem: 'si' }))
})

describe('recursive engine result display', () => {
  it('converts by exact API path descriptors and leaves uncontracted numeric values explicitly unknown', () => {
    act(() => useSession.setState({ unitSystem: 'oilfield' }))
    renderResult(
      {
        flow_rate_si: 0.0000630901964,
        mud_weight_si: 1200,
      },
      {
        flow_rate_si: descriptor('m3/s', 'volume_flow', 'gpm', 15850.323141489),
      },
    )

    expect(screen.getByText('1')).toBeInTheDocument()
    expect(screen.getByText('gpm')).toBeInTheDocument()
    expect(screen.getByText('unit unknown')).toBeInTheDocument()
    expect(screen.getByText('1,200')).toBeInTheDocument()
  })

  it('uses runtime array paths and recursively renders nested numeric values', () => {
    act(() => useSession.setState({ unitSystem: 'oilfield' }))
    renderResult(
      {
        profile: [{ md_si: 304.8, nested: { pressure_si: 1000 } }],
      },
      {
        'profile[0].md_si': descriptor('m', 'length', 'ft', 3.280839895013123),
        'profile[0].nested.pressure_si': descriptor('Pa', 'pressure', 'psi', 1 / 6894.757293168),
      },
    )

    const details = screen.getByTestId('unit-aware-result').querySelectorAll('details')
    fireEvent.click(details[0]!.querySelector('summary')!)
    expect(screen.getByText('1,000')).toBeInTheDocument()
    expect(screen.getByText('ft')).toBeInTheDocument()

    fireEvent.click(details[1]!.querySelector('summary')!)
    expect(screen.getByText('0.145')).toBeInTheDocument()
    expect(screen.getByText('psi')).toBeInTheDocument()
  })
})
