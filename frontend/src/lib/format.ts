/**
 * Presentation-layer formatting.
 *
 * The canonical unit system is SI: the API sends metres, kilograms per cubic metre, pascals and so
 * on. This module only *renders*; it never converts a value that a calculation depends on. The
 * display-unit switch is explicit and reversible, so a user reading oilfield units and a user
 * reading SI are looking at the same stored number.
 */

export type UnitSystem = 'si' | 'oilfield'

export interface UnitDescriptor {
  /** SI unit symbol expected from the API. */
  si: string
  /** Human label for the SI value. */
  siLabel: string
  /** Conversion factor to the oilfield value: value_oilfield = value_si / factor. */
  factor?: number
  oilfield?: string
  oilfieldLabel?: string
}

/**
 * Unit bridge for the quantities the UI displays. Add an entry only when the platform actually
 * emits that unit — an entry implies the conversion has been checked against the unit registry.
 */
export const UNITS: Record<string, UnitDescriptor> = {
  m: { si: 'm', siLabel: 'm', factor: 0.3048, oilfield: 'ft', oilfieldLabel: 'ft' },
  'kg/m3': {
    si: 'kg/m3',
    siLabel: 'kg/m³',
    factor: 119.8264273,
    oilfield: 'ppg',
    oilfieldLabel: 'ppg',
  },
  Pa: { si: 'Pa', siLabel: 'Pa', factor: 6894.757293, oilfield: 'psi', oilfieldLabel: 'psi' },
  'N.m': { si: 'N.m', siLabel: 'N·m', factor: 1.3558179483, oilfield: 'ft-lbf', oilfieldLabel: 'ft·lbf' },
  N: { si: 'N', siLabel: 'N', factor: 4448.221615, oilfield: 'lbf', oilfieldLabel: 'lbf' },
  'm3/s': { si: 'm3/s', siLabel: 'm³/s', factor: 0.0000630901964, oilfield: 'gpm', oilfieldLabel: 'gpm' },
  'm/s': { si: 'm/s', siLabel: 'm/s', factor: 0.3048, oilfield: 'ft/s', oilfieldLabel: 'ft/s' },
  K: { si: 'K', siLabel: 'K', factor: 0.5555555556, oilfield: 'degR', oilfieldLabel: '°R' },
}

export interface RenderedValue {
  text: string
  unit: string | null
  /** True when no value exists — the UI must show the reason, never a zero. */
  missing: boolean
  title: string
}

const DEFAULT_PRECISION = 3

function localeTag(locale: string): string {
  return locale === 'fa' ? 'fa-IR' : 'en-US'
}

export interface FormatValueOptions {
  unit?: string | null
  unitSystem?: UnitSystem
  locale?: string
  precision?: number
  /** Suffix shown when the value is absent, e.g. "not measured". */
  missingLabel?: string
}

export function formatValue(value: unknown, options: FormatValueOptions = {}): RenderedValue {
  const { unit = null, unitSystem = 'si', locale = 'en', precision, missingLabel = '—' } = options

  if (value === null || value === undefined || value === '') {
    return { text: missingLabel, unit: null, missing: true, title: 'no value was recorded' }
  }
  if (typeof value === 'boolean') {
    return { text: value ? 'true' : 'false', unit: null, missing: false, title: '' }
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return { text: String(value), unit, missing: false, title: String(value) }
  }

  const descriptor = unit ? UNITS[unit] : undefined
  let display = value
  let displayUnit = descriptor?.siLabel ?? unit
  let title = `${value} ${descriptor?.si ?? unit ?? ''}`.trim()
  if (descriptor && unitSystem === 'oilfield' && descriptor.factor && descriptor.oilfield) {
    display = value / descriptor.factor
    displayUnit = descriptor.oilfieldLabel ?? descriptor.oilfield
    title = `${value} ${descriptor.siLabel} = ${display} ${displayUnit}`
  }

  const digits =
    precision ?? Math.abs(display) >= 100
      ? (precision ?? 1)
      : Math.abs(display) >= 1
        ? (precision ?? 2)
        : (precision ?? DEFAULT_PRECISION)

  return {
    text: new Intl.NumberFormat(localeTag(locale), {
      maximumFractionDigits: digits,
      minimumFractionDigits: 0,
    }).format(display),
    unit: displayUnit,
    missing: false,
    title,
  }
}

export function formatNumber(value: number | null | undefined, locale = 'en', digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat(localeTag(locale), { maximumFractionDigits: digits }).format(value)
}

export function formatPercent(value: number | null | undefined, locale = 'en', digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${new Intl.NumberFormat(localeTag(locale), { maximumFractionDigits: digits }).format(value)}%`
}

/** Dates/times in the user's locale and the well's timezone-neutral ISO instants from the API. */
export function formatDateTime(value: string | null | undefined, locale = 'en'): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(localeTag(locale), {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

export function formatDate(value: string | null | undefined, locale = 'en'): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(localeTag(locale), { dateStyle: 'medium' }).format(parsed)
}

export function formatDuration(hours: number | null | undefined, locale = 'en'): string {
  if (hours === null || hours === undefined || !Number.isFinite(hours)) return '—'
  const whole = Math.floor(hours)
  const minutes = Math.round((hours - whole) * 60)
  if (whole === 0) return `${minutes} min`
  if (minutes === 0) return `${whole} h`
  return `${formatNumber(whole, locale, 0)} h ${formatNumber(minutes, locale, 0)} min`
}

/** Turn `lost_circulation` into `Lost circulation` for display when no label is supplied. */
export function humanise(key: string | null | undefined): string {
  if (!key) return '—'
  const spaced = key.replace(/[_.]/g, ' ').trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

const ACTION_LEVEL_LABELS: Record<string, string> = {
  L0: 'L0 · observe',
  L1: 'L1 · advise',
  L2: 'L2 · draft',
  L3: 'L3 · propose',
  L4: 'L4 · execute with approval',
  L5: 'L5 · authorized automation',
}

export function formatActionLevel(level: string | null | undefined): string {
  if (!level) return '—'
  return ACTION_LEVEL_LABELS[level] ?? level
}

export function formatValidationStatus(status: string | null | undefined): string {
  switch (status) {
    case 'verified_against_reference':
      return 'verified against reference'
    case 'formula_derived':
      return 'formula derived'
    case 'needs_field_validation':
      return 'needs field validation'
    case 'provisional':
      return 'provisional'
    default:
      return status ?? '—'
  }
}

/** Title-case a status token coming from the API (`awaiting_approval` → `Awaiting approval`). */
export function formatStatus(status: string | null | undefined): string {
  if (!status) return '—'
  const text = status.replace(/[_.]/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}
