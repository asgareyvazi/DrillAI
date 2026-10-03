/**
 * Hole sections — and the one rule that matters most on this panel.
 *
 * A section carries four kinds of number that all look alike and mean completely different things: what
 * the programme *plans*, what the hole *is* as drilled, what was *interpreted* off a measurement, and
 * where the bit *is now*. They are all metres, and presenting one in the place of another is how a
 * programme inherits a number nobody can defend.
 *
 * So every value on this panel is labelled with its kind, and the label comes from the server's
 * `semantics` map rather than from the column name. `planned_bottom_md_si` is never rendered in the
 * current-depth position: when no current depth has been recorded the panel says exactly that
 * (`master.currentDepthMissing`) rather than borrowing the plan's number.
 *
 * Editing follows the same split. The form groups the fields as plan, as-drilled and interpreted, and
 * `is_planned_only` is never sent — it is derived by the server from whether any as-drilled depth is
 * recorded, so a caller-settable flag would let a section claim to be drilled while carrying only a
 * plan.
 */

import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { drillingApi, type SectionCreatePayload, type SectionUpdatePayload } from '../../api/endpoints'
import type { SectionNumberSemantics, WellSection, Wellbore, WithTransitions } from '../../api/types'
import { Badge, Button, Card, Drawer, EmptyState, ErrorState, Table } from '../../components/common'
import { useActionGate } from '../../hooks/useActionGate'
import { useI18n } from '../../i18n'
import { formatNumber, formatStatus, humanise } from '../../lib/format'
import { useSession } from '../../stores/session'
import { FormField, Select, TextInput, numberOrNull, numberInput } from './form'

type Row = WellSection & WithTransitions
/** A wellbore as the structure read returns it: the hole, its sections, and its next transitions. */
export type StructureWellbore = Wellbore & WithTransitions & { sections: WellSection[] }

const SECTION_KINDS = [
  'conductor',
  'surface',
  'intermediate',
  'production',
  'liner',
  'tieback',
  'open_hole',
  'rathole',
] as const

/** The translation key for what a recorded number *is*, per the server's classification. */
const SEMANTIC_LABELS: Record<SectionNumberSemantics, string> = {
  plan: 'master.plan',
  actual: 'master.actual',
  computed: 'master.computed',
  interpreted: 'master.interpreted',
  progress: 'master.progress',
}

function SemanticsTag({ semantics, field }: { semantics: Row['semantics']; field: string }) {
  const { t } = useI18n()
  const kind = semantics[field]
  if (!kind) return null
  // The badge says what the number *is* — the classification the server published for this column.
  return <Badge tone={kind === 'plan' ? 'neutral' : kind === 'actual' ? 'ok' : 'info'}>{t(SEMANTIC_LABELS[kind])}</Badge>
}

/**
 * One depth readout: the number, its unit, and what kind of number it is.
 *
 * `null` renders as a stated absence, never as a dash borrowed from a neighbouring column.
 */
function Depth({
  label,
  value,
  semantics,
  field,
  missing,
}: {
  label: string
  value: number | null
  semantics: Row['semantics']
  field: string
  missing?: string
}) {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  return (
    <div className="rounded border border-graphite-100 bg-graphite-50/40 p-2 dark:border-graphite-800 dark:bg-graphite-900/60">
      <span className="block text-[11px] font-medium tracking-wide text-graphite-500 uppercase">
        {label}
      </span>
      <span className="mt-1 flex items-baseline gap-1.5">
        <span className={`font-mono text-sm ${value === null ? 'text-graphite-400' : ''}`}>
          {value === null
            ? (missing ?? t('master.notRecorded'))
            : `${formatNumber(value, locale, 1)} ${unitSystem === 'oilfield' ? 'ft' : 'm'}`}
        </span>
        <SemanticsTag semantics={semantics} field={field} />
      </span>
    </div>
  )
}

function SectionEditor({
  wellboreId,
  section,
  open,
  onClose,
  onSaved,
}: {
  wellboreId: string
  section: Row
  open: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useI18n()
  const gate = useActionGate('section.update')
  const [form, setForm] = useState({
    name: section.name,
    kind: section.kind as string,
    planned_top_md_si: numberInput(section.planned_top_md_si),
    planned_bottom_md_si: numberInput(section.planned_bottom_md_si),
    actual_top_md_si: numberInput(section.actual_top_md_si),
    actual_bottom_md_si: numberInput(section.actual_bottom_md_si),
    current_md_si: numberInput(section.current_md_si),
    casing_shoe_md_si: numberInput(section.casing_shoe_md_si),
    lot_fit_equivalent_mw_si: numberInput(section.lot_fit_equivalent_mw_si),
    hole_diameter_nominal: section.hole_diameter_nominal ?? '',
    notes: section.notes ?? '',
  })
  const [reason, setReason] = useState('')

  const set = (key: keyof typeof form, value: string) => setForm((current) => ({ ...current, [key]: value }))

  const save = useMutation({
    mutationFn: () => {
      const payload: SectionUpdatePayload = {}
      const numbers: Array<[keyof SectionUpdatePayload, keyof typeof form]> = [
        ['planned_top_md_si', 'planned_top_md_si'],
        ['planned_bottom_md_si', 'planned_bottom_md_si'],
        ['actual_top_md_si', 'actual_top_md_si'],
        ['actual_bottom_md_si', 'actual_bottom_md_si'],
        ['current_md_si', 'current_md_si'],
        ['casing_shoe_md_si', 'casing_shoe_md_si'],
        ['lot_fit_equivalent_mw_si', 'lot_fit_equivalent_mw_si'],
      ]
      for (const [target, source] of numbers) {
        const before = numberInput(section[target as keyof WellSection] as number | null)
        if (form[source] !== before) Object.assign(payload, { [target]: numberOrNull(form[source]) })
      }
      if (form.name !== section.name) payload.name = form.name
      if (form.kind !== section.kind) payload.kind = form.kind
      if (form.hole_diameter_nominal !== (section.hole_diameter_nominal ?? '')) {
        payload.hole_diameter_nominal = form.hole_diameter_nominal === '' ? null : form.hole_diameter_nominal
      }
      if (form.notes !== (section.notes ?? '')) payload.notes = form.notes === '' ? null : form.notes
      return drillingApi.updateSection(
        wellboreId,
        section.id,
        // `reason` is not part of the section body: the section service takes the edit as data and
        // records it; the ledger names the change itself.
        payload,
        `ui-section-update-${section.id}-${crypto.randomUUID()}`,
      )
    },
    onSuccess: () => {
      onSaved()
      onClose()
    },
  })

  return (
    <Drawer open={open} title={`${t('master.edit')} — ${section.name}`} onClose={onClose} wide>
      <div className="space-y-4">
        <p className="text-xs text-graphite-500">{t('master.semanticsNote')}</p>
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Depth label={t('master.plannedTop')} value={section.planned_top_md_si} semantics={section.semantics} field="planned_top_md_si" />
          <Depth label={t('master.plannedBottom')} value={section.planned_bottom_md_si} semantics={section.semantics} field="planned_bottom_md_si" />
          <Depth label={t('master.actualBottom')} value={section.actual_bottom_md_si} semantics={section.semantics} field="actual_bottom_md_si" />
          <Depth
            label={t('master.currentDepth')}
            value={section.current_md_si}
            semantics={section.semantics}
            field="current_md_si"
            missing={t('master.currentDepthMissing')}
          />
        </dl>

        <div className="grid gap-3 sm:grid-cols-3">
          <FormField label={t('master.sectionName')}>
            <TextInput value={form.name} onChange={(value) => set('name', value)} />
          </FormField>
          <FormField label={t('master.kind')}>
            <Select
              value={form.kind}
              onChange={(value) => set('kind', value)}
              options={SECTION_KINDS.map((value) => ({ value, label: humanise(value) }))}
            />
          </FormField>
          <FormField label={t('master.holeDiameter')}>
            <TextInput value={form.hole_diameter_nominal} onChange={(value) => set('hole_diameter_nominal', value)} ltr />
          </FormField>
        </div>

        <fieldset className="grid gap-3 sm:grid-cols-2">
          <legend className="mb-1 text-xs font-semibold">
            {t('master.plan')} <SemanticsTag semantics={section.semantics} field="planned_bottom_md_si" />
          </legend>
          <FormField label={t('master.plannedTop')}>
            <TextInput type="number" value={form.planned_top_md_si} onChange={(value) => set('planned_top_md_si', value)} />
          </FormField>
          <FormField label={t('master.plannedBottom')}>
            <TextInput type="number" value={form.planned_bottom_md_si} onChange={(value) => set('planned_bottom_md_si', value)} />
          </FormField>
        </fieldset>

        <fieldset className="grid gap-3 sm:grid-cols-2">
          <legend className="mb-1 text-xs font-semibold">
            {t('master.actual')} <SemanticsTag semantics={section.semantics} field="actual_bottom_md_si" />
          </legend>
          <FormField label={t('master.actualTop')}>
            <TextInput type="number" value={form.actual_top_md_si} onChange={(value) => set('actual_top_md_si', value)} />
          </FormField>
          <FormField label={t('master.actualBottom')}>
            <TextInput type="number" value={form.actual_bottom_md_si} onChange={(value) => set('actual_bottom_md_si', value)} />
          </FormField>
          <FormField label={t('master.casingShoe')}>
            <TextInput type="number" value={form.casing_shoe_md_si} onChange={(value) => set('casing_shoe_md_si', value)} />
          </FormField>
          <FormField label={t('master.currentDepth')} hint={t('master.currentDepthNote')}>
            <TextInput type="number" value={form.current_md_si} onChange={(value) => set('current_md_si', value)} />
          </FormField>
        </fieldset>

        <fieldset className="grid gap-3 sm:grid-cols-2">
          <legend className="mb-1 text-xs font-semibold">
            {t('master.interpreted')} <SemanticsTag semantics={section.semantics} field="lot_fit_equivalent_mw_si" />
          </legend>
          <FormField label={t('master.lotEquivalentMw')}>
            <TextInput
              type="number"
              value={form.lot_fit_equivalent_mw_si}
              onChange={(value) => set('lot_fit_equivalent_mw_si', value)}
            />
          </FormField>
        </fieldset>

        <FormField label={t('master.notes')}>
          <TextInput value={form.notes} onChange={(value) => set('notes', value)} />
        </FormField>
        <FormField label={t('master.reason')} hint={t('master.reasonHint')}>
          <TextInput value={reason} onChange={setReason} />
        </FormField>

        {save.error && <ErrorState error={save.error} />}
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            disabled={save.isPending || gate.suggestDisabled}
            title={gate.reason || undefined}
            data-testid="section-save"
            data-gate-state={gate.state}
          >
            {save.isPending ? t('master.saving') : t('master.save')}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t('master.cancel')}
          </Button>
        </div>
      </div>
    </Drawer>
  )
}

export function SectionPanel({
  wellbore,
  sections,
  onChanged,
}: {
  wellbore: StructureWellbore | null
  sections: Row[]
  onChanged: () => void
}) {
  const { t, locale } = useI18n()
  const { unitSystem } = useSession()
  const createGate = useActionGate('section.create')
  const lifecycleGate = useActionGate('section.lifecycle')
  const [editing, setEditing] = useState<Row | null>(null)
  const [target, setTarget] = useState('')
  const [selected, setSelected] = useState<Row | null>(null)

  const create = useMutation({
    mutationFn: () => {
      if (!wellbore) throw new Error('no wellbore selected')
      const nextSequence = sections.reduce((max, row) => Math.max(max, row.sequence), 0) + 1
      const payload: SectionCreatePayload = {
        sequence: nextSequence,
        name: `${nextSequence}. section`,
        kind: 'intermediate',
      }
      return drillingApi.createSection(wellbore.id, payload, `ui-section-create-${wellbore.id}-${crypto.randomUUID()}`)
    },
    onSuccess: onChanged,
  })

  const transition = useMutation({
    mutationFn: ({ sectionId, to }: { sectionId: string; to: string }) => {
      if (!wellbore) throw new Error('no wellbore selected')
      return drillingApi.transitionSection(
        wellbore.id,
        sectionId,
        { target: to, reason: null },
        `ui-section-lifecycle-${sectionId}-${crypto.randomUUID()}`,
      )
    },
    onSuccess: () => {
      setTarget('')
      onChanged()
    },
  })

  if (!wellbore) {
    return (
      <Card title={t('master.sections')}>
        <EmptyState message={t('master.selectWell')} />
      </Card>
    )
  }

  const unit = unitSystem === 'oilfield' ? 'ft' : 'm'

  return (
    <div className="space-y-4">
      <Card
        title={`${t('master.sections')} — ${wellbore.name}`}
        subtitle={t('master.currentDepthNote')}
        actions={
          <Button
            size="sm"
            onClick={() => create.mutate()}
            disabled={create.isPending || createGate.suggestDisabled}
            title={createGate.reason || undefined}
            data-testid="section-create"
            data-gate-state={createGate.state}
          >
            {create.isPending ? t('master.creating') : t('master.addSection')}
          </Button>
        }
      >
        <Table
          rows={sections}
          rowKey={(row) => row.id}
          isRowActive={(row) => row.id === selected?.id}
          onRowClick={(row) => setSelected(row)}
          empty={<EmptyState message={t('master.emptySections')} />}
          columns={[
            { key: 'sequence', header: t('master.sequence'), align: 'end', render: (row) => row.sequence },
            {
              key: 'name',
              header: t('master.sectionName'),
              render: (row) => (
                <span>
                  {row.name}
                  {row.is_planned_only && <Badge tone="neutral">{t('master.plannedOnly')}</Badge>}
                  <span className="block font-mono text-[10px] text-graphite-500" dir="ltr">
                    {row.id}
                  </span>
                </span>
              ),
            },
            { key: 'kind', header: t('master.kind'), render: (row) => humanise(row.kind) },
            { key: 'status', header: t('master.status'), render: (row) => formatStatus(row.status) },
            {
              key: 'hole',
              header: t('master.holeDiameter'),
              render: (row) => row.hole_diameter_nominal ?? '—',
            },
            {
              key: 'planned',
              header: t('master.plannedBottom'),
              align: 'end',
              render: (row) => (
                <span className="flex items-center justify-end gap-1.5">
                  <span className="font-mono">
                    {row.planned_bottom_md_si === null ? '—' : `${formatNumber(row.planned_bottom_md_si, locale, 0)} ${unit}`}
                  </span>
                  <SemanticsTag semantics={row.semantics} field="planned_bottom_md_si" />
                </span>
              ),
            },
            {
              key: 'actual',
              header: t('master.actualBottom'),
              align: 'end',
              render: (row) => (
                <span className="flex items-center justify-end gap-1.5">
                  <span className="font-mono">
                    {row.actual_bottom_md_si === null ? '—' : `${formatNumber(row.actual_bottom_md_si, locale, 0)} ${unit}`}
                  </span>
                  <SemanticsTag semantics={row.semantics} field="actual_bottom_md_si" />
                </span>
              ),
            },
            {
              key: 'current',
              header: t('master.currentDepth'),
              align: 'end',
              // The plan's number is not used here, whatever it says: a section with no recorded
              // current depth shows the absence, in words.
              render: (row) => (
                <span className="flex items-center justify-end gap-1.5" data-testid={`current-${row.id}`}>
                  {row.current_md_si === null ? (
                    <span className="text-[11px] text-graphite-500">{t('master.currentDepthMissing')}</span>
                  ) : (
                    <span className="font-mono">
                      {formatNumber(row.current_md_si, locale, 0)} {unit}
                    </span>
                  )}
                  <SemanticsTag semantics={row.semantics} field="current_md_si" />
                </span>
              ),
            },
            {
              key: 'edit',
              header: '',
              render: (row) => (
                <Button size="sm" variant="ghost" onClick={() => setEditing(row)} data-testid={`section-edit-${row.id}`}>
                  {t('master.edit')}
                </Button>
              ),
            },
          ]}
        />
        {create.error && <div className="mt-3">{<ErrorState error={create.error} />}</div>}
      </Card>

      {selected && (
        <Card title={`${t('master.lifecycle')} — ${selected.name}`} subtitle={formatStatus(selected.status)}>
          <div className="flex flex-wrap items-end gap-3">
            <FormField label={t('master.transition')}>
              <Select
                value={target}
                onChange={setTarget}
                options={(selected.allowed_transitions ?? []).map((value) => ({ value, label: formatStatus(value) }))}
                placeholder={t('master.transition')}
              />
            </FormField>
            <Button
              onClick={() => transition.mutate({ sectionId: selected.id, to: target })}
              disabled={target === '' || transition.isPending || lifecycleGate.suggestDisabled}
              title={lifecycleGate.reason || undefined}
              data-testid="section-transition"
              data-gate-state={lifecycleGate.state}
            >
              {t('master.transition')}
            </Button>
          </div>
          {(selected.allowed_transitions ?? []).length === 0 && (
            <p className="mt-2 text-xs text-graphite-500">{t('master.terminal')}</p>
          )}
          {transition.error && <div className="mt-3">{<ErrorState error={transition.error} />}</div>}
        </Card>
      )}

      {editing && wellbore && (
        <SectionEditor
          wellboreId={wellbore.id}
          section={editing}
          open
          onClose={() => setEditing(null)}
          onSaved={onChanged}
        />
      )}
    </div>
  )
}
