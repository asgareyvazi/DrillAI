/**
 * The governance ledger for one well.
 *
 * Every mutation in the asset domain writes an `AuditLog` row, and this panel is the reader for them:
 * who acted, at what level, on which resource, and what the row looked like before and after. It is the
 * reason a rename can be trusted — the well keeps its id, the history stays attached, and the change
 * itself is a record rather than a gap.
 *
 * Two things it refuses to do:
 *
 * * **It does not invent detail.** `details.changed` is what the service recorded; a row that carries no
 *   field-level detail says so (`master.noDetail`) instead of rendering an empty "changed" cell that
 *   could be read as "nothing changed".
 * * **It does not present the ledger as the *only* history.** The cockpit's timeline merges these rows
 *   with extracted operations and twin changes; this is the governance view of the same records.
 */

import { useQuery } from '@tanstack/react-query'
import { drillingApi } from '../../api/endpoints'
import type { AuditLogEntry } from '../../api/types'
import { Async, Badge, Card, Table } from '../../components/common'
import { useI18n } from '../../i18n'
import { formatActionLevel, formatDateTime, formatStatus } from '../../lib/format'

/** The fields a ledger row says changed, if it says any. */
export function changedFields(entry: AuditLogEntry): string[] {
  const changed = entry.details?.changed
  return Array.isArray(changed) ? changed.filter((value): value is string => typeof value === 'string') : []
}

/** One `before → after` line for a changed field, or `null` when either side is not recorded. */
function transitionOf(entry: AuditLogEntry, field: string): string | null {
  const before = entry.before?.[field]
  const after = entry.after?.[field]
  if (before === undefined && after === undefined) return null
  const render = (value: unknown): string => {
    if (value === null) return '—'
    if (typeof value === 'object') return JSON.stringify(value)
    return String(value)
  }
  return `${render(before)} → ${render(after)}`
}

export function AuditPanel({ wellId }: { wellId: string }) {
  const { t, locale } = useI18n()
  const ledger = useQuery({
    queryKey: ['well-audit-log', wellId],
    queryFn: ({ signal }) => drillingApi.wellAuditLog(wellId, { limit: 100 }, signal),
  })

  return (
    <Card
      title={t('master.ledger')}
      subtitle={
        ledger.data ? `${ledger.data.total} ${t('master.ledger')}` : undefined
      }
    >
      <Async query={ledger} empty={(data) => data.items.length === 0}>
        {(data) => (
          <Table
            rows={data.items}
            rowKey={(row) => row.id}
            columns={[
              {
                key: 'at',
                header: t('master.changed'),
                render: (row) => formatDateTime(row.occurred_at, locale),
              },
              {
                key: 'actor',
                header: t('master.operator'),
                render: (row) => (
                  <span>
                    {row.actor_display ?? row.actor_id ?? '—'}
                    <span className="block text-[10px] text-graphite-500">{row.actor_kind}</span>
                  </span>
                ),
              },
              {
                key: 'action',
                header: t('master.transition'),
                render: (row) => (
                  <span>
                    <span className="font-mono text-xs" dir="ltr">
                      {row.action}
                    </span>
                    <Badge tone="neutral">{formatActionLevel(row.action_level)}</Badge>
                  </span>
                ),
              },
              {
                key: 'resource',
                header: t('master.well'),
                render: (row) => (
                  <span className="font-mono text-[11px]" dir="ltr">
                    {row.resource_kind}:{row.resource_id ?? '—'}
                  </span>
                ),
              },
              {
                key: 'outcome',
                header: t('master.status'),
                render: (row) => (
                  <Badge tone={row.outcome === 'success' ? 'ok' : 'warning'}>{formatStatus(row.outcome)}</Badge>
                ),
              },
              {
                key: 'changed',
                header: t('master.changed'),
                render: (row) => {
                  const fields = changedFields(row)
                  if (fields.length === 0) {
                    return <span className="text-[11px] text-graphite-500">{t('master.noDetail')}</span>
                  }
                  return (
                    <ul className="space-y-0.5">
                      {fields.map((field) => {
                        const line = transitionOf(row, field)
                        return (
                          <li key={field} className="text-[11px]">
                            <span className="font-mono" dir="ltr">
                              {field}
                            </span>
                            {line && <span className="ms-1 text-graphite-600 dark:text-graphite-300">{line}</span>}
                          </li>
                        )
                      })}
                    </ul>
                  )
                },
              },
            ]}
          />
        )}
      </Async>
    </Card>
  )
}
