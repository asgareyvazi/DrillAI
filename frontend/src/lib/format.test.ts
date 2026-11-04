/**
 * Value semantics and unit rendering.
 *
 * The product promises that a missing measurement, a measured zero, an unavailable capability and a
 * failed computation are four different things on screen. These tests are where that promise is
 * enforced, because a formatting helper is exactly where such a distinction gets quietly lost.
 */

import { describe, expect, it } from 'vitest'
import {
  UNITS,
  formatActionLevel,
  formatDate,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  formatStatus,
  formatValidationStatus,
  formatUnitContractValue,
  formatValue,
  humanise,
} from './format'

describe('formatValue: absence is not zero', () => {
  it('reports a missing value as missing, with no unit', () => {
    for (const absent of [null, undefined, '']) {
      const rendered = formatValue(absent, { unit: 'm' })
      expect(rendered.missing).toBe(true)
      expect(rendered.unit).toBeNull()
      expect(rendered.text).not.toBe('0')
    }
  })

  it('renders a recorded zero as a value', () => {
    const rendered = formatValue(0, { unit: 'm' })
    expect(rendered.missing).toBe(false)
    expect(rendered.text).toBe('0')
    expect(rendered.unit).toBe('m')
  })

  it('uses the caller-supplied label for absence instead of an em dash', () => {
    expect(formatValue(null, { missingLabel: 'not measured' }).text).toBe('not measured')
  })

  it('does not invent a number for a non-numeric payload', () => {
    const rendered = formatValue('reported as "tight"', { unit: 'm' })
    expect(rendered.text).toBe('reported as "tight"')
    expect(rendered.missing).toBe(false)
  })

  it('treats a non-finite number as unusable rather than rendering it', () => {
    for (const value of [Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(formatValue(value).text).toBe('NaN' === String(value) ? 'NaN' : 'Infinity')
      expect(formatValue(value).missing).toBe(false)
    }
  })

  it('renders booleans without units', () => {
    expect(formatValue(true)).toMatchObject({ text: 'true', unit: null, missing: false })
    expect(formatValue(false)).toMatchObject({ text: 'false', unit: null, missing: false })
  })
})

describe('formatValue: digits never depend on the value being a number', () => {
  it('honours an explicit precision', () => {
    expect(formatValue(1234.5678, { precision: 2 }).text).toBe('1,234.57')
  })

  it('reduces precision as the magnitude grows', () => {
    // A depth of 2412 m does not need three decimals; a 0.4 kg/m3 margin does.
    expect(formatValue(2412.34567).text).toBe('2,412.3')
    expect(formatValue(12.34567).text).toBe('12.35')
    expect(formatValue(0.34567).text).toBe('0.346')
  })

  it('keeps formatting a large value when no precision is passed', () => {
    const rendered = formatValue(9_876_543.21)
    expect(rendered.text).toBe('9,876,543.2')
  })
})

describe('formatValue: unit systems', () => {
  it('renders SI unchanged', () => {
    const rendered = formatValue(2412, { unit: 'm', unitSystem: 'si' })
    expect(rendered.text).toBe('2,412')
    expect(rendered.unit).toBe('m')
  })

  it('converts depth to feet on request and states both in the tooltip', () => {
    const rendered = formatValue(304.8, { unit: 'm', unitSystem: 'oilfield' })
    expect(rendered.text).toBe('1,000')
    expect(rendered.unit).toBe('ft')
    expect(rendered.title).toBe('304.8 m = 1000 ft')
  })

  it('converts every declared unit with a factor, and only those', () => {
    for (const [key, descriptor] of Object.entries(UNITS)) {
      expect(descriptor.si).toBe(key)
      if (descriptor.factor === undefined) continue
      const rendered = formatValue(descriptor.factor, { unit: key, unitSystem: 'oilfield' })
      expect(rendered.unit).toBe(descriptor.oilfieldLabel ?? descriptor.oilfield)
      expect(rendered.text.replace(/,/g, '')).toBe('1')
    }
  })

  it('leaves an unknown unit alone instead of guessing a conversion', () => {
    const rendered = formatValue(5, { unit: 'not-a-registered-unit', unitSystem: 'oilfield' })
    expect(rendered.unit).toBe('not-a-registered-unit')
    expect(rendered.text).toBe('5')
  })

  it('never converts a unitless value', () => {
    const rendered = formatValue(5, { unit: null, unitSystem: 'oilfield' })
    expect(rendered.unit).toBeNull()
    expect(rendered.text).toBe('5')
  })
})

describe('formatUnitContractValue: conversion is driven only by the API descriptor', () => {
  const density = {
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
      metric_engineering: { unit_symbol: 'kg/m³', registry_symbol: 'kg/m3', factor: 1, offset: 0, supported: true, behavior: 'linear' },
      field_us: { unit_symbol: 'ppg', registry_symbol: 'ppg', factor: 1 / 119.8264273, offset: 0, supported: true, behavior: 'linear' },
    },
  }

  it('uses the supplied affine conversion and selected display system', () => {
    const rendered = formatUnitContractValue(1198.264273, density, { unitSystem: 'oilfield' })
    expect(rendered.text).toBe('10')
    expect(rendered.unit).toBe('ppg')
    expect(rendered.title).toContain('1198.264273 kg/m3 =')
  })

  it('keeps canonical display values unchanged when the contract says SI', () => {
    const rendered = formatUnitContractValue(1198.264273, density, { unitSystem: 'si' })
    expect(rendered.text).toBe('1,198.264')
    expect(rendered.unit).toBe('kg/m³')
  })

  it('does not convert or invent a unit for absent/unknown contracts', () => {
    const unknown = formatUnitContractValue(5, undefined, {
      unitSystem: 'oilfield',
      unknownUnitLabel: 'unit unknown',
    })
    expect(unknown.text).toBe('5')
    expect(unknown.unit).toBe('unit unknown')
    expect(unknown.title).toContain('No authoritative unit metadata')

    const explicitUnknown = formatUnitContractValue(5, {
      ...density,
      status: 'unknown',
      value_unit: null,
      registry_unit: null,
      dimension: null,
      canonical_unit: null,
      unknown_reason: 'the historical run predates unit contracts',
      display_units: {
        si: { unit_symbol: null, registry_symbol: null, factor: null, offset: null, supported: false, behavior: 'unknown' },
        metric_engineering: { unit_symbol: null, registry_symbol: null, factor: null, offset: null, supported: false, behavior: 'unknown' },
        field_us: { unit_symbol: null, registry_symbol: null, factor: null, offset: null, supported: false, behavior: 'unknown' },
      },
    }, { unitSystem: 'oilfield', unknownUnitLabel: 'unit unknown' })
    expect(explicitUnknown.text).toBe('5')
    expect(explicitUnknown.unit).toBe('unit unknown')
    expect(explicitUnknown.title).toContain('predates unit contracts')
  })

  it('falls back to the stored unit without conversion when a display conversion is unsupported', () => {
    const unsupported = formatUnitContractValue(12, {
      ...density,
      display_units: {
        ...density.display_units,
        field_us: { unit_symbol: 'ppg', registry_symbol: 'ppg', factor: null, offset: null, supported: false, behavior: 'nonlinear' },
      },
    }, { unitSystem: 'oilfield' })
    expect(unsupported.text).toBe('12')
    expect(unsupported.unit).toBe('kg/m3')
    expect(unsupported.title).toContain('No supported conversion')
  })
})

describe('scalar helpers', () => {
  it('formats numbers, percentages and durations with absence intact', () => {
    expect(formatNumber(null)).toBe('—')
    expect(formatNumber(3.14159, 'en', 3)).toBe('3.142')
    expect(formatPercent(48.5712)).toBe('48.6%')
    expect(formatPercent(undefined)).toBe('—')
    expect(formatDuration(8.5)).toBe('8 h 30 min')
    expect(formatDuration(0)).toBe('0 min')
    expect(formatDuration(null)).toBe('—')
  })

  it('formats dates in the requested locale and returns input it cannot parse', () => {
    expect(formatDate('2026-03-15T00:00:00+00:00', 'en')).toMatch(/Mar/)
    expect(formatDate(null)).toBe('—')
    expect(formatDateTime('not-a-date')).toBe('not-a-date')
    expect(formatDateTime(null)).toBe('—')
  })

  it('humanises machine tokens and statuses', () => {
    expect(humanise('lost_circulation')).toBe('Lost circulation')
    expect(humanise('waiting_on_weather')).toBe('Waiting on weather')
    expect(humanise(null)).toBe('—')
    expect(formatStatus('waiting_approval')).toBe('Waiting approval')
    expect(formatStatus(null)).toBe('—')
    expect(formatValidationStatus('needs_field_validation')).toBe('needs field validation')
    expect(formatValidationStatus('unknown_status')).toBe('unknown_status')
  })

  it('names action levels L0-L5 rather than showing a bare token', () => {
    expect(formatActionLevel('L4')).toBe('L4 · execute with approval')
    expect(formatActionLevel('L5')).toBe('L5 · authorized automation')
    expect(formatActionLevel(null)).toBe('—')
  })
})
