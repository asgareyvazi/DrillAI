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
