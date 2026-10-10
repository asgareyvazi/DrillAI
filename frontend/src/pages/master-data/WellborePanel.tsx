/**
 * Wellbores and their lineage.
 *
 * A well is not one hole. Almost every well that matters has a sidetrack, a bypass or a re-entry, and
 * the difference between them is a *recorded relation* — which hole was drilled from which — not a
 * naming convention. This panel is where that relation is created and read.
 *
 * What it deliberately does not do:
 *
 * * **It never infers a parent from a name.** `ST-1` is a label; the lineage endpoint reads
 *   `parent_wellbore_id`. A wellbore is offered as a parent only if the server would accept it, and the
 *   server refuses a self-parent, a cross-well parent and a cycle regardless of what this form sends.
 * * **It never activates a wellbore as a side effect.** Activation is a separate, explicit act — the
 *   active hole is the one being drilled, and a create must not silently move that claim.
 * * **It shows the transition menu the server offered.** The states in the select are the ones in
 *   `allowed_transitions`; there is no transition table in this file.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { drillingApi, type WellboreCreatePayload } from '../../api/endpoints'
import type { Wellbore, WithTransitions } from '../../api/types'
import { Async, Badge, Button, Card, EmptyState, ErrorState, Table } from '../../components/common'
import { useActionGate } from '../../hooks/useActionGate'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber, formatStatus, humanise } from '../../lib/format'
import { useSession } from '../../stores/session'
import { ELEVATION_DATUMS } from './WellCreateForm'
import { FormField, Select, TextInput, numberOrNull } from './form'

const PURPOSES = ['original', 'sidetrack', 'bypass', 'reentry', 'reamed', 'pilot', 'contingency'] as const
/** Purposes the server requires a parent for: a hole that starts inside another hole. */
const NEEDS_PARENT = ['sidetrack', 'bypass', 'reentry']

type Row = Wellbore & WithTransitions

function CreateWellboreForm({
  wellId,
  existing,
  onCreated,
}: {
  wellId: string
  existing: Wellbore[]
  onCreated: () => void
}) {
  const { t } = useI18n()
  const gate = useActionGate('wellbore.create')
  const [name, setName] = useState('')
  const [purpose, setPurpose] = useState<string>('original')
  const [parent, setParent] = useState('')
  const [plannedMd, setPlannedMd] = useState('')
  const [plannedTvd, setPlannedTvd] = useState('')
  const [datum, setDatum] = useState('msl')

  const parentRequired = NEEDS_PARENT.includes(purpose)
  const missing = name.trim() === '' || (parentRequired && parent === '')

  const create = useMutation({
    mutationFn: () => {
      const payload: WellboreCreatePayload = { name: name.trim(), purpose, datum }
      if (parent !== '') payload.parent_wellbore_id = parent
      const md = numberOrNull(plannedMd)
      const tvd = numberOrNull(plannedTvd)
      if (md !== null) payload.planned_td_md_si = md
      if (tvd !== null) payload.planned_td_tvd_si = tvd
      return drillingApi.createWellbore(wellId, payload, `ui-wellbore-create-${wellId}-${crypto.randomUUID()}`)
    },
    onSuccess: () => {
      setName('')
      setParent('')
      setPlannedMd('')
      setPlannedTvd('')
      onCreated()
    },
  })

  return (
    <form
      className="space-y-3 border-t border-graphite-100 pt-3 dark:border-graphite-800"
      data-testid="wellbore-create-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (!missing && !create.isPending && !gate.suggestDisabled) create.mutate()
      }}
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <FormField label={t('master.wellboreName')} required>
          <TextInput value={name} onChange={setName} />
        </FormField>
        <FormField label={t('master.purpose')}>
          <Select
            value={purpose}
            onChange={(value) => {
              setPurpose(value)
              // A parent is only meaningful for a hole that starts inside another hole; switching back
              // to `original` clears it rather than sending a relation the server would refuse.
              if (!NEEDS_PARENT.includes(value)) setParent('')
            }}
            options={PURPOSES.map((value) => ({ value, label: humanise(value) }))}
          />
        </FormField>
        <FormField
          label={t('master.parentWellbore')}
          hint={t('master.parentWellboreHint')}
          required={parentRequired}
        >
          <Select
            value={parent}
            onChange={setParent}
            options={existing.map((row) => ({ value: row.id, label: `${row.sequence}. ${row.name}` }))}
            placeholder={t('master.noParent')}
            disabled={!parentRequired}
          />
        </FormField>
        <FormField label={t('master.plannedTdMd')}>
          <TextInput type="number" value={plannedMd} onChange={setPlannedMd} />
        </FormField>
        <FormField label={t('master.plannedTdTvd')}>
          <TextInput type="number" value={plannedTvd} onChange={setPlannedTvd} />
        </FormField>
        <FormField label={t('master.datum')}>
          <Select
            value={datum}
            onChange={setDatum}
            options={ELEVATION_DATUMS.map((value) => ({ value, label: value.toUpperCase() }))}
          />
        </FormField>
      </div>
      {create.error && <ErrorState error={create.error} />}
      <Button
        type="submit"
        disabled={missing || create.isPending || gate.suggestDisabled}
        title={gate.reason || undefined}
        data-testid="wellbore-create-submit"
        data-gate-state={gate.state}
      >
        {create.isPending ? t('master.creating') : t('master.addWellbore')}
      </Button>
    </form>
  )
}

function Lineage({ wellboreId }: { wellboreId: string }) {
  const { t } = useI18n()
  const lineage = useQuery({
    queryKey: ['wellbore-lineage', wellboreId],
    queryFn: ({ signal }) => drillingApi.wellboreLineage(wellboreId, signal),
  })

  return (
    <div className="mt-2 text-xs">
      <span className="font-medium">{t('master.lineage')}: </span>
      <Async query={lineage}>
        {(data) =>
          data.items.length <= 1 ? (
            <span className="text-graphite-500" data-testid="lineage-root">
              {t('master.lineageEmpty')}
            </span>
          ) : (
            <ol className="mt-1 space-y-1" data-testid="lineage-chain">
              {data.items.map((node, index) => (
                <li key={node.id} className="flex items-center gap-2">
                  <span className="font-mono text-[11px] text-graphite-500">{index + 1}</span>
                  <span>{node.name}</span>
                  <Badge tone="neutral">{humanise(node.purpose)}</Badge>
                  <span className="font-mono text-[10px] text-graphite-400" dir="ltr">
                    {node.id}
                  </span>
                </li>
              ))}
            </ol>
          )
        }
      </Async>
    </div>
  )
}

export function WellborePanel({ wellId, wellbores, onChanged }: { wellId: string; wellbores: Row[]; onChanged: () => void }) {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  const activateGate = useActionGate('wellbore.activate')
  const lifecycleGate = useActionGate('wellbore.lifecycle')
  const [selected, setSelected] = useState<string | null>(wellbores[0]?.id ?? null)
  const [target, setTarget] = useState('')
  const [reason, setReason] = useState('')

  useEffect(() => {
    if (selected === null || !wellbores.some((row) => row.id === selected)) {
      setSelected(wellbores[0]?.id ?? null)
    }
  }, [wellbores, selected])

  const activate = useMutation({
    mutationFn: (wellboreId: string) =>
      drillingApi.activateWellbore(wellboreId, `ui-wellbore-activate-${wellboreId}-${crypto.randomUUID()}`),
    onSuccess: onChanged,
  })

  const transition = useMutation({
    mutationFn: (wellboreId: string) =>
      drillingApi.transitionWellbore(
        wellboreId,
        { target, reason: reason.trim() === '' ? null : reason.trim() },
        `ui-wellbore-lifecycle-${wellboreId}-${crypto.randomUUID()}`,
      ),
    onSuccess: () => {
      setTarget('')
      setReason('')
      onChanged()
    },
  })

  const active = wellbores.find((row) => row.is_active) ?? null
  const focus = wellbores.find((row) => row.id === selected) ?? null

  return (
    <div className="space-y-4">
      <Card
        title={t('master.structure')}
        subtitle={active ? `${t('master.activeHole')}: ${active.name}` : t('master.emptyWellbores')}
        actions={active ? <Badge tone="ok">{t('master.activeHole')}</Badge> : null}
      >
        <Table
          rows={wellbores}
          rowKey={(row) => row.id}
          isRowActive={(row) => row.id === selected}
          onRowClick={(row) => setSelected(row.id)}
          empty={<EmptyState message={t('master.emptyWellbores')} />}
          columns={[
            { key: 'sequence', header: t('master.sequence'), align: 'end', render: (row) => row.sequence },
            {
              key: 'name',
              header: t('master.wellboreName'),
              render: (row) => (
                <span>
                  {row.name}
                  {row.is_active && (
                    <Badge tone="ok" {...{ 'data-testid': `active-${row.id}` }}>
                      {t('master.activeHole')}
                    </Badge>
                  )}
                  <span className="block font-mono text-[10px] text-graphite-500" dir="ltr">
                    {row.id}
                  </span>
                </span>
              ),
            },
            { key: 'purpose', header: t('master.purpose'), render: (row) => humanise(row.purpose) },
            { key: 'status', header: t('master.status'), render: (row) => formatStatus(row.status) },
            {
              key: 'planned',
              header: t('master.plannedTdMd'),
              align: 'end',
              render: (row) =>
                row.planned_td_md_si === null
                  ? '—'
                  : `${formatNumber(row.planned_td_md_si, locale, 0)} ${unitSystem === 'oilfield' ? 'ft' : 'm'}`,
            },
            {
              key: 'actual',
              header: t('master.actualTdMd'),
              align: 'end',
              render: (row) =>
                row.actual_td_md_si === null
                  ? '—'
                  : `${formatNumber(row.actual_td_md_si, locale, 0)} ${unitSystem === 'oilfield' ? 'ft' : 'm'}`,
            },
            {
              key: 'created',
              header: t('master.changed'),
              render: (row) => formatDateTime(row.created_at ?? null, locale),
            },
          ]}
        />

        {focus && (
          <div className="mt-3 space-y-3 border-t border-graphite-100 pt-3 dark:border-graphite-800">
            <Lineage wellboreId={focus.id} />

            <div className="flex flex-wrap items-end gap-3">
              <Button
                onClick={() => activate.mutate(focus.id)}
                disabled={focus.is_active || activate.isPending || activateGate.suggestDisabled}
                title={activateGate.reason || t('master.activateHint')}
                data-testid="wellbore-activate"
                data-gate-state={activateGate.state}
              >
                {t('master.activate')}
              </Button>
              <FormField label={t('master.transition')}>
                <Select
                  value={target}
                  onChange={setTarget}
                  options={(focus.allowed_transitions ?? []).map((value) => ({
                    value,
                    label: formatStatus(value),
                  }))}
                  placeholder={t('master.transition')}
                />
              </FormField>
              <FormField label={t('master.reason')}>
                <TextInput value={reason} onChange={setReason} />
              </FormField>
              <Button
                onClick={() => transition.mutate(focus.id)}
                disabled={target === '' || transition.isPending || lifecycleGate.suggestDisabled}
                title={lifecycleGate.reason || undefined}
                data-testid="wellbore-transition"
                data-gate-state={lifecycleGate.state}
              >
                {t('master.transition')}
              </Button>
            </div>
            {(focus.allowed_transitions ?? []).length === 0 && (
              <p className="text-xs text-graphite-500">{t('master.terminal')}</p>
            )}
            {activate.error && <ErrorState error={activate.error} />}
            {transition.error && <ErrorState error={transition.error} />}
          </div>
        )}

        <CreateWellboreForm wellId={wellId} existing={wellbores} onCreated={onChanged} />
      </Card>
    </div>
  )
}
