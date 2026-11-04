import type { EngineUnitContract, EngineValueUnitContract } from '../../api/types'
import { useI18n } from '../../i18n'
import { Badge } from '../common'

function UnitFieldList({ title, fields }: { title: string; fields: Record<string, EngineValueUnitContract> }) {
  const { t } = useI18n()
  const entries = Object.entries(fields).sort(([left], [right]) => left.localeCompare(right))
  return (
    <section>
      <h4 className="text-xs font-medium">{title}</h4>
      {entries.length === 0 ? (
        <p className="mt-1 text-[11px] text-graphite-500">No numeric fields are declared.</p>
      ) : (
        <ul className="mt-1 max-h-56 space-y-1 overflow-auto">
          {entries.map(([path, descriptor]) => (
            <li key={path} className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
              <code className="break-all text-graphite-600 dark:text-graphite-300">{path}</code>
              <span className="flex items-center gap-1.5">
                <Badge tone={descriptor.status === 'declared' ? 'info' : 'neutral'}>
                  {descriptor.status === 'declared' ? descriptor.value_unit : t('common.unknownUnit')}
                </Badge>
                {descriptor.dimension && <span className="text-graphite-500">{descriptor.dimension}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** Catalogue-level unit schema; runtime results carry exact numeric paths and conversions. */
export function UnitContractSummary({ contract }: { contract?: EngineUnitContract | null }) {
  const { t } = useI18n()
  if (!contract) return null
  return (
    <details data-testid="engine-unit-contract" className="rounded border border-graphite-100 p-2 dark:border-graphite-800">
      <summary className="cursor-pointer text-xs font-medium">{t('engineering.unitContractTitle')}</summary>
      <p className="mt-2 text-[11px] text-graphite-500">{t('engineering.unitContractHint')}</p>
      <div className="mt-2 grid gap-3 md:grid-cols-2">
        <UnitFieldList title={t('engineering.inputUnitFields')} fields={contract.input_fields ?? {}} />
        <UnitFieldList title={t('engineering.outputUnitFields')} fields={contract.output_fields ?? {}} />
      </div>
    </details>
  )
}
