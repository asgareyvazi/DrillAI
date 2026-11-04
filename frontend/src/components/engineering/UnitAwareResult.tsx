import type { EngineValueUnitContract } from '../../api/types'
import { useI18n } from '../../i18n'
import { humanise } from '../../lib/format'
import { useSession } from '../../stores/session'
import { Badge, Value } from '../common'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function UnitAwareNode({
  label,
  path,
  value,
  unitFields,
  onEvidence,
}: {
  label: string
  path: string
  value: unknown
  unitFields: Record<string, EngineValueUnitContract>
  onEvidence?: () => void
}) {
  const { unitSystem } = useSession()
  const descriptor = unitFields[path] ?? null

  if (typeof value === 'number') {
    return (
      <Value
        label={humanise(label)}
        value={value}
        unitContract={descriptor}
        unitSystem={unitSystem}
        source={descriptor ? `unit contract · ${descriptor.source} · ${path}` : `unit contract missing · ${path}`}
        meta={descriptor?.description ?? descriptor?.unknown_reason ?? undefined}
        onEvidence={onEvidence}
      />
    )
  }

  if (Array.isArray(value)) {
    return (
      <details className="rounded border border-graphite-100 p-2 dark:border-graphite-800">
        <summary className="cursor-pointer text-xs font-medium">
          {humanise(label)} <Badge tone="neutral">{value.length}</Badge>
        </summary>
        <div className="mt-2 space-y-2 ps-2">
          {value.map((item, index) => (
            <UnitAwareNode
              key={`${path}[${index}]`}
              label={`${humanise(label)} ${index + 1}`}
              path={`${path}[${index}]`}
              value={item}
              unitFields={unitFields}
              onEvidence={onEvidence}
            />
          ))}
          {value.length === 0 && <p className="text-xs text-graphite-500">No items.</p>}
        </div>
      </details>
    )
  }

  if (isRecord(value)) {
    const entries = Object.entries(value)
    return (
      <details className="rounded border border-graphite-100 p-2 dark:border-graphite-800">
        <summary className="cursor-pointer text-xs font-medium">{humanise(label)}</summary>
        <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {entries.map(([key, child]) => (
            <UnitAwareNode
              key={`${path}.${key}`}
              label={key}
              path={`${path}.${key}`}
              value={child}
              unitFields={unitFields}
              onEvidence={onEvidence}
            />
          ))}
          {entries.length === 0 && <p className="text-xs text-graphite-500">Empty object.</p>}
        </div>
      </details>
    )
  }

  return <Value label={humanise(label)} value={value} onEvidence={onEvidence} />
}

/** Recursively renders results using only per-path unit metadata from the engine API contract. */
export function UnitAwareResult({
  value,
  unitFields,
  onEvidence,
}: {
  value: unknown
  unitFields: Record<string, EngineValueUnitContract>
  onEvidence?: () => void
}) {
  const { t } = useI18n()
  const root = isRecord(value) ? value : { result: value }
  return (
    <div className="space-y-3" data-testid="unit-aware-result">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(root).map(([key, child]) => (
          <UnitAwareNode
            key={key}
            label={key}
            path={key}
            value={child}
            unitFields={unitFields}
            onEvidence={onEvidence}
          />
        ))}
      </div>
      <p className="text-[11px] text-graphite-500">{t('engineering.unitContractDisclaimer')}</p>
    </div>
  )
}
