/**
 * The small form pieces the master-data screens are built from.
 *
 * Two rules live here rather than in each form, because getting either wrong is how a screen starts
 * inventing data:
 *
 * 1. **An empty input is `null`, never `0` and never `''`.** `numberOrNull` is the only way a number
 *    field leaves these forms. A blank depth means "not recorded", and a `0` would be a measurement
 *    nobody made — the difference between the two is the whole point of the master-data contract.
 * 2. **A field is marked required in the way the server marks it**, so the asterisk in the label and
 *    the 422 from the API agree. The forms do not enforce anything the server does not: they disable
 *    the submit while a required value is missing, and the server still decides.
 *
 * Identifiers are rendered `dir="ltr"` and in a mono face: a UWI, an API number and a record id are
 * data in a Latin script, and letting RTL reorder them would make them unreadable copies of
 * themselves.
 */

import type { ReactNode } from 'react'
import { useI18n } from '../../i18n'

/** Empty → `null`; a number → that number; anything unparseable → `null` (the server refuses it). */
export function numberOrNull(value: string): number | null {
  const trimmed = value.trim()
  if (trimmed === '') return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

/** Empty → `null`; otherwise the trimmed text. Used where the server distinguishes blank from absent. */
export function textOrNull(value: string): string | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/** A list field: one entry per line, blank lines dropped. */
export function linesToList(value: string): string[] {
  return value
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

/** A list field: comma separated, empties dropped. */
export function commaToList(value: string): string[] {
  return value
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part !== '')
}

/** A number → the string an `<input>` shows. `null` stays an empty box, never `0`. */
export function numberInput(value: number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value)
}

const INPUT =
  'w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-900'

export function TextInput({
  value,
  onChange,
  type = 'text',
  placeholder,
  ltr,
  disabled,
  name,
}: {
  value: string
  onChange: (next: string) => void
  type?: 'text' | 'number' | 'date' | 'datetime-local'
  placeholder?: string
  /** Identifiers and codes: Latin script, never reordered by the RTL layout. */
  ltr?: boolean
  disabled?: boolean
  name?: string
}) {
  return (
    <input
      type={type}
      name={name}
      value={value}
      disabled={disabled}
      placeholder={placeholder}
      dir={ltr ? 'ltr' : undefined}
      className={`${INPUT}${ltr ? ' font-mono' : ''}`}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

export function TextArea({
  value,
  onChange,
  rows = 3,
  disabled,
}: {
  value: string
  onChange: (next: string) => void
  rows?: number
  disabled?: boolean
}) {
  return (
    <textarea
      value={value}
      rows={rows}
      disabled={disabled}
      className={INPUT}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

export function Select({
  value,
  onChange,
  options,
  disabled,
  placeholder,
}: {
  value: string
  onChange: (next: string) => void
  options: Array<{ value: string; label: string }>
  disabled?: boolean
  /** The option that means "nothing chosen". Its value is the empty string. */
  placeholder?: string
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      className={INPUT}
      onChange={(event) => onChange(event.target.value)}
    >
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  )
}

export function Checkbox({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  label: ReactNode
  disabled?: boolean
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  )
}

/**
 * One labelled field in a form grid.
 *
 * `required` draws the marker the server's schema implies; it does not add a rule of its own.
 */
export function FormField({
  label,
  hint,
  required,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  required?: boolean
  children: ReactNode
}) {
  const { t } = useI18n()
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-graphite-600 dark:text-graphite-300">
        {label}
        {required && <span className="ms-1 text-danger">*</span>}
        {required && <span className="sr-only"> ({t('master.required')})</span>}
      </span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-graphite-500">{hint}</span>}
    </label>
  )
}

/** A read-only value shown next to editable ones: server-owned data the form never sends. */
export function ReadOnlyValue({ label, value }: { label: ReactNode; value: ReactNode }) {
  const { t } = useI18n()
  return (
    <div className="rounded border border-graphite-100 bg-graphite-50/60 p-2 dark:border-graphite-800 dark:bg-graphite-900/60">
      <span className="block text-[11px] font-medium tracking-wide text-graphite-500 uppercase">
        {label}
      </span>
      <span className="mt-0.5 block break-all font-mono text-xs" dir="ltr">
        {value ?? t('master.notRecorded')}
      </span>
      <span className="mt-0.5 block text-[10px] text-graphite-400">{t('master.serverOwned')}</span>
    </div>
  )
}
