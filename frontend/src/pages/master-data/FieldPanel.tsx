/**
 * Field master data: the level between a project and its wells.
 *
 * A field is the geologist's unit and the planner's filter, so what makes it findable matters as much
 * as its geometry: a field is known by more than one name — a discovery name, a licence block name, a
 * local spelling — and those are recorded as searchable aliases rather than buried in a notes column.
 * The aliases are sent to the server, which searches them alongside the name, so "search by alias"
 * works because the *server* knows the aliases, not because this screen filters the rows it happened to
 * load.
 *
 * The create/edit split follows the API: a field is created active and retired deliberately, so
 * `status` appears only as something the server owns.
 */

import { useMutation } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import { drillingApi, type FieldCreatePayload, type FieldUpdatePayload } from '../../api/endpoints'
import type { Field } from '../../api/types'
import { Badge, Button, Card, Drawer, EmptyState, ErrorState, Table } from '../../components/common'
import { useActionGate } from '../../hooks/useActionGate'
import { useI18n } from '../../i18n'
import { formatDateTime } from '../../lib/format'
import { FormField, TextArea, TextInput, commaToList, numberOrNull, numberInput, textOrNull } from './form'

function FieldForm({
  projectId,
  field,
  open,
  onClose,
  onSaved,
}: {
  projectId: string
  field: Field | null
  open: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useI18n()
  const gate = useActionGate(field ? 'field.update' : 'field.create')
  const [form, setForm] = useState({
    name: '',
    aliases: '',
    country: '',
    basin: '',
    water_depth_si: '',
    centroid_lat: '',
    centroid_lon: '',
    notes: '',
  })

  useEffect(() => {
    setForm({
      name: field?.name ?? '',
      aliases: (field?.aliases ?? []).join(', '),
      country: field?.country ?? '',
      basin: field?.basin ?? '',
      water_depth_si: numberInput(field?.water_depth_si ?? null),
      centroid_lat: numberInput(field?.centroid_lat ?? null),
      centroid_lon: numberInput(field?.centroid_lon ?? null),
      notes: field?.notes ?? '',
    })
  }, [field, open])

  const set = (key: keyof typeof form, value: string) => setForm((current) => ({ ...current, [key]: value }))

  const save = useMutation({
    mutationFn: () => {
      const aliases = commaToList(form.aliases)
      if (field) {
        const payload: FieldUpdatePayload = {}
        if (form.name !== field.name) payload.name = form.name
        if (form.country !== (field.country ?? '')) payload.country = textOrNull(form.country)
        if (form.basin !== (field.basin ?? '')) payload.basin = textOrNull(form.basin)
        if (form.notes !== (field.notes ?? '')) payload.notes = textOrNull(form.notes)
        if (form.aliases !== field.aliases.join(', ')) payload.aliases = aliases
        if (form.water_depth_si !== numberInput(field.water_depth_si)) {
          payload.water_depth_si = numberOrNull(form.water_depth_si)
        }
        if (form.centroid_lat !== numberInput(field.centroid_lat)) {
          payload.centroid_lat = numberOrNull(form.centroid_lat)
        }
        if (form.centroid_lon !== numberInput(field.centroid_lon)) {
          payload.centroid_lon = numberOrNull(form.centroid_lon)
        }
        return drillingApi.updateField(
          field.id,
          { ...payload, expected_updated_at: field.updated_at ?? null },
          `ui-field-update-${field.id}-${crypto.randomUUID()}`,
        )
      }
      const payload: FieldCreatePayload = { project_id: projectId, name: form.name.trim() }
      if (aliases.length > 0) payload.aliases = aliases
      const optional: Array<[keyof FieldCreatePayload, string]> = [
        ['country', form.country],
        ['basin', form.basin],
        ['notes', form.notes],
      ]
      for (const [key, value] of optional) {
        const parsed = textOrNull(value)
        if (parsed !== null) Object.assign(payload, { [key]: parsed })
      }
      const numbers: Array<[keyof FieldCreatePayload, string]> = [
        ['water_depth_si', form.water_depth_si],
        ['centroid_lat', form.centroid_lat],
        ['centroid_lon', form.centroid_lon],
      ]
      for (const [key, value] of numbers) {
        const parsed = numberOrNull(value)
        if (parsed !== null) Object.assign(payload, { [key]: parsed })
      }
      return drillingApi.createField(payload, `ui-field-create-${projectId}-${crypto.randomUUID()}`)
    },
    onSuccess: () => {
      onSaved()
      onClose()
    },
  })

  const stale = save.error instanceof ApiError && save.error.isConflict
  const missingName = form.name.trim() === ''

  return (
    <Drawer open={open} title={field ? `${t('master.edit')} — ${field.name}` : t('master.createFieldTitle')} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-xs text-graphite-500">{t('master.createFieldNote')}</p>
        <FormField label={t('master.fieldName')} required>
          <TextInput value={form.name} onChange={(value) => set('name', value)} />
        </FormField>
        <FormField label={t('master.aliases')} hint={t('master.aliasesHint')}>
          <TextInput value={form.aliases} onChange={(value) => set('aliases', value)} />
        </FormField>
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label={t('master.country')}>
            <TextInput value={form.country} onChange={(value) => set('country', value)} />
          </FormField>
          <FormField label={t('master.basin')}>
            <TextInput value={form.basin} onChange={(value) => set('basin', value)} />
          </FormField>
          <FormField label={t('master.waterDepth')}>
            <TextInput type="number" value={form.water_depth_si} onChange={(value) => set('water_depth_si', value)} />
          </FormField>
          <FormField label={t('master.centroidLat')}>
            <TextInput type="number" value={form.centroid_lat} onChange={(value) => set('centroid_lat', value)} />
          </FormField>
          <FormField label={t('master.centroidLon')}>
            <TextInput type="number" value={form.centroid_lon} onChange={(value) => set('centroid_lon', value)} />
          </FormField>
        </div>
        <FormField label={t('master.notes')}>
          <TextArea value={form.notes} onChange={(value) => set('notes', value)} />
        </FormField>

        {stale && (
          <p className="text-xs text-warning" data-testid="stale-field">
            {t('master.stale')}
          </p>
        )}
        {save.error && !stale && <ErrorState error={save.error} />}

        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            disabled={missingName || save.isPending || gate.suggestDisabled}
            title={gate.reason || undefined}
            data-testid="field-save"
            data-gate-state={gate.state}
          >
            {save.isPending ? t('master.saving') : field ? t('master.save') : t('master.create')}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t('master.cancel')}
          </Button>
        </div>
      </div>
    </Drawer>
  )
}

export function FieldPanel({
  projectId,
  fields,
  onChanged,
  onSelectField,
}: {
  projectId: string
  fields: Field[]
  onChanged: () => void
  onSelectField?: (fieldId: string) => void
}) {
  const { t } = useI18n()
  const createGate = useActionGate('field.create')
  const [editing, setEditing] = useState<Field | null>(null)
  const [creating, setCreating] = useState(false)

  return (
    <Card
      title={t('master.fields')}
      actions={
        <Button
          size="sm"
          onClick={() => {
            setEditing(null)
            setCreating(true)
          }}
          disabled={createGate.suggestDisabled}
          title={createGate.reason || undefined}
          data-testid="field-create"
          data-gate-state={createGate.state}
        >
          {t('master.createFieldTitle')}
        </Button>
      }
    >
      <Table
        rows={fields}
        rowKey={(row) => row.id}
        onRowClick={(row) => onSelectField?.(row.id)}
        empty={<EmptyState message={t('master.emptyFields')} />}
        columns={[
          {
            key: 'name',
            header: t('master.fieldName'),
            render: (row) => (
              <span>
                {row.name}
                <span className="block font-mono text-[10px] text-graphite-500" dir="ltr">
                  {row.id}
                </span>
              </span>
            ),
          },
          {
            key: 'aliases',
            header: t('master.aliases'),
            render: (row) =>
              row.aliases.length === 0 ? <span className="text-graphite-400">—</span> : row.aliases.join(' · '),
          },
          { key: 'country', header: t('master.country'), render: (row) => row.country ?? '—' },
          { key: 'basin', header: t('master.basin'), render: (row) => row.basin ?? '—' },
          { key: 'status', header: t('master.status'), render: (row) => <Badge tone="neutral">{row.status}</Badge> },
          {
            key: 'updated',
            header: t('master.changed'),
            render: (row) => formatDateTime(row.updated_at ?? null),
          },
          {
            key: 'edit',
            header: '',
            render: (row) => (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setCreating(false)
                  setEditing(row)
                }}
                data-testid={`field-edit-${row.id}`}
              >
                {t('master.edit')}
              </Button>
            ),
          },
        ]}
      />

      {(creating || editing) && (
        <FieldForm
          projectId={projectId}
          field={editing}
          open
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={onChanged}
        />
      )}
    </Card>
  )
}
