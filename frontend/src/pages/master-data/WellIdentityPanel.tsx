/**
 * A well's identity, its life cycle and its rig.
 *
 * ## What is editable, and why the split is shown rather than implied
 *
 * The panel renders three groups: the fields this form may change, the fields the *domain* manages
 * (life cycle status, twin state, the assigned rig), and the fields that are immutable. The split is
 * the server's — `assets/identity.py` classifies every column, and the service refuses an edit to
 * anything outside the editable set with an error naming the door the change has to go through. Drawing
 * the same split here is not a second copy of the rules; it is the reason the operator is not offered a
 * control the API would reject. If the classification ever changes, the refusals change with it and the
 * panel degrades to a form the server declines — never to a form that appears to succeed.
 *
 * ## Stale writes
 *
 * The form remembers the `updated_at` it was rendered from and sends it back as
 * `expected_updated_at`. Two operators with the same well open is the normal case; the second save is
 * refused (409) instead of silently overwriting the first. The refusal is not retried — a blind retry
 * of a stale write is exactly the overwrite the check exists to prevent — and the panel says what
 * happened and offers a reload.
 *
 * ## The optimistic state is never the truth
 *
 * Nothing is written locally on submit. The rendered values come from the mutation's own response (the
 * server's row, after its writes), and the queries that display the well are invalidated so the next
 * read is the server's answer rather than the form's hope.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '../../api/client'
import { drillingApi, type WellUpdatePayload } from '../../api/endpoints'
import type { Rig, Well, WithTransitions } from '../../api/types'
import { Badge, Button, Card, ErrorState } from '../../components/common'
import { useActionGate } from '../../hooks/useActionGate'
import { useI18n } from '../../i18n'
import { formatDateTime, formatStatus, humanise } from '../../lib/format'
import { ELEVATION_DATUMS, WELL_TYPES } from './WellCreateForm'
import { FormField, ReadOnlyValue, Select, TextArea, TextInput, commaToList, numberOrNull, textOrNull } from './form'

interface IdentityForm {
  name: string
  uwi: string
  api_number: string
  operator: string
  well_type: string
  elevation_datum: string
  slot: string
  pad_name: string
  is_offshore: boolean
  surface_lat: string
  surface_lon: string
  kb_elevation_si: string
  ground_elevation_si: string
  water_depth_si: string
  total_depth_planned_si: string
  spud_date: string
  release_date: string
  objectives: string
  target_formations: string
  tags: string
  field_id: string
}

const asInput = (value: number | null): string => (value === null ? '' : String(value))
/** A timestamp as the date input wants it (`YYYY-MM-DD`); `null` stays blank. */
const asDate = (value: string | null): string => (value === null ? '' : value.slice(0, 10))

function toForm(well: Well): IdentityForm {
  return {
    name: well.name,
    uwi: well.uwi ?? '',
    api_number: well.api_number ?? '',
    operator: well.operator ?? '',
    well_type: well.well_type,
    elevation_datum: well.elevation_datum,
    slot: well.slot ?? '',
    pad_name: well.pad_name ?? '',
    is_offshore: well.is_offshore,
    surface_lat: asInput(well.surface_lat),
    surface_lon: asInput(well.surface_lon),
    kb_elevation_si: asInput(well.kb_elevation_si),
    ground_elevation_si: asInput(well.ground_elevation_si),
    water_depth_si: asInput(well.water_depth_si),
    total_depth_planned_si: asInput(well.total_depth_planned_si),
    spud_date: asDate(well.spud_date),
    release_date: asDate(well.release_date),
    objectives: well.objectives ?? '',
    target_formations: well.target_formations.join('\n'),
    tags: well.tags.join(', '),
    field_id: well.field_id ?? '',
  }
}

/**
 * What changed, in the shape the API takes.
 *
 * Only fields that actually differ are sent, and a value the form *cleared* is sent as `null` rather
 * than omitted — clearing a recorded slot is an edit, and omitting it would silently keep the old value.
 */
export function wellUpdatePayload(well: Well, form: IdentityForm): WellUpdatePayload {
  const before = toForm(well)
  const payload: WellUpdatePayload = {}
  // The list is typed by the *source* key it reads; the boolean key (`is_offshore`) is handled
  // separately below, because it is the one value that must never be coerced to `null` when cleared.
  const text: Array<[keyof WellUpdatePayload, keyof IdentityForm]> = [
    ['name', 'name'],
    ['uwi', 'uwi'],
    ['api_number', 'api_number'],
    ['operator', 'operator'],
    ['slot', 'slot'],
    ['pad_name', 'pad_name'],
    ['objectives', 'objectives'],
    ['field_id', 'field_id'],
    ['well_type', 'well_type'],
    ['elevation_datum', 'elevation_datum'],
  ]
  for (const [target, source] of text) {
    const next = form[source]
    if (next === before[source]) continue
    if (target === 'name' || target === 'well_type' || target === 'elevation_datum') {
      Object.assign(payload, { [target]: String(next) })
    } else {
      Object.assign(payload, { [target]: textOrNull(String(next)) })
    }
  }
  const numbers: Array<[keyof WellUpdatePayload, keyof IdentityForm]> = [
    ['surface_lat', 'surface_lat'],
    ['surface_lon', 'surface_lon'],
    ['kb_elevation_si', 'kb_elevation_si'],
    ['ground_elevation_si', 'ground_elevation_si'],
    ['water_depth_si', 'water_depth_si'],
    ['total_depth_planned_si', 'total_depth_planned_si'],
  ]
  for (const [target, source] of numbers) {
    const next = form[source]
    if (next === before[source]) continue
    Object.assign(payload, { [target]: numberOrNull(String(next)) })
  }
  if (form.is_offshore !== well.is_offshore) payload.is_offshore = form.is_offshore
  if (form.spud_date !== before.spud_date) payload.spud_date = form.spud_date || null
  if (form.release_date !== before.release_date) payload.release_date = form.release_date || null
  if (form.target_formations !== before.target_formations) {
    payload.target_formations = form.target_formations
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line !== '')
  }
  if (form.tags !== before.tags) payload.tags = commaToList(form.tags)
  return payload
}

export function WellIdentityPanel({
  well,
  rigs,
  fields,
  onChanged,
}: {
  /** The well as `GET /wells/{id}/structure` returns it: its row plus the transitions it may take. */
  well: Well & WithTransitions
  rigs: Rig[]
  fields: Array<{ id: string; name: string }>
  onChanged: () => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const editGate = useActionGate('well.update')
  const lifecycleGate = useActionGate('well.lifecycle')
  const rigGate = useActionGate('well.assign_rig')

  const [form, setForm] = useState<IdentityForm>(() => toForm(well))
  const [reason, setReason] = useState('')
  const [transition, setTransition] = useState('')
  const [transitionReason, setTransitionReason] = useState('')
  const [rigChoice, setRigChoice] = useState(well.rig_id ?? '')

  // A different well (or a server-side change to this one) replaces the form: the panel is about the
  // well it is showing, and keeping a half-edited copy of another well's values would be a trap.
  useEffect(() => {
    setForm(toForm(well))
    setReason('')
    setRigChoice(well.rig_id ?? '')
  }, [well])

  const set = <K extends keyof IdentityForm>(key: K, value: IdentityForm[K]) =>
    setForm((current) => ({ ...current, [key]: value }))

  const payload = useMemo(() => wellUpdatePayload(well, form), [well, form])
  const dirty = Object.keys(payload).length > 0

  const invalidate = () => {
    // Only what a well edit can invalidate: the well itself, the list that ranks it, and the well's
    // own structure/ledger. A blanket invalidation would re-read every panel in the application.
    void queryClient.invalidateQueries({ queryKey: ['well', well.id] })
    void queryClient.invalidateQueries({ queryKey: ['well-structure', well.id] })
    void queryClient.invalidateQueries({ queryKey: ['well-audit-log', well.id] })
    void queryClient.invalidateQueries({ queryKey: ['wells'] })
  }

  const save = useMutation({
    mutationFn: () =>
      drillingApi.updateWell(
        well.id,
        { ...payload, reason: textOrNull(reason), expected_updated_at: well.updated_at ?? null },
        `ui-well-update-${well.id}-${crypto.randomUUID()}`,
      ),
    onSuccess: () => {
      setReason('')
      invalidate()
      onChanged()
    },
  })

  const move = useMutation({
    mutationFn: () =>
      drillingApi.transitionWell(
        well.id,
        { target: transition, reason: textOrNull(transitionReason) },
        `ui-well-lifecycle-${well.id}-${crypto.randomUUID()}`,
      ),
    onSuccess: () => {
      setTransition('')
      setTransitionReason('')
      invalidate()
      onChanged()
    },
  })

  const assign = useMutation({
    mutationFn: (rigId: string | null) =>
      drillingApi.assignRig(
        well.id,
        { rig_id: rigId, reason: textOrNull(reason) },
        `ui-well-rig-${well.id}-${crypto.randomUUID()}`,
      ),
    onSuccess: () => {
      invalidate()
      onChanged()
    },
  })

  const allowed = well.allowed_transitions ?? []
  const stale = save.error instanceof ApiError && save.error.isConflict

  return (
    <div className="space-y-4">
      <Card
        title={t('master.identity')}
        subtitle={t('master.versionNote', { version: formatDateTime(well.updated_at ?? null) })}
        actions={<Badge tone="neutral">{formatStatus(well.status)}</Badge>}
      >
        <p className="mb-3 text-xs text-graphite-500">{t('master.identityNote')}</p>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <FormField label={t('master.name')} required>
            <TextInput value={form.name} onChange={(value) => set('name', value)} />
          </FormField>
          <FormField label={t('master.wellType')}>
            <Select
              value={form.well_type}
              onChange={(value) => set('well_type', value)}
              options={WELL_TYPES.map((value) => ({ value, label: humanise(value) }))}
            />
          </FormField>
          <FormField label={t('master.field')}>
            <Select
              value={form.field_id}
              onChange={(value) => set('field_id', value)}
              options={fields.map((field) => ({ value: field.id, label: field.name }))}
              placeholder={t('master.notRecorded')}
            />
          </FormField>
          <FormField label={t('master.uwi')} hint={t('master.uwiHint')}>
            <TextInput value={form.uwi} onChange={(value) => set('uwi', value)} ltr />
          </FormField>
          <FormField label={t('master.apiNumber')}>
            <TextInput value={form.api_number} onChange={(value) => set('api_number', value)} ltr />
          </FormField>
          <FormField label={t('master.operator')}>
            <TextInput value={form.operator} onChange={(value) => set('operator', value)} />
          </FormField>
          <FormField label={t('master.datum')}>
            <Select
              value={form.elevation_datum}
              onChange={(value) => set('elevation_datum', value)}
              options={ELEVATION_DATUMS.map((value) => ({ value, label: value.toUpperCase() }))}
            />
          </FormField>
          <FormField label={t('master.kbElevation')}>
            <TextInput
              type="number"
              value={form.kb_elevation_si}
              onChange={(value) => set('kb_elevation_si', value)}
            />
          </FormField>
          <FormField label={t('master.groundElevation')}>
            <TextInput
              type="number"
              value={form.ground_elevation_si}
              onChange={(value) => set('ground_elevation_si', value)}
            />
          </FormField>
          <FormField label={t('master.waterDepth')}>
            <TextInput
              type="number"
              value={form.water_depth_si}
              onChange={(value) => set('water_depth_si', value)}
            />
          </FormField>
          <FormField label={t('master.surfaceLat')}>
            <TextInput type="number" value={form.surface_lat} onChange={(value) => set('surface_lat', value)} />
          </FormField>
          <FormField label={t('master.surfaceLon')}>
            <TextInput type="number" value={form.surface_lon} onChange={(value) => set('surface_lon', value)} />
          </FormField>
          <FormField label={t('master.slot')}>
            <TextInput value={form.slot} onChange={(value) => set('slot', value)} ltr />
          </FormField>
          <FormField label={t('master.padName')}>
            <TextInput value={form.pad_name} onChange={(value) => set('pad_name', value)} />
          </FormField>
          <FormField label={t('master.plannedTd')}>
            <TextInput
              type="number"
              value={form.total_depth_planned_si}
              onChange={(value) => set('total_depth_planned_si', value)}
            />
          </FormField>
          <FormField label={t('master.spudDate')}>
            <TextInput type="date" value={form.spud_date} onChange={(value) => set('spud_date', value)} />
          </FormField>
          <FormField label={t('master.releaseDate')}>
            <TextInput type="date" value={form.release_date} onChange={(value) => set('release_date', value)} />
          </FormField>
        </div>

        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <FormField label={t('master.objectives')}>
            <TextArea value={form.objectives} onChange={(value) => set('objectives', value)} />
          </FormField>
          <div className="space-y-3">
            <FormField label={t('master.targetFormations')} hint={t('master.targetFormationsHint')}>
              <TextArea
                rows={3}
                value={form.target_formations}
                onChange={(value) => set('target_formations', value)}
              />
            </FormField>
            <FormField label={t('master.tags')} hint={t('master.tagsHint')}>
              <TextInput value={form.tags} onChange={(value) => set('tags', value)} />
            </FormField>
          </div>
        </div>

        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <FormField label={t('master.reason')} hint={t('master.reasonHint')}>
            <TextInput value={reason} onChange={setReason} />
          </FormField>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <ReadOnlyValue label={t('master.project')} value={well.project_id} />
            <ReadOnlyValue label={t('master.well')} value={well.id} />
            <ReadOnlyValue label="Twin" value={well.twin_state} />
          </div>
        </div>

        {stale && (
          <p className="mt-3 text-xs text-warning" data-testid="stale-edit">
            {t('master.stale')}
          </p>
        )}
        {save.error && !stale && <div className="mt-3">{<ErrorState error={save.error} />}</div>}
        {save.isSuccess && <p className="mt-3 text-xs text-ok">{t('master.saved')}</p>}
        {!dirty && <p className="mt-3 text-xs text-graphite-500">{t('master.noChanges')}</p>}

        <div className="mt-3 flex items-center gap-2">
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            disabled={!dirty || save.isPending || editGate.suggestDisabled}
            title={editGate.reason || undefined}
            data-testid="well-save"
            data-gate-state={editGate.state}
          >
            {save.isPending ? t('master.saving') : t('master.save')}
          </Button>
          {editGate.suggestDisabled && editGate.reason && (
            <span className="text-[11px] text-graphite-500">{editGate.reason}</span>
          )}
        </div>
      </Card>

      <Card
        title={t('master.lifecycle')}
        subtitle={formatStatus(well.status)}
        actions={
          <span className="font-mono text-[11px] text-graphite-500" dir="ltr">
            {allowed.length > 0 ? allowed.join(' · ') : '—'}
          </span>
        }
      >
        <p className="mb-3 text-xs text-graphite-500">{t('master.lifecycleHint')}</p>
        {allowed.length === 0 ? (
          <p className="text-xs" data-testid="lifecycle-terminal">
            {t('master.terminal')}
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-3">
            <FormField label={t('master.transition')}>
              <Select
                value={transition}
                onChange={setTransition}
                options={allowed.map((target) => ({ value: target, label: formatStatus(target) }))}
                placeholder={t('master.transition')}
              />
            </FormField>
            <FormField label={t('master.reason')} hint={t('master.transitionReasonHint')}>
              <TextInput value={transitionReason} onChange={setTransitionReason} />
            </FormField>
            <div className="flex items-end">
              <Button
                onClick={() => move.mutate()}
                disabled={transition === '' || move.isPending || lifecycleGate.suggestDisabled}
                title={lifecycleGate.reason || undefined}
                data-testid="well-transition"
                data-gate-state={lifecycleGate.state}
              >
                {move.isPending ? t('master.transitioning') : t('master.transition')}
              </Button>
            </div>
          </div>
        )}
        {move.error && <div className="mt-3">{<ErrorState error={move.error} />}</div>}
      </Card>

      <Card title={t('master.rigAssignment')} subtitle={well.rig_id ?? t('master.noRig')}>
        <div className="grid gap-3 sm:grid-cols-3">
          <FormField label={t('master.rig')}>
            <Select
              value={rigChoice}
              onChange={setRigChoice}
              options={rigs.map((rig) => ({
                value: rig.id,
                label: rig.current_well_id && rig.current_well_id !== well.id ? `${rig.name} — ${t('master.rigBusy')}` : rig.name,
              }))}
              placeholder={t('master.noRig')}
            />
          </FormField>
          <div className="flex items-end gap-2">
            <Button
              onClick={() => assign.mutate(rigChoice)}
              disabled={assign.isPending || rigGate.suggestDisabled || rigChoice === ''}
              title={rigGate.reason || undefined}
              data-testid="well-assign-rig"
              data-gate-state={rigGate.state}
            >
              {t('master.attachRig')}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setRigChoice('')
                assign.mutate(null)
              }}
              disabled={well.rig_id === null || assign.isPending || rigGate.suggestDisabled}
              data-testid="well-detach-rig"
            >
              {t('master.detachRig')}
            </Button>
          </div>
        </div>
        {assign.error && <div className="mt-3">{<ErrorState error={assign.error} />}</div>}
      </Card>
    </div>
  )
}
