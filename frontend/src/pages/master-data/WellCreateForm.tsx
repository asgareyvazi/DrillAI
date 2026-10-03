/**
 * Creating a well.
 *
 * This form is the master-data entry point's first act, so it carries the identity a well is *known*
 * by rather than everything a well can hold: the name, the project and field it belongs to, its
 * regulator identifiers, its location and datum, and the plan's total depth. Life cycle status is
 * absent on purpose — a well is created planned and moves by a recorded transition, and offering a
 * status here would let the form assert a state the platform has no transition record for. The server
 * refuses the key outright, so sending it would be a dropped instruction.
 *
 * Two behaviours are deliberate and worth naming:
 *
 * * **The idempotency key lives for one submission.** It is generated when the form is opened and
 *   regenerated after a successful create. Retrying a request whose response was lost therefore
 *   returns the well that was created, instead of creating a second one.
 * * **A blank number field is `null`.** "Planned TD" left empty is "not recorded"; it is never sent as
 *   `0`, which would be a depth somebody would later read as a real plan.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { ApiError } from '../../api/client'
import { drillingApi, type WellCreatePayload } from '../../api/endpoints'
import type { Field, Project, Rig, Well } from '../../api/types'
import { Badge, Button, ErrorState } from '../../components/common'
import { useActionGate } from '../../hooks/useActionGate'
import { useI18n } from '../../i18n'
import { Checkbox, FormField, Select, TextArea, TextInput, commaToList, numberOrNull, textOrNull } from './form'

/** The canonical vocabularies, as the server defines them. Order is the server's order. */
export const WELL_TYPES = [
  'exploration',
  'appraisal',
  'development_producer',
  'development_injector',
  'observation',
  'water_source',
  'disposal',
  'sidetrack',
  'reentry',
] as const

export const ELEVATION_DATUMS = ['rkb', 'msl', 'gl', 'cf', 'derrick_floor'] as const

interface FormState {
  name: string
  project_id: string
  field_id: string
  uwi: string
  api_number: string
  well_type: string
  operator: string
  rig_id: string
  is_offshore: boolean
  surface_lat: string
  surface_lon: string
  kb_elevation_si: string
  ground_elevation_si: string
  water_depth_si: string
  elevation_datum: string
  slot: string
  pad_name: string
  total_depth_planned_si: string
  spud_date: string
  objectives: string
  target_formations: string
  tags: string
}

function blank(projectId: string): FormState {
  return {
    name: '',
    project_id: projectId,
    field_id: '',
    uwi: '',
    api_number: '',
    // The server's own default, written out so the select shows what the create will actually do.
    well_type: 'development_producer',
    operator: '',
    rig_id: '',
    is_offshore: false,
    surface_lat: '',
    surface_lon: '',
    kb_elevation_si: '',
    ground_elevation_si: '',
    water_depth_si: '',
    elevation_datum: 'msl',
    slot: '',
    pad_name: '',
    total_depth_planned_si: '',
    spud_date: '',
    objectives: '',
    target_formations: '',
    tags: '',
  }
}

/**
 * The body, built from the form.
 *
 * Absent fields are omitted rather than sent as `null`: the two mean different things to the API (a
 * key the caller left out keeps the server's default, while an explicit `null` clears a value), and
 * omitting is what "the operator did not say" means.
 */
export function wellCreatePayload(form: FormState): WellCreatePayload {
  const payload: WellCreatePayload = {
    project_id: form.project_id,
    name: form.name.trim(),
  }
  const optionalText: Array<[keyof WellCreatePayload, string]> = [
    ['field_id', form.field_id],
    ['uwi', form.uwi],
    ['api_number', form.api_number],
    ['operator', form.operator],
    ['rig_id', form.rig_id],
    ['slot', form.slot],
    ['pad_name', form.pad_name],
    ['objectives', form.objectives],
  ]
  for (const [key, value] of optionalText) {
    const parsed = textOrNull(value)
    if (parsed !== null) Object.assign(payload, { [key]: parsed })
  }
  const optionalNumbers: Array<[keyof WellCreatePayload, string]> = [
    ['surface_lat', form.surface_lat],
    ['surface_lon', form.surface_lon],
    ['kb_elevation_si', form.kb_elevation_si],
    ['ground_elevation_si', form.ground_elevation_si],
    ['water_depth_si', form.water_depth_si],
    ['total_depth_planned_si', form.total_depth_planned_si],
  ]
  for (const [key, value] of optionalNumbers) {
    const parsed = numberOrNull(value)
    if (parsed !== null) Object.assign(payload, { [key]: parsed })
  }
  if (form.well_type) payload.well_type = form.well_type
  if (form.elevation_datum) payload.elevation_datum = form.elevation_datum
  if (form.is_offshore) payload.is_offshore = true
  const formations = form.target_formations
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
  if (formations.length > 0) payload.target_formations = formations
  const tags = commaToList(form.tags)
  if (tags.length > 0) payload.tags = tags
  if (form.spud_date) payload.spud_date = form.spud_date
  return payload
}

export function WellCreateForm({
  projects,
  fields,
  rigs,
  defaultProjectId,
  onCreated,
}: {
  projects: Project[]
  fields: Field[]
  rigs: Rig[]
  defaultProjectId?: string
  onCreated?: (well: Well) => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const gate = useActionGate('well.create')
  const [form, setForm] = useState<FormState>(() => blank(defaultProjectId ?? ''))
  // One key per attempt, kept until the attempt succeeds. A retry after a lost response therefore
  // reaches the server as the same request, not as a second well.
  const [idempotencyKey, setKey] = useState(() => `ui-well-create-${crypto.randomUUID()}`)

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }))

  const projectFields = useMemo(
    () => fields.filter((field) => field.project_id === form.project_id),
    [fields, form.project_id],
  )

  const create = useMutation({
    mutationFn: () => drillingApi.createWell(wellCreatePayload(form), idempotencyKey),
    onSuccess: (well) => {
      // Targeted invalidation only: the wells list and the project's well count changed. Nothing else
      // in the application has any reason to be re-read because a well was created.
      void queryClient.invalidateQueries({ queryKey: ['wells'] })
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      setForm(blank(defaultProjectId ?? ''))
      setKey(`ui-well-create-${crypto.randomUUID()}`)
      onCreated?.(well)
    },
  })

  const missingRequired = form.name.trim() === '' || form.project_id === ''
  const canSubmit = !missingRequired && !create.isPending && !gate.suggestDisabled

  return (
    <form
      className="space-y-3"
      data-testid="well-create-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (canSubmit) create.mutate()
      }}
    >
      <p className="text-xs text-graphite-500">{t('master.createWellNote')}</p>

      <div className="grid gap-3 sm:grid-cols-2">
        <FormField label={t('master.name')} required>
          <TextInput value={form.name} onChange={(value) => set('name', value)} name="name" />
        </FormField>
        <FormField label={t('master.project')} required>
          <Select
            value={form.project_id}
            onChange={(value) => {
              set('project_id', value)
              // A field belongs to one project: a project change clears a field that no longer applies
              // rather than silently sending a field id the server must refuse.
              set('field_id', '')
            }}
            options={projects.map((project) => ({ value: project.id, label: project.name }))}
            placeholder={t('wells.allProjects')}
          />
        </FormField>
        <FormField label={t('master.field')}>
          <Select
            value={form.field_id}
            onChange={(value) => set('field_id', value)}
            options={projectFields.map((field) => ({ value: field.id, label: field.name }))}
            placeholder={t('master.notRecorded')}
            disabled={form.project_id === ''}
          />
        </FormField>
        <FormField label={t('master.operator')}>
          <TextInput value={form.operator} onChange={(value) => set('operator', value)} />
        </FormField>
        <FormField label={t('master.uwi')} hint={t('master.uwiHint')}>
          <TextInput value={form.uwi} onChange={(value) => set('uwi', value)} ltr name="uwi" />
        </FormField>
        <FormField label={t('master.apiNumber')}>
          <TextInput
            value={form.api_number}
            onChange={(value) => set('api_number', value)}
            ltr
            name="api_number"
          />
        </FormField>
        <FormField label={t('master.wellType')}>
          <Select
            value={form.well_type}
            onChange={(value) => set('well_type', value)}
            options={WELL_TYPES.map((value) => ({ value, label: value.replaceAll('_', ' ') }))}
          />
        </FormField>
        <FormField label={t('master.rig')}>
          <Select
            value={form.rig_id}
            onChange={(value) => set('rig_id', value)}
            options={rigs.map((rig) => ({
              value: rig.id,
              // A rig already working a well is still selectable — a well can be *planned* against a
              // rig — but the name says so, because the alternative is finding out at the pad.
              label: rig.current_well_id ? `${rig.name} — ${t('master.rigBusy')}` : rig.name,
            }))}
            placeholder={t('master.noRig')}
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
        <FormField label={t('master.datum')}>
          <Select
            value={form.elevation_datum}
            onChange={(value) => set('elevation_datum', value)}
            options={ELEVATION_DATUMS.map((value) => ({ value, label: value.toUpperCase() }))}
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
          {/* A date input yields `YYYY-MM-DD`; the server needs an offset-bearing timestamp, and it
              applies the offset rather than the browser guessing a timezone. */}
          <TextInput type="date" value={form.spud_date} onChange={(value) => set('spud_date', value)} />
        </FormField>
        <FormField label={t('master.offshore')}>
          <Checkbox
            checked={form.is_offshore}
            onChange={(checked) => set('is_offshore', checked)}
            label={t('master.offshore')}
          />
        </FormField>
      </div>

      <FormField label={t('master.objectives')}>
        <TextArea value={form.objectives} onChange={(value) => set('objectives', value)} />
      </FormField>
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

      {create.error && <ErrorState error={create.error} />}
      {create.isSuccess && (
        <p className="text-xs text-ok" data-testid="well-created">
          {t('master.saved')}
        </p>
      )}

      <div className="flex items-center gap-2">
        <Button
          type="submit"
          variant="primary"
          disabled={!canSubmit}
          title={gate.reason || undefined}
          data-testid="well-create-submit"
          data-gate-state={gate.state}
        >
          {create.isPending ? t('master.creating') : t('master.create')}
        </Button>
        {gate.suggestDisabled && gate.reason && (
          <span className="text-[11px] text-graphite-500">{gate.reason}</span>
        )}
        {create.error instanceof ApiError && create.error.isConflict && (
          <Badge tone="warning">{create.error.code}</Badge>
        )}
      </div>
    </form>
  )
}
