/**
 * Node configuration editor.
 *
 * The editor has to survive the state a person is actually in while typing JSON — half-finished,
 * unbalanced, temporarily wrong. The previous implementation re-derived the textarea's value from the
 * parsed config on every keystroke, so a character that broke the JSON was discarded and the field
 * snapped back: typing was effectively impossible. Here the typed text is the source of truth while
 * the field is being edited, the parse error is shown next to it, and the config is committed on blur
 * (or on an explicit apply) only when the text parses.
 *
 * Where the node type publishes a `config_schema`, the properties are also offered as form fields
 * derived from that schema — names, required markers, primitive types, enums and defaults. Structured
 * fields and the raw JSON view write to the same config, so neither is a second source of truth.
 */

import { useEffect, useState } from 'react'
import { Badge, Field } from '../../components/common'
import { humanise } from '../../lib/format'

export interface ConfigSchema {
  type?: string
  properties?: Record<string, ConfigSchemaProperty>
  required?: string[]
}

export interface ConfigSchemaProperty {
  type?: string | string[]
  title?: string
  description?: string
  enum?: unknown[]
  default?: unknown
  minimum?: number
  maximum?: number
}

function primitiveType(property: ConfigSchemaProperty): 'string' | 'number' | 'integer' | 'boolean' | 'enum' | 'other' {
  if (Array.isArray(property.enum) && property.enum.length > 0) return 'enum'
  const raw = Array.isArray(property.type) ? property.type.find((entry) => entry !== 'null') : property.type
  if (raw === 'string' || raw === 'number' || raw === 'integer' || raw === 'boolean') return raw
  return 'other'
}

function parseValue(raw: string, kind: string): unknown {
  if (kind === 'number' || kind === 'integer') {
    const parsed = Number(raw)
    return Number.isNaN(parsed) ? raw : parsed
  }
  if (kind === 'boolean') return raw === 'true'
  return raw
}

/**
 * Narrow the registry's `config_schema` — an untyped JSON-schema blob on the wire — into the shape
 * the form understands.
 *
 * This is the API boundary, so it is checked rather than cast: a schema that is not an object, or
 * whose `properties` is not a map, yields `null` and the editor falls back to raw JSON. Anything
 * unexpected inside a property is left as-is; `primitiveType` below decides what it can render.
 */
export function toConfigSchema(raw: unknown): ConfigSchema | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const candidate = raw as { type?: unknown; properties?: unknown; required?: unknown }
  const properties = candidate.properties
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return null
  const required = Array.isArray(candidate.required)
    ? candidate.required.filter((entry): entry is string => typeof entry === 'string')
    : []
  return {
    type: typeof candidate.type === 'string' ? candidate.type : undefined,
    properties: properties as Record<string, ConfigSchemaProperty>,
    required,
  }
}

export function NodeConfigEditor({
  config,
  schema,
  onChange,
}: {
  config: Record<string, unknown>
  schema?: ConfigSchema | null
  onChange: (config: Record<string, unknown>) => void
}) {
  const [text, setText] = useState(() => JSON.stringify(config ?? {}, null, 2))
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)

  // The draft is keyed by the *serialized* config, not by object identity: an equal config arriving
  // as a new object must not overwrite what the user is in the middle of typing.
  const serialized = JSON.stringify(config ?? {})
  useEffect(() => {
    // `serialized` is the value that matters: when it changes, the graph node itself changed.
    setText(JSON.stringify(JSON.parse(serialized) as Record<string, unknown>, null, 2))
    setError(null)
    setEditing(false)
  }, [serialized])

  function commit(candidate: string) {
    try {
      const parsed = JSON.parse(candidate) as unknown
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setError('the configuration must be a JSON object')
        return
      }
      setError(null)
      setEditing(false)
      onChange(parsed as Record<string, unknown>)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const properties = schema?.properties ?? {}
  const required = new Set(schema?.required ?? [])
  const hasSchema = Object.keys(properties).length > 0

  function setProperty(key: string, value: unknown) {
    const next = { ...(config ?? {}), [key]: value }
    onChange(next)
    setText(JSON.stringify(next, null, 2))
    setError(null)
  }

  function clearProperty(key: string) {
    const next = { ...(config ?? {}) }
    delete next[key]
    onChange(next)
    setText(JSON.stringify(next, null, 2))
  }

  return (
    <div className="space-y-3">
      {hasSchema && (
        <div className="space-y-2">
          <p className="text-[11px] font-semibold tracking-wide text-graphite-500 uppercase">
            Configuration fields
          </p>
          {Object.entries(properties).map(([key, property]) => {
            const kind = primitiveType(property)
            const value = (config ?? {})[key]
            const label = property.title ?? humanise(key)
            const hint = [
              property.description,
              required.has(key) ? 'required' : null,
              property.default !== undefined ? `default ${JSON.stringify(property.default)}` : null,
              property.minimum !== undefined || property.maximum !== undefined
                ? `range ${property.minimum ?? '−∞'}…${property.maximum ?? '∞'}`
                : null,
            ]
              .filter(Boolean)
              .join(' · ')
            const inputClass =
              'w-full rounded border border-graphite-300 px-2 py-1 text-xs dark:border-graphite-700 dark:bg-graphite-900'
            return (
              <Field key={key} label={<span data-testid={`config-label-${key}`}>{label}</span>} hint={hint}>
                <div className="flex items-center gap-1.5">
                  {kind === 'boolean' ? (
                    <select
                      aria-label={label}
                      value={value === undefined ? '' : String(value)}
                      onChange={(event) => setProperty(key, event.target.value === 'true')}
                      className={inputClass}
                    >
                      <option value="">— not set —</option>
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  ) : kind === 'enum' ? (
                    <select
                      aria-label={label}
                      value={value === undefined ? '' : String(value)}
                      onChange={(event) => setProperty(key, event.target.value)}
                      className={inputClass}
                    >
                      <option value="">— not set —</option>
                      {(property.enum ?? []).map((option) => (
                        <option key={String(option)} value={String(option)}>
                          {String(option)}
                        </option>
                      ))}
                    </select>
                  ) : kind === 'other' ? (
                    <input
                      aria-label={label}
                      value={value === undefined ? '' : JSON.stringify(value)}
                      onChange={(event) => {
                        try {
                          setProperty(key, JSON.parse(event.target.value))
                        } catch {
                          setError(`${key}: this field takes JSON, and it does not parse yet`)
                        }
                      }}
                      className={inputClass}
                    />
                  ) : (
                    <input
                      aria-label={label}
                      type={kind === 'string' ? 'text' : 'number'}
                      value={value === undefined || value === null ? '' : String(value)}
                      onChange={(event) => setProperty(key, parseValue(event.target.value, kind))}
                      className={inputClass}
                    />
                  )}
                  {value !== undefined && (
                    <button
                      type="button"
                      onClick={() => clearProperty(key)}
                      title={`remove ${key}`}
                      className="shrink-0 rounded px-1 text-xs text-graphite-500 hover:bg-graphite-100 dark:hover:bg-graphite-800"
                    >
                      clear
                    </button>
                  )}
                </div>
              </Field>
            )
          })}
        </div>
      )}

      <Field
        label="Configuration (JSON)"
        hint="Validated by the node type's own schema on the server; the graph is not saved until it parses."
      >
        <textarea
          aria-label="Configuration (JSON)"
          rows={hasSchema ? 6 : 10}
          spellCheck={false}
          value={text}
          onChange={(event) => {
            setEditing(true)
            setText(event.target.value)
            // Parse as the user types so the error appears immediately, but never rewrite the text.
            try {
              JSON.parse(event.target.value)
              setError(null)
            } catch (cause) {
              setError(cause instanceof Error ? cause.message : String(cause))
            }
          }}
          onBlur={() => commit(text)}
          className="w-full rounded border border-graphite-300 bg-graphite-50 p-2 font-mono text-[11px] dark:border-graphite-700 dark:bg-graphite-950"
        />
      </Field>

      {error && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-danger/40 bg-red-50/60 p-2 dark:bg-red-950/20">
          <Badge tone="danger">not valid JSON yet</Badge>
          <span className="text-[11px] text-danger">{error}</span>
          {editing && (
            <span className="text-[11px] text-graphite-500">
              the configuration is kept as you type; it is applied when the text parses
            </span>
          )}
        </div>
      )}
      {!error && editing && <p className="text-[11px] text-graphite-500">unapplied edits — leave the field to apply</p>}
    </div>
  )
}
