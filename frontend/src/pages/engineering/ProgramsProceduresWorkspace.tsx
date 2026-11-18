/** Well-scoped authoring and governance for structured drilling programs and controlled procedures. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type {
  AdvisorAnswer,
  EngineeringProductArtifact,
  EngineeringProductCreatePayload,
  EngineeringProductKind,
  EngineeringProductVersion,
  UnitCatalogue,
  UnitRow,
  Wellbore,
} from '../../api/types'
import { PageHeader } from '../../components/layout/AppShell'
import { Badge, Button, Card, EmptyState, ErrorState, Field, Json, Loading } from '../../components/common'
import { useI18n } from '../../i18n'

const CONTROL = 'w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-950'
const TEXTAREA = `${CONTROL} min-h-16`
const FALLBACK_UNITS: UnitCatalogue['items'] = []

type QuantityState = 'known' | 'unknown' | 'conflicting'
type SourceKind = 'operator_entered' | 'source_document' | 'engine_run'
type SourceReferenceDraft = { kind: 'document' | 'engine_run'; id: string; extra: Record<string, unknown> }
type QuantityDraft = {
  state: QuantityState
  dimension: string
  value: string
  unit: string
  sourceKind: SourceKind
  sourceRef: string
  reason: string
  observationsText: string
  extra: Record<string, unknown>
}
type ObjectiveDraft = { statement: string; targetMd: QuantityDraft | null; extra: Record<string, unknown> }
type BasisDraft = {
  topic: string
  statement: string
  basis: 'reported' | 'assumption' | 'unknown' | 'conflicting'
  reason: string
  references: SourceReferenceDraft[]
  extra: Record<string, unknown>
}
type ParameterDraft = { key: string; label: string; dimension: string; value: QuantityDraft; extra: Record<string, unknown> }
type OperationDraft = { activity: string; purpose: string; acceptanceText: string; duration: QuantityDraft | null; extra: Record<string, unknown> }
type ProgramSectionDraft = {
  name: string
  sectionId: string
  holeDiameter: QuantityDraft
  mdFrom: QuantityDraft
  mdTo: QuantityDraft
  mudMin: QuantityDraft | null
  mudMax: QuantityDraft | null
  parameters: ParameterDraft[]
  operations: OperationDraft[]
  extra: Record<string, unknown>
}
type RiskDraft = {
  hazard: string
  causesText: string
  consequencesText: string
  safeguardsText: string
  contingency: string
  extra: Record<string, unknown>
}
type PrerequisiteDraft = { requirement: string; verification: string; extra: Record<string, unknown> }
type ProcedureStepDraft = {
  title: string
  instruction: string
  responsibleRole: string
  preconditionsText: string
  hazardsText: string
  controlsText: string
  verificationText: string
  response: string
  holdPoint: boolean
  extra: Record<string, unknown>
}
type StopWorkDraft = { trigger: string; immediateResponse: string; escalationRole: string; extra: Record<string, unknown> }
type ProductDraft = {
  purpose: string
  objectives: ObjectiveDraft[]
  designBasis: BasisDraft[]
  sections: ProgramSectionDraft[]
  risks: RiskDraft[]
  assumptions: BasisDraft[]
  scopeStatement: string
  prerequisites: PrerequisiteDraft[]
  steps: ProcedureStepDraft[]
  stopWork: StopWorkDraft[]
  sourceReferences: SourceReferenceDraft[]
  extra: Record<string, unknown>
}

type ProductSave = {
  title?: string
  code?: string
  wellboreId?: string
  payload: Record<string, unknown>
  linkedProgramVersionId?: string
  changeReason?: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function row(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {}
}

function rows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asLines(value: unknown): string {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').join('\n') : ''
}

function lines(value: string): string[] {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function sourceReferences(value: unknown): SourceReferenceDraft[] {
  return rows(value)
    .filter((item) => (item.kind === 'document' || item.kind === 'engine_run') && typeof item.id === 'string')
    .map((item) => ({ kind: item.kind as 'document' | 'engine_run', id: text(item.id), extra: item }))
}

function blankQuantity(dimension: string): QuantityDraft {
  return {
    state: 'unknown',
    dimension,
    value: '',
    unit: '',
    sourceKind: 'operator_entered',
    sourceRef: '',
    reason: '',
    observationsText: '[]',
    extra: {},
  }
}

function quantityDraft(value: unknown, dimension: string): QuantityDraft {
  const source = row(value)
  const state = source.state === 'known' || source.state === 'conflicting' ? source.state : 'unknown'
  const observations = Array.isArray(source.observations) ? source.observations : []
  return {
    state,
    dimension: text(source.dimension) || dimension,
    value: source.value === undefined || source.value === null ? '' : String(source.value),
    unit: text(source.unit),
    sourceKind:
      source.source_kind === 'source_document' || source.source_kind === 'engine_run'
        ? source.source_kind
        : 'operator_entered',
    sourceRef: text(source.source_ref),
    reason: text(source.reason),
    observationsText: JSON.stringify(observations.map((item) => withoutDerived(row(item))), null, 2),
    extra: source,
  }
}

function withoutDerived(value: Record<string, unknown>): Record<string, unknown> {
  const result = { ...value }
  delete result.value_si
  delete result.canonical_unit
  delete result.entered_by
  delete result.entered_at
  return result
}

function quantityPayload(draft: QuantityDraft, label: string): Record<string, unknown> {
  const extra = withoutDerived(draft.extra)
  if (draft.state === 'known') {
    const value = Number(draft.value)
    if (!draft.value.trim() || !Number.isFinite(value)) throw new Error(`${label}: enter a finite numeric value or mark it unknown.`)
    if (!draft.unit) throw new Error(`${label}: select a registered unit; no unit is inferred.`)
    if (draft.sourceKind !== 'operator_entered' && !draft.sourceRef.trim()) {
      throw new Error(`${label}: provide the source document or registered engine-run ID.`)
    }
    return {
      ...extra,
      state: 'known',
      dimension: draft.dimension,
      value,
      unit: draft.unit,
      source_kind: draft.sourceKind,
      source_ref: draft.sourceKind === 'operator_entered' ? null : draft.sourceRef.trim(),
      reason: null,
      observations: [],
    }
  }
  if (!draft.reason.trim()) throw new Error(`${label}: state why this value is unknown or unresolved.`)
  if (draft.state === 'unknown') {
    return {
      ...extra,
      state: 'unknown',
      dimension: draft.dimension,
      value: null,
      unit: null,
      source_kind: null,
      source_ref: null,
      reason: draft.reason.trim(),
      observations: [],
    }
  }
  let observations: unknown
  try {
    observations = JSON.parse(draft.observationsText)
  } catch {
    throw new Error(`${label}: conflicting observations must be valid JSON.`)
  }
  if (!Array.isArray(observations) || observations.length < 2 || !observations.every(isRecord)) {
    throw new Error(`${label}: conflicting state requires at least two retained observations.`)
  }
  return {
    ...extra,
    state: 'conflicting',
    dimension: draft.dimension,
    value: null,
    unit: null,
    source_kind: null,
    source_ref: null,
    reason: draft.reason.trim(),
    observations: observations.map((item) => withoutDerived(row(item))),
  }
}

function sourceReferencePayload(references: SourceReferenceDraft[]): Record<string, unknown>[] {
  return references.filter((item) => item.id.trim()).map((item) => ({ ...item.extra, kind: item.kind, id: item.id.trim() }))
}

function emptyBasis(): BasisDraft {
  return { topic: '', statement: '', basis: 'unknown', reason: '', references: [], extra: {} }
}

function emptySection(): ProgramSectionDraft {
  return {
    name: '',
    sectionId: '',
    holeDiameter: blankQuantity('length'),
    mdFrom: blankQuantity('length'),
    mdTo: blankQuantity('length'),
    mudMin: null,
    mudMax: null,
    parameters: [],
    operations: [],
    extra: {},
  }
}

function draftFromPayload(payload: Record<string, unknown> | undefined): ProductDraft {
  const source = payload ?? {}
  return {
    purpose: text(source.purpose),
    objectives: rows(source.objectives).map((item) => ({
      statement: text(item.statement),
      targetMd: item.target_md ? quantityDraft(item.target_md, 'length') : null,
      extra: item,
    })),
    designBasis: rows(source.design_basis).map((item) => ({
      topic: text(item.topic),
      statement: text(item.statement),
      basis: item.basis === 'reported' || item.basis === 'assumption' || item.basis === 'conflicting' ? item.basis : 'unknown',
      reason: text(item.reason),
      references: sourceReferences(item.source_references),
      extra: item,
    })),
    sections: rows(source.sections).map((item) => ({
      name: text(item.name),
      sectionId: text(item.section_id),
      holeDiameter: quantityDraft(item.hole_diameter, 'length'),
      mdFrom: quantityDraft(item.planned_md_from, 'length'),
      mdTo: quantityDraft(item.planned_md_to, 'length'),
      mudMin: item.mud_density_min ? quantityDraft(item.mud_density_min, 'density') : null,
      mudMax: item.mud_density_max ? quantityDraft(item.mud_density_max, 'density') : null,
      parameters: rows(item.operating_parameters).map((parameter) => ({
        key: text(parameter.key),
        label: text(parameter.label),
        dimension: text(row(parameter.value).dimension) || 'length',
        value: quantityDraft(parameter.value, text(row(parameter.value).dimension) || 'length'),
        extra: parameter,
      })),
      operations: rows(item.planned_operations).map((operation) => ({
        activity: text(operation.activity),
        purpose: text(operation.purpose),
        acceptanceText: asLines(operation.acceptance_criteria),
        duration: operation.planned_duration ? quantityDraft(operation.planned_duration, 'time') : null,
        extra: operation,
      })),
      extra: item,
    })),
    risks: rows(source.risks).map((item) => ({
      hazard: text(item.hazard),
      causesText: asLines(item.initiating_causes),
      consequencesText: asLines(item.consequences),
      safeguardsText: asLines(item.safeguards),
      contingency: text(item.contingency),
      extra: item,
    })),
    assumptions: rows(source.assumptions).map((item) => ({
      topic: text(item.topic),
      statement: text(item.statement),
      basis: item.basis === 'reported' || item.basis === 'assumption' || item.basis === 'conflicting' ? item.basis : 'unknown',
      reason: text(item.reason),
      references: sourceReferences(item.source_references),
      extra: item,
    })),
    scopeStatement: text(source.scope_statement),
    prerequisites: rows(source.prerequisites).map((item) => ({
      requirement: text(item.requirement),
      verification: text(item.verification),
      extra: item,
    })),
    steps: rows(source.steps).map((item) => ({
      title: text(item.title),
      instruction: text(item.instruction),
      responsibleRole: text(item.responsible_role),
      preconditionsText: asLines(item.preconditions),
      hazardsText: asLines(item.hazards),
      controlsText: asLines(item.control_measures),
      verificationText: asLines(item.verification_criteria),
      response: text(item.response_if_not_met),
      holdPoint: item.hold_point === true,
      extra: item,
    })),
    stopWork: rows(source.stop_work_conditions).map((item) => ({
      trigger: text(item.trigger),
      immediateResponse: text(item.immediate_response),
      escalationRole: text(item.escalation_role),
      extra: item,
    })),
    sourceReferences: sourceReferences(source.source_references),
    extra: { ...source },
  }
}

function buildPayload(kind: EngineeringProductKind, draft: ProductDraft): Record<string, unknown> {
  if (kind === 'program') {
    const objectives = draft.objectives.map((item, index) => {
      if (!item.statement.trim()) throw new Error(`Objective ${index + 1}: enter a statement.`)
      return {
        ...item.extra,
        sequence: index + 1,
        statement: item.statement.trim(),
        target_md: item.targetMd ? quantityPayload(item.targetMd, `Objective ${index + 1} target depth`) : null,
      }
    })
    const designBasis = draft.designBasis.filter((item) => item.topic.trim() || item.statement.trim()).map((item, index) => {
      if (!item.topic.trim() || !item.statement.trim()) throw new Error(`Design-basis item ${index + 1}: provide both topic and statement.`)
      if (item.basis !== 'reported' && !item.reason.trim()) throw new Error(`Design-basis item ${index + 1}: explain the unresolved basis.`)
      return {
        ...item.extra,
        topic: item.topic.trim(),
        statement: item.statement.trim(),
        basis: item.basis,
        reason: item.reason.trim() || null,
        source_references: sourceReferencePayload(item.references),
      }
    })
    const sections = draft.sections.map((section, index) => {
      if (!section.name.trim()) throw new Error(`Section ${index + 1}: enter a name.`)
      const parameters = section.parameters.filter((item) => item.key.trim() || item.label.trim()).map((item, parameterIndex) => {
        if (!item.key.trim() || !item.label.trim()) throw new Error(`Section ${index + 1}, parameter ${parameterIndex + 1}: enter both key and label.`)
        return {
          ...item.extra,
          key: item.key.trim(),
          label: item.label.trim(),
          value: quantityPayload(item.value, `Section ${index + 1} parameter ${item.label}`),
        }
      })
      const operations = section.operations.filter((item) => item.activity.trim() || item.purpose.trim()).map((item, operationIndex) => {
        if (!item.activity.trim() || !item.purpose.trim()) throw new Error(`Section ${index + 1}, operation ${operationIndex + 1}: enter both activity and purpose.`)
        return {
          ...item.extra,
          sequence: operationIndex + 1,
          activity: item.activity.trim(),
          purpose: item.purpose.trim(),
          acceptance_criteria: lines(item.acceptanceText),
          planned_duration: item.duration ? quantityPayload(item.duration, `Section ${index + 1} operation duration`) : null,
        }
      })
      return {
        ...section.extra,
        sequence: index + 1,
        name: section.name.trim(),
        section_id: section.sectionId || null,
        hole_diameter: quantityPayload(section.holeDiameter, `Section ${index + 1} hole diameter`),
        planned_md_from: quantityPayload(section.mdFrom, `Section ${index + 1} top measured depth`),
        planned_md_to: quantityPayload(section.mdTo, `Section ${index + 1} bottom measured depth`),
        mud_density_min: section.mudMin ? quantityPayload(section.mudMin, `Section ${index + 1} mud density minimum`) : null,
        mud_density_max: section.mudMax ? quantityPayload(section.mudMax, `Section ${index + 1} mud density maximum`) : null,
        operating_parameters: parameters,
        planned_operations: operations,
      }
    })
    const risks = draft.risks.filter((item) => item.hazard.trim()).map((item, index) => ({
      ...item.extra,
      sequence: index + 1,
      hazard: item.hazard.trim(),
      initiating_causes: lines(item.causesText),
      consequences: lines(item.consequencesText),
      safeguards: lines(item.safeguardsText),
      contingency: item.contingency.trim() || null,
    }))
    const assumptions = draft.assumptions.filter((item) => item.topic.trim() || item.statement.trim()).map((item, index) => {
      if (!item.topic.trim() || !item.statement.trim()) throw new Error(`Assumption ${index + 1}: provide both topic and statement.`)
      if (item.basis !== 'reported' && !item.reason.trim()) throw new Error(`Assumption ${index + 1}: explain why it is unresolved.`)
      return {
        ...item.extra,
        topic: item.topic.trim(),
        statement: item.statement.trim(),
        basis: item.basis,
        reason: item.reason.trim() || null,
        source_references: sourceReferencePayload(item.references),
      }
    })
    return { ...draft.extra, purpose: draft.purpose.trim(), objectives, design_basis: designBasis, sections, risks, assumptions }
  }

  const prerequisites = draft.prerequisites.filter((item) => item.requirement.trim() || item.verification.trim()).map((item, index) => {
    if (!item.requirement.trim() || !item.verification.trim()) throw new Error(`Prerequisite ${index + 1}: enter both requirement and verification.`)
    return { ...item.extra, sequence: index + 1, requirement: item.requirement.trim(), verification: item.verification.trim() }
  })
  const steps = draft.steps.filter((item) => item.title.trim() || item.instruction.trim()).map((item, index) => {
    if (!item.title.trim() || !item.instruction.trim() || !item.responsibleRole.trim() || !item.response.trim()) {
      throw new Error(`Procedure step ${index + 1}: complete title, instruction, responsible role, and response.`)
    }
    const verification = lines(item.verificationText)
    if (verification.length === 0) throw new Error(`Procedure step ${index + 1}: add at least one verification criterion.`)
    return {
      ...item.extra,
      sequence: index + 1,
      title: item.title.trim(),
      instruction: item.instruction.trim(),
      responsible_role: item.responsibleRole.trim(),
      preconditions: lines(item.preconditionsText),
      hazards: lines(item.hazardsText),
      control_measures: lines(item.controlsText),
      verification_criteria: verification,
      response_if_not_met: item.response.trim(),
      hold_point: item.holdPoint,
    }
  })
  const stopWork = draft.stopWork.filter((item) => item.trigger.trim() || item.immediateResponse.trim()).map((item, index) => {
    if (!item.trigger.trim() || !item.immediateResponse.trim() || !item.escalationRole.trim()) {
      throw new Error(`Stop-work condition ${index + 1}: complete trigger, immediate response, and escalation role.`)
    }
    return {
      ...item.extra,
      sequence: index + 1,
      trigger: item.trigger.trim(),
      immediate_response: item.immediateResponse.trim(),
      escalation_role: item.escalationRole.trim(),
    }
  })
  return {
    ...draft.extra,
    purpose: draft.purpose.trim(),
    scope_statement: draft.scopeStatement.trim(),
    prerequisites,
    steps,
    stop_work_conditions: stopWork,
    source_references: sourceReferencePayload(draft.sourceReferences),
  }
}

function QuantityEditor({
  label,
  draft,
  units,
  onChange,
}: {
  label: string
  draft: QuantityDraft
  units: UnitRow[]
  onChange: (next: QuantityDraft) => void
}) {
  const { t } = useI18n()
  const options = units.filter((unit) => unit.dimension === draft.dimension)
  const update = (patch: Partial<QuantityDraft>) => onChange({ ...draft, ...patch })
  return (
    <div className="space-y-2 rounded border border-graphite-200 p-2 dark:border-graphite-800" data-testid="quantity-editor">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-medium">{label}</span>
        <span className="text-[10px] text-graphite-500">{t('engineeringProducts.dimension')}: {draft.dimension}</span>
      </div>
      <Field label={t('engineeringProducts.valueState')}>
        <select className={CONTROL} value={draft.state} onChange={(event) => update({ state: event.target.value as QuantityState })}>
          <option value="known">{t('engineeringProducts.known')}</option>
          <option value="unknown">{t('engineeringProducts.unknown')}</option>
          <option value="conflicting">{t('engineeringProducts.conflicting')}</option>
        </select>
      </Field>
      {draft.state === 'known' && (
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label={t('engineeringProducts.enteredValue')}>
            <input className={CONTROL} type="number" step="any" value={draft.value} onChange={(event) => update({ value: event.target.value })} />
          </Field>
          <Field label={t('engineeringProducts.registeredUnit')} hint={t('engineeringProducts.noUnitInference')}>
            <select className={CONTROL} value={draft.unit} onChange={(event) => update({ unit: event.target.value })}>
              <option value="">{t('engineeringProducts.selectUnit')}</option>
              {options.map((unit) => <option key={unit.symbol} value={unit.symbol}>{unit.symbol} · {unit.name}</option>)}
            </select>
          </Field>
          <Field label={t('engineeringProducts.sourceKind')}>
            <select className={CONTROL} value={draft.sourceKind} onChange={(event) => update({ sourceKind: event.target.value as SourceKind, sourceRef: event.target.value === 'operator_entered' ? '' : draft.sourceRef })}>
              <option value="operator_entered">{t('engineeringProducts.operatorEntered')}</option>
              <option value="source_document">{t('engineeringProducts.sourceDocument')}</option>
              <option value="engine_run">{t('engineeringProducts.registeredEngineRun')}</option>
            </select>
          </Field>
          {draft.sourceKind !== 'operator_entered' && (
            <Field label={t('engineeringProducts.sourceId')}>
              <input className={CONTROL} value={draft.sourceRef} onChange={(event) => update({ sourceRef: event.target.value })} />
            </Field>
          )}
        </div>
      )}
      {draft.state !== 'known' && (
        <Field label={draft.state === 'unknown' ? t('engineeringProducts.unknownReason') : t('engineeringProducts.conflictReason')}>
          <textarea className={TEXTAREA} value={draft.reason} onChange={(event) => update({ reason: event.target.value })} />
        </Field>
      )}
      {draft.state === 'conflicting' && (
        <Field label={t('engineeringProducts.conflictingObservations')} hint={t('engineeringProducts.observationJsonHint')}>
          <textarea dir="ltr" spellCheck={false} className={`${TEXTAREA} font-mono text-xs`} rows={5} value={draft.observationsText} onChange={(event) => update({ observationsText: event.target.value })} />
        </Field>
      )}
    </div>
  )
}

function SourceReferencesEditor({
  references,
  onChange,
  title,
}: {
  references: SourceReferenceDraft[]
  onChange: (next: SourceReferenceDraft[]) => void
  title: string
}) {
  const { t } = useI18n()
  return (
    <div className="space-y-2 rounded border border-graphite-200 p-2 dark:border-graphite-800">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-medium">{title}</h4>
        <Button size="sm" onClick={() => onChange([...references, { kind: 'document', id: '', extra: {} }])}>{t('engineeringProducts.addReference')}</Button>
      </div>
      {references.map((reference, index) => (
        <div className="grid gap-2 sm:grid-cols-[10rem_1fr_auto]" key={`${index}-${reference.id}`}>
          <select className={CONTROL} value={reference.kind} aria-label={t('engineeringProducts.referenceKind')} onChange={(event) => onChange(references.map((item, i) => i === index ? { ...item, kind: event.target.value as 'document' | 'engine_run' } : item))}>
            <option value="document">{t('engineeringProducts.sourceDocument')}</option>
            <option value="engine_run">{t('engineeringProducts.registeredEngineRun')}</option>
          </select>
          <input className={CONTROL} value={reference.id} aria-label={t('engineeringProducts.sourceId')} onChange={(event) => onChange(references.map((item, i) => i === index ? { ...item, id: event.target.value } : item))} />
          <Button size="sm" variant="ghost" onClick={() => onChange(references.filter((_, i) => i !== index))}>{t('common.remove')}</Button>
        </div>
      ))}
      <p className="text-[11px] text-graphite-500">{t('engineeringProducts.referencesMustBeScoped')}</p>
    </div>
  )
}

function emptyProductDraft(): ProductDraft {
  return draftFromPayload(undefined)
}

function ProductEditor({
  kind,
  mode,
  artifact,
  initialVersion,
  wellbores,
  unitItems,
  approvedProgramVersions,
  onSave,
  saving,
  onCancel,
}: {
  kind: EngineeringProductKind
  mode: 'create' | 'revise'
  artifact?: EngineeringProductArtifact
  initialVersion?: EngineeringProductVersion
  wellbores: Wellbore[]
  unitItems: UnitRow[]
  approvedProgramVersions: Array<{ title: string; artifactId: string; version: EngineeringProductVersion }>
  onSave: (input: ProductSave) => Promise<void>
  saving: boolean
  onCancel: () => void
}) {
  const { t } = useI18n()
  const [title, setTitle] = useState(artifact?.title ?? '')
  const [code, setCode] = useState(artifact?.code ?? '')
  const [wellboreId, setWellboreId] = useState(artifact?.wellbore_id ?? '')
  const productSections = useQuery({
    queryKey: ['sections-for-product-editor', wellboreId],
    queryFn: ({ signal }) => drillingApi.listSections(wellboreId, signal),
    enabled: Boolean(wellboreId),
  })
  const [linkedProgramVersionId, setLinkedProgramVersionId] = useState(
    text(row(initialVersion?.validation_summary.linked_program).version_id),
  )
  const [changeReason, setChangeReason] = useState('')
  const [draft, setDraft] = useState<ProductDraft>(() =>
    initialVersion ? draftFromPayload(initialVersion.payload) : emptyProductDraft(),
  )
  const [formError, setFormError] = useState<string | null>(null)

  function submit(event: FormEvent) {
    event.preventDefault()
    try {
      setFormError(null)
      if (mode === 'create' && !title.trim()) throw new Error(t('engineeringProducts.titleRequired'))
      if (mode === 'create' && !wellboreId) throw new Error(t('engineeringProducts.wellboreRequired'))
      if (mode === 'revise' && !changeReason.trim()) throw new Error(t('engineeringProducts.changeReasonRequired'))
      const payload = buildPayload(kind, draft)
      void onSave({
        title: title.trim(),
        code: code.trim() || undefined,
        wellboreId: mode === 'create' ? wellboreId : undefined,
        payload,
        linkedProgramVersionId: kind === 'procedure' ? linkedProgramVersionId || undefined : undefined,
        changeReason: changeReason.trim(),
      }).catch((error: unknown) => setFormError(error instanceof Error ? error.message : String(error)))
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error))
    }
  }

  const updateObjective = (index: number, patch: Partial<ObjectiveDraft>) =>
    setDraft((value) => ({ ...value, objectives: value.objectives.map((item, i) => i === index ? { ...item, ...patch } : item) }))
  const updateSection = (index: number, patch: Partial<ProgramSectionDraft>) =>
    setDraft((value) => ({ ...value, sections: value.sections.map((item, i) => i === index ? { ...item, ...patch } : item) }))
  const updateBasis = (key: 'designBasis' | 'assumptions', index: number, patch: Partial<BasisDraft>) =>
    setDraft((value) => ({ ...value, [key]: value[key].map((item, i) => i === index ? { ...item, ...patch } : item) }))
  const updateRisk = (index: number, patch: Partial<RiskDraft>) =>
    setDraft((value) => ({ ...value, risks: value.risks.map((item, i) => i === index ? { ...item, ...patch } : item) }))
  const sectionOptions = productSections.data?.items ?? []

  return (
    <form onSubmit={submit} className="space-y-4" data-testid="engineering-product-editor">
      <Card title={mode === 'create' ? t('engineeringProducts.newProduct') : t('engineeringProducts.newRevision')}>
        <div className="grid gap-3 md:grid-cols-2">
          <Field label={t('engineeringProducts.productKind')}>
            <input className={CONTROL} readOnly value={kind === 'program' ? t('engineeringProducts.program') : t('engineeringProducts.procedure')} />
          </Field>
          <Field label={t('engineeringProducts.title')}>
            <input className={CONTROL} data-testid="product-title" value={title} disabled={mode === 'revise'} onChange={(event) => setTitle(event.target.value)} />
          </Field>
          {mode === 'create' && <Field label={t('engineeringProducts.code')}>
            <input className={CONTROL} data-testid="product-code" value={code} onChange={(event) => setCode(event.target.value)} />
          </Field>}
          {mode === 'create' && <Field label={t('engineeringProducts.wellbore')} hint={t('engineeringProducts.wellboreScopeHint')}>
            <select className={CONTROL} data-testid="product-wellbore" value={wellboreId} onChange={(event) => setWellboreId(event.target.value)}>
              <option value="">{t('engineeringProducts.selectWellbore')}</option>
              {wellbores.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
            </select>
          </Field>}
          {mode === 'revise' && <Field label={t('engineeringProducts.changeReason')}>
            <input className={CONTROL} data-testid="product-change-reason" value={changeReason} onChange={(event) => setChangeReason(event.target.value)} />
          </Field>}
          {kind === 'procedure' && <Field label={t('engineeringProducts.linkedProgramRevision')} hint={t('engineeringProducts.programLinkHint')}>
            <select className={CONTROL} value={linkedProgramVersionId} onChange={(event) => setLinkedProgramVersionId(event.target.value)}>
              <option value="">{t('engineeringProducts.noProgramLink')}</option>
              {approvedProgramVersions.map((item) => <option key={item.version.id} value={item.version.id}>{item.title} · v{item.version.version} · {item.version.id}</option>)}
              {linkedProgramVersionId && !approvedProgramVersions.some((item) => item.version.id === linkedProgramVersionId) && (
                <option value={linkedProgramVersionId}>{t('engineeringProducts.existingPinnedRevision')} · {linkedProgramVersionId}</option>
              )}
            </select>
          </Field>}
        </div>
        <Field label={t('engineeringProducts.purpose')}>
          <textarea className={TEXTAREA} data-testid="product-purpose" value={draft.purpose} onChange={(event) => setDraft((value) => ({ ...value, purpose: event.target.value }))} />
        </Field>
      </Card>

      {kind === 'program' ? (
        <>
          <Card title={t('engineeringProducts.objectives')}>
            <div className="space-y-3">
              {draft.objectives.map((objective, index) => (
                <div className="space-y-2 rounded border border-graphite-200 p-3 dark:border-graphite-800" key={index}>
                  <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-medium">{t('engineeringProducts.objectiveNumber', { number: index + 1 })}</h3><Button size="sm" variant="ghost" onClick={() => setDraft((value) => ({ ...value, objectives: value.objectives.filter((_, i) => i !== index) }))}>{t('common.remove')}</Button></div>
                  <Field label={t('engineeringProducts.objectiveStatement')}><textarea className={TEXTAREA} value={objective.statement} onChange={(event) => updateObjective(index, { statement: event.target.value })} /></Field>
                  {objective.targetMd && <QuantityEditor label={t('engineeringProducts.targetMd')} draft={objective.targetMd} units={unitItems} onChange={(targetMd) => updateObjective(index, { targetMd })} />}
                  <Button size="sm" onClick={() => updateObjective(index, { targetMd: objective.targetMd ? null : blankQuantity('length') })}>{objective.targetMd ? t('engineeringProducts.removeOptionalTarget') : t('engineeringProducts.addOptionalTarget')}</Button>
                </div>
              ))}
              <Button size="sm" onClick={() => setDraft((value) => ({ ...value, objectives: [...value.objectives, { statement: '', targetMd: null, extra: {} }] }))}>{t('engineeringProducts.addObjective')}</Button>
            </div>
          </Card>
          <Card title={t('engineeringProducts.sections')}>
            <div className="space-y-4">
              {draft.sections.map((section, index) => (
                <div className="space-y-3 rounded border border-graphite-200 p-3 dark:border-graphite-800" key={index} data-testid="program-section">
                  <div className="flex items-center justify-between"><h3 className="text-sm font-medium">{t('engineeringProducts.sectionNumber', { number: index + 1 })}</h3><Button size="sm" variant="ghost" onClick={() => setDraft((value) => ({ ...value, sections: value.sections.filter((_, i) => i !== index) }))}>{t('common.remove')}</Button></div>
                  <div className="grid gap-2 md:grid-cols-2">
                    <Field label={t('engineeringProducts.sectionName')}><input className={CONTROL} value={section.name} onChange={(event) => updateSection(index, { name: event.target.value })} /></Field>
                    <Field label={t('engineeringProducts.wellSectionIdentity')} hint={t('engineeringProducts.sectionIdentityHint')}>
                      <select className={CONTROL} value={section.sectionId} onChange={(event) => updateSection(index, { sectionId: event.target.value })}>
                        <option value="">{t('engineeringProducts.noMasterSection')}</option>
                        {sectionOptions.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
                      </select>
                    </Field>
                  </div>
                  <div className="grid gap-2 xl:grid-cols-3">
                    <QuantityEditor label={t('engineeringProducts.holeDiameter')} draft={section.holeDiameter} units={unitItems} onChange={(holeDiameter) => updateSection(index, { holeDiameter })} />
                    <QuantityEditor label={t('engineeringProducts.plannedMdFrom')} draft={section.mdFrom} units={unitItems} onChange={(mdFrom) => updateSection(index, { mdFrom })} />
                    <QuantityEditor label={t('engineeringProducts.plannedMdTo')} draft={section.mdTo} units={unitItems} onChange={(mdTo) => updateSection(index, { mdTo })} />
                  </div>
                  {section.mudMin && section.mudMax ? (
                    <div className="grid gap-2 md:grid-cols-2">
                      <QuantityEditor label={t('engineeringProducts.mudDensityMin')} draft={section.mudMin} units={unitItems} onChange={(mudMin) => updateSection(index, { mudMin })} />
                      <QuantityEditor label={t('engineeringProducts.mudDensityMax')} draft={section.mudMax} units={unitItems} onChange={(mudMax) => updateSection(index, { mudMax })} />
                      <Button size="sm" onClick={() => updateSection(index, { mudMin: null, mudMax: null })}>{t('engineeringProducts.removeMudRange')}</Button>
                    </div>
                  ) : <Button size="sm" onClick={() => updateSection(index, { mudMin: blankQuantity('density'), mudMax: blankQuantity('density') })}>{t('engineeringProducts.addMudRange')}</Button>}
                  <div className="space-y-2">
                    <div className="flex items-center justify-between"><h4 className="text-xs font-semibold">{t('engineeringProducts.operatingParameters')}</h4><Button size="sm" onClick={() => updateSection(index, { parameters: [...section.parameters, { key: '', label: '', dimension: 'length', value: blankQuantity('length'), extra: {} }] })}>{t('engineeringProducts.addParameter')}</Button></div>
                    {section.parameters.map((parameter, parameterIndex) => (
                      <div className="space-y-2 rounded border border-graphite-200 p-2 dark:border-graphite-800" key={parameterIndex}>
                        <div className="grid gap-2 md:grid-cols-3">
                          <Field label={t('engineeringProducts.parameterKey')}><input className={CONTROL} value={parameter.key} onChange={(event) => updateSection(index, { parameters: section.parameters.map((item, i) => i === parameterIndex ? { ...item, key: event.target.value } : item) })} /></Field>
                          <Field label={t('engineeringProducts.parameterLabel')}><input className={CONTROL} value={parameter.label} onChange={(event) => updateSection(index, { parameters: section.parameters.map((item, i) => i === parameterIndex ? { ...item, label: event.target.value } : item) })} /></Field>
                          <Field label={t('engineeringProducts.dimension')}>
                            <select className={CONTROL} value={parameter.dimension} onChange={(event) => updateSection(index, { parameters: section.parameters.map((item, i) => i === parameterIndex ? { ...item, dimension: event.target.value, value: blankQuantity(event.target.value) } : item) })}>
                              {unitDimensions(unitItems).map((dimension) => <option key={dimension} value={dimension}>{dimension}</option>)}
                            </select>
                          </Field>
                        </div>
                        <QuantityEditor label={t('engineeringProducts.parameterValue')} draft={parameter.value} units={unitItems} onChange={(value) => updateSection(index, { parameters: section.parameters.map((item, i) => i === parameterIndex ? { ...item, value } : item) })} />
                        <Button size="sm" variant="ghost" onClick={() => updateSection(index, { parameters: section.parameters.filter((_, i) => i !== parameterIndex) })}>{t('common.remove')}</Button>
                      </div>
                    ))}
                  </div>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between"><h4 className="text-xs font-semibold">{t('engineeringProducts.plannedOperations')}</h4><Button size="sm" onClick={() => updateSection(index, { operations: [...section.operations, { activity: '', purpose: '', acceptanceText: '', duration: null, extra: {} }] })}>{t('engineeringProducts.addOperation')}</Button></div>
                    {section.operations.map((operation, operationIndex) => (
                      <div className="space-y-2 rounded border border-graphite-200 p-2 dark:border-graphite-800" key={operationIndex}>
                        <Field label={t('engineeringProducts.activity')}><input className={CONTROL} value={operation.activity} onChange={(event) => updateSection(index, { operations: section.operations.map((item, i) => i === operationIndex ? { ...item, activity: event.target.value } : item) })} /></Field>
                        <Field label={t('engineeringProducts.activityPurpose')}><textarea className={TEXTAREA} value={operation.purpose} onChange={(event) => updateSection(index, { operations: section.operations.map((item, i) => i === operationIndex ? { ...item, purpose: event.target.value } : item) })} /></Field>
                        <Field label={t('engineeringProducts.acceptanceCriteria')} hint={t('engineeringProducts.oneCriterionPerLine')}><textarea className={TEXTAREA} value={operation.acceptanceText} onChange={(event) => updateSection(index, { operations: section.operations.map((item, i) => i === operationIndex ? { ...item, acceptanceText: event.target.value } : item) })} /></Field>
                        <Button size="sm" variant="ghost" onClick={() => updateSection(index, { operations: section.operations.filter((_, i) => i !== operationIndex) })}>{t('common.remove')}</Button>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              <Button size="sm" onClick={() => setDraft((value) => ({ ...value, sections: [...value.sections, emptySection()] }))}>{t('engineeringProducts.addSection')}</Button>
            </div>
          </Card>
          <Card title={t('engineeringProducts.designBasis')}>
            <BasisList items={draft.designBasis} labels={t} onChange={(designBasis) => setDraft((value) => ({ ...value, designBasis }))} onUpdate={(index, patch) => updateBasis('designBasis', index, patch)} addLabel={t('engineeringProducts.addBasis')} />
          </Card>
          <Card title={t('engineeringProducts.risks')}>
            <div className="space-y-3">
              {draft.risks.map((risk, index) => (
                <div className="space-y-2 rounded border border-graphite-200 p-3 dark:border-graphite-800" key={index}>
                  <div className="flex items-center justify-between"><h3 className="text-sm font-medium">{t('engineeringProducts.riskNumber', { number: index + 1 })}</h3><Button size="sm" variant="ghost" onClick={() => setDraft((value) => ({ ...value, risks: value.risks.filter((_, i) => i !== index) }))}>{t('common.remove')}</Button></div>
                  <Field label={t('engineeringProducts.hazard')}><input className={CONTROL} value={risk.hazard} onChange={(event) => updateRisk(index, { hazard: event.target.value })} /></Field>
                  <div className="grid gap-2 md:grid-cols-3">
                    <Field label={t('engineeringProducts.initiatingCauses')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={risk.causesText} onChange={(event) => updateRisk(index, { causesText: event.target.value })} /></Field>
                    <Field label={t('engineeringProducts.consequences')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={risk.consequencesText} onChange={(event) => updateRisk(index, { consequencesText: event.target.value })} /></Field>
                    <Field label={t('engineeringProducts.safeguards')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={risk.safeguardsText} onChange={(event) => updateRisk(index, { safeguardsText: event.target.value })} /></Field>
                  </div>
                  <Field label={t('engineeringProducts.contingency')}><textarea className={TEXTAREA} value={risk.contingency} onChange={(event) => updateRisk(index, { contingency: event.target.value })} /></Field>
                </div>
              ))}
              <Button size="sm" onClick={() => setDraft((value) => ({ ...value, risks: [...value.risks, { hazard: '', causesText: '', consequencesText: '', safeguardsText: '', contingency: '', extra: {} }] }))}>{t('engineeringProducts.addRisk')}</Button>
            </div>
          </Card>
          <Card title={t('engineeringProducts.assumptions')}>
            <BasisList items={draft.assumptions} labels={t} onChange={(assumptions) => setDraft((value) => ({ ...value, assumptions }))} onUpdate={(index, patch) => updateBasis('assumptions', index, patch)} addLabel={t('engineeringProducts.addAssumption')} />
          </Card>
        </>
      ) : (
        <>
          <Card title={t('engineeringProducts.procedureScope')}>
            <Field label={t('engineeringProducts.scopeStatement')} hint={t('engineeringProducts.scopeRequired')}>
              <textarea className={TEXTAREA} value={draft.scopeStatement} onChange={(event) => setDraft((value) => ({ ...value, scopeStatement: event.target.value }))} />
            </Field>
            <div className="space-y-3">
              <h3 className="text-sm font-medium">{t('engineeringProducts.prerequisites')}</h3>
              {draft.prerequisites.map((item, index) => (
                <div className="grid gap-2 rounded border border-graphite-200 p-3 md:grid-cols-2 dark:border-graphite-800" key={index}>
                  <Field label={t('engineeringProducts.prerequisite')}><textarea className={TEXTAREA} value={item.requirement} onChange={(event) => setDraft((value) => ({ ...value, prerequisites: value.prerequisites.map((rowItem, i) => i === index ? { ...rowItem, requirement: event.target.value } : rowItem) }))} /></Field>
                  <Field label={t('engineeringProducts.verification')}><textarea className={TEXTAREA} value={item.verification} onChange={(event) => setDraft((value) => ({ ...value, prerequisites: value.prerequisites.map((rowItem, i) => i === index ? { ...rowItem, verification: event.target.value } : rowItem) }))} /></Field>
                  <Button size="sm" variant="ghost" onClick={() => setDraft((value) => ({ ...value, prerequisites: value.prerequisites.filter((_, i) => i !== index) }))}>{t('common.remove')}</Button>
                </div>
              ))}
              <Button size="sm" onClick={() => setDraft((value) => ({ ...value, prerequisites: [...value.prerequisites, { requirement: '', verification: '', extra: {} }] }))}>{t('engineeringProducts.addPrerequisite')}</Button>
            </div>
          </Card>
          <Card title={t('engineeringProducts.controlledSteps')}>
            <div className="space-y-3">
              {draft.steps.map((item, index) => (
                <div className="space-y-2 rounded border border-graphite-200 p-3 dark:border-graphite-800" key={index} data-testid="procedure-step">
                  <div className="flex items-center justify-between"><h3 className="text-sm font-medium">{t('engineeringProducts.stepNumber', { number: index + 1 })}</h3><Button size="sm" variant="ghost" onClick={() => setDraft((value) => ({ ...value, steps: value.steps.filter((_, i) => i !== index) }))}>{t('common.remove')}</Button></div>
                  <div className="grid gap-2 md:grid-cols-2">
                    <Field label={t('engineeringProducts.stepTitle')}><input className={CONTROL} value={item.title} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, title: event.target.value } : rowItem) }))} /></Field>
                    <Field label={t('engineeringProducts.responsibleRole')}><input className={CONTROL} value={item.responsibleRole} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, responsibleRole: event.target.value } : rowItem) }))} /></Field>
                  </div>
                  <Field label={t('engineeringProducts.instruction')}><textarea className={TEXTAREA} value={item.instruction} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, instruction: event.target.value } : rowItem) }))} /></Field>
                  <div className="grid gap-2 md:grid-cols-2">
                    <Field label={t('engineeringProducts.preconditions')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={item.preconditionsText} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, preconditionsText: event.target.value } : rowItem) }))} /></Field>
                    <Field label={t('engineeringProducts.hazards')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={item.hazardsText} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, hazardsText: event.target.value } : rowItem) }))} /></Field>
                    <Field label={t('engineeringProducts.controlMeasures')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={item.controlsText} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, controlsText: event.target.value } : rowItem) }))} /></Field>
                    <Field label={t('engineeringProducts.verificationCriteria')} hint={t('engineeringProducts.oneItemPerLine')}><textarea className={TEXTAREA} value={item.verificationText} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, verificationText: event.target.value } : rowItem) }))} /></Field>
                  </div>
                  <Field label={t('engineeringProducts.responseIfNotMet')}><textarea className={TEXTAREA} value={item.response} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, response: event.target.value } : rowItem) }))} /></Field>
                  <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={item.holdPoint} onChange={(event) => setDraft((value) => ({ ...value, steps: value.steps.map((rowItem, i) => i === index ? { ...rowItem, holdPoint: event.target.checked } : rowItem) }))} />{t('engineeringProducts.holdPoint')}</label>
                </div>
              ))}
              <Button size="sm" onClick={() => setDraft((value) => ({ ...value, steps: [...value.steps, { title: '', instruction: '', responsibleRole: '', preconditionsText: '', hazardsText: '', controlsText: '', verificationText: '', response: '', holdPoint: false, extra: {} }] }))}>{t('engineeringProducts.addStep')}</Button>
            </div>
          </Card>
          <Card title={t('engineeringProducts.stopWorkConditions')}>
            <div className="space-y-3">
              {draft.stopWork.map((item, index) => (
                <div className="space-y-2 rounded border border-graphite-200 p-3 dark:border-graphite-800" key={index}>
                  <div className="flex items-center justify-between"><h3 className="text-sm font-medium">{t('engineeringProducts.stopWorkNumber', { number: index + 1 })}</h3><Button size="sm" variant="ghost" onClick={() => setDraft((value) => ({ ...value, stopWork: value.stopWork.filter((_, i) => i !== index) }))}>{t('common.remove')}</Button></div>
                  <Field label={t('engineeringProducts.trigger')}><textarea className={TEXTAREA} value={item.trigger} onChange={(event) => setDraft((value) => ({ ...value, stopWork: value.stopWork.map((rowItem, i) => i === index ? { ...rowItem, trigger: event.target.value } : rowItem) }))} /></Field>
                  <Field label={t('engineeringProducts.immediateResponse')}><textarea className={TEXTAREA} value={item.immediateResponse} onChange={(event) => setDraft((value) => ({ ...value, stopWork: value.stopWork.map((rowItem, i) => i === index ? { ...rowItem, immediateResponse: event.target.value } : rowItem) }))} /></Field>
                  <Field label={t('engineeringProducts.escalationRole')}><input className={CONTROL} value={item.escalationRole} onChange={(event) => setDraft((value) => ({ ...value, stopWork: value.stopWork.map((rowItem, i) => i === index ? { ...rowItem, escalationRole: event.target.value } : rowItem) }))} /></Field>
                </div>
              ))}
              <Button size="sm" onClick={() => setDraft((value) => ({ ...value, stopWork: [...value.stopWork, { trigger: '', immediateResponse: '', escalationRole: '', extra: {} }] }))}>{t('engineeringProducts.addStopWorkCondition')}</Button>
            </div>
          </Card>
          <Card title={t('engineeringProducts.sourceLineage')}>
            <SourceReferencesEditor references={draft.sourceReferences} onChange={(sourceReferences) => setDraft((value) => ({ ...value, sourceReferences }))} title={t('engineeringProducts.productSourceReferences')} />
          </Card>
        </>
      )}

      {formError && <p role="alert" className="rounded border border-danger/40 bg-red-50 p-2 text-xs text-danger dark:bg-red-950/20">{formError}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={saving} data-testid="product-save">{saving ? t('common.saving') : mode === 'create' ? t('engineeringProducts.createDraft') : t('engineeringProducts.createRevision')}</Button>
        <Button onClick={onCancel}>{t('common.cancel')}</Button>
      </div>
    </form>
  )
}

function BasisList({
  items,
  labels,
  onChange,
  onUpdate,
  addLabel,
}: {
  items: BasisDraft[]
  labels: (key: string, vars?: Record<string, string | number>) => string
  onChange: (next: BasisDraft[]) => void
  onUpdate: (index: number, patch: Partial<BasisDraft>) => void
  addLabel: string
}) {
  return (
    <div className="space-y-3">
      {items.map((item, index) => (
        <div className="space-y-2 rounded border border-graphite-200 p-3 dark:border-graphite-800" key={index}>
          <div className="flex items-center justify-between"><h3 className="text-sm font-medium">{labels('engineeringProducts.basisNumber', { number: index + 1 })}</h3><Button size="sm" variant="ghost" onClick={() => onChange(items.filter((_, i) => i !== index))}>{labels('common.remove')}</Button></div>
          <div className="grid gap-2 md:grid-cols-2">
            <Field label={labels('engineeringProducts.topic')}><input className={CONTROL} value={item.topic} onChange={(event) => onUpdate(index, { topic: event.target.value })} /></Field>
            <Field label={labels('engineeringProducts.basisState')}>
              <select className={CONTROL} value={item.basis} onChange={(event) => onUpdate(index, { basis: event.target.value as BasisDraft['basis'] })}>
                <option value="reported">{labels('engineeringProducts.reported')}</option>
                <option value="assumption">{labels('engineeringProducts.assumption')}</option>
                <option value="unknown">{labels('engineeringProducts.unknown')}</option>
                <option value="conflicting">{labels('engineeringProducts.conflicting')}</option>
              </select>
            </Field>
          </div>
          <Field label={labels('engineeringProducts.statement')}><textarea className={TEXTAREA} value={item.statement} onChange={(event) => onUpdate(index, { statement: event.target.value })} /></Field>
          {item.basis !== 'reported' && <Field label={labels('engineeringProducts.reason')}><textarea className={TEXTAREA} value={item.reason} onChange={(event) => onUpdate(index, { reason: event.target.value })} /></Field>}
          <SourceReferencesEditor references={item.references} onChange={(references) => onUpdate(index, { references })} title={labels('engineeringProducts.sourceReferences')} />
        </div>
      ))}
      <Button size="sm" onClick={() => onChange([...items, emptyBasis()])}>{addLabel}</Button>
    </div>
  )
}

function unitDimensions(units: UnitRow[]): string[] {
  return [...new Set(units.map((unit) => unit.dimension))].sort()
}

function GroundedContextAssistant({ wellId }: { wellId: string }) {
  const { t } = useI18n()
  const [useLlm, setUseLlm] = useState(false)
  const advisor = useMutation({ mutationFn: () => drillingApi.askAdvisor(wellId, { question: 'engineering_checks', use_llm: useLlm }) })
  const answer: AdvisorAnswer | undefined = advisor.data?.answer
  return (
    <Card title={t('engineeringProducts.groundedAssistantTitle')} subtitle={t('engineeringProducts.groundedAssistantSubtitle')}>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={useLlm} onChange={(event) => setUseLlm(event.target.checked)} />{t('engineeringProducts.optionalNarration')}</label>
        <Button size="sm" onClick={() => advisor.mutate()} disabled={advisor.isPending}>{advisor.isPending ? t('common.loading') : t('engineeringProducts.reviewContext')}</Button>
      </div>
      <p className="mt-2 text-[11px] text-graphite-500">{t('engineeringProducts.groundedAssistantSafety')}</p>
      {advisor.error && <ErrorState error={advisor.error} onRetry={() => advisor.mutate()} />}
      {answer && (
        <div className="mt-3 space-y-3" data-testid="grounded-engineering-context">
          <div className="flex flex-wrap gap-1"><Badge tone="info">{t('engineeringProducts.readOnlyContext')}</Badge><Badge tone="neutral">{answer.sources.length} {t('engineeringProducts.sources')}</Badge></div>
          <section>
            <h4 className="text-xs font-semibold">{t('engineeringProducts.recordedFacts')}</h4>
            {answer.facts.length ? <ul className="list-disc space-y-2 ps-4 text-xs">{answer.facts.map((fact) => (
              <li key={fact.key}>
                {fact.statement} <span className="text-graphite-500">({fact.source_id ?? fact.source})</span>
                {fact.citations.length > 0 && <details className="mt-1"><summary className="cursor-pointer">{t('advisor.citations')} · {fact.citations.length}</summary><Json value={fact.citations} /></details>}
              </li>
            ))}</ul> : <p className="text-xs text-graphite-500">{t('engineeringProducts.noFactsReported')}</p>}
          </section>
          <section><h4 className="text-xs font-semibold">{t('engineeringProducts.registeredEngineResults')}</h4>{answer.calculations.length ? <Json value={answer.calculations} /> : <p className="text-xs text-graphite-500">{t('engineeringProducts.noCalculationEvidence')}</p>}</section>
          <section>
            <h4 className="text-xs font-semibold">{t('advisor.evidence')}</h4>
            {answer.evidence.length ? <div className="space-y-2">{answer.evidence.map((item, index) => <div className="rounded border border-graphite-200 p-2 text-xs dark:border-graphite-800" key={String(item.id ?? item.evidence_id ?? index)}>
              <div className="font-medium">{String(item.document_title ?? item.kind ?? t('advisor.evidence'))}{item.page_number !== null && item.page_number !== undefined ? ` · ${t('common.page')} ${String(item.page_number)}` : ''}</div>
              {typeof item.excerpt === 'string' && <p className="mt-1 whitespace-pre-wrap">{item.excerpt}</p>}
              <Json value={{ evidence_id: item.evidence_id ?? item.id, method: item.method, confidence: item.confidence, quote_verified: item.quote_verified, locator: item.locator }} />
            </div>)}</div> : <p className="text-xs text-graphite-500">{t('engineeringProducts.noEvidenceReported')}</p>}
          </section>
          <section><h4 className="text-xs font-semibold">{t('advisor.inference')}</h4>{answer.inference.length ? <Json value={answer.inference} /> : <p className="text-xs text-graphite-500">{t('engineeringProducts.noInferenceReported')}</p>}</section>
          <section><h4 className="text-xs font-semibold">{t('advisor.recommendation')}</h4>{answer.recommendation.length ? <Json value={answer.recommendation} /> : <p className="text-xs text-graphite-500">{t('engineeringProducts.noRecommendationsReported')}</p>}</section>
          <section><h4 className="text-xs font-semibold">{t('engineeringProducts.unknowns')}</h4>{answer.unknown.length ? <Json value={answer.unknown} /> : <p className="text-xs text-graphite-500">{t('engineeringProducts.noUnknownsReported')}</p>}</section>
          {answer.narrative && <section><h4 className="text-xs font-semibold">{t('engineeringProducts.optionalNarration')} · {answer.narrative.status}</h4><p className="whitespace-pre-wrap text-xs">{answer.narrative.text ?? answer.narrative.reason ?? answer.narrative.note}</p>{answer.narrative.grounding_refs && <Json value={answer.narrative.grounding_refs} />}</section>}
          <p className="text-[11px] text-graphite-500">{answer.limitations.join(' ')}</p>
        </div>
      )}
    </Card>
  )
}

function saveBlob(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(href), 0)
}

function stateTone(state: string): 'ok' | 'warning' | 'danger' | 'neutral' {
  if (state === 'approved' || state === 'passed') return 'ok'
  if (state === 'in_review' || state === 'warnings' || state === 'superseded') return 'warning'
  if (state === 'blocked' || state === 'rejected') return 'danger'
  return 'neutral'
}

export default function ProgramsProceduresWorkspace() {
  const { wellId = '' } = useParams<{ wellId: string }>()
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [kind, setKind] = useState<EngineeringProductKind>('program')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null)
  const [editorMode, setEditorMode] = useState<'create' | 'revise' | null>(null)
  const [reviewNotes, setReviewNotes] = useState('')
  const [decisionNote, setDecisionNote] = useState('')
  const [error, setError] = useState<unknown>(null)
  const products = useQuery({ queryKey: ['engineering-products', wellId], queryFn: ({ signal }) => drillingApi.listEngineeringProducts(wellId, undefined, signal), enabled: Boolean(wellId) })
  const well = useQuery({ queryKey: ['well', wellId], queryFn: ({ signal }) => drillingApi.getWell(wellId, signal), enabled: Boolean(wellId) })
  const wellbores = useQuery({ queryKey: ['wellbores', wellId], queryFn: ({ signal }) => drillingApi.listWellbores(wellId, signal), enabled: Boolean(wellId) })
  const unitCatalogue = useQuery({ queryKey: ['units'], queryFn: ({ signal }) => drillingApi.units(signal) })
  const visibleItems = (products.data?.items ?? []).filter((item) => item.artifact.kind === kind)
  const selectedItem = products.data?.items.find((item) => item.artifact.id === selectedId)
  const activeVersionId = selectedVersionId ?? selectedItem?.current_version?.id ?? null
  const versions = useQuery({ queryKey: ['engineering-product-versions', selectedId], queryFn: ({ signal }) => drillingApi.listEngineeringProductVersions(selectedId!, signal), enabled: Boolean(selectedId) })
  const detail = useQuery({ queryKey: ['engineering-product-version', selectedId, activeVersionId], queryFn: ({ signal }) => drillingApi.getEngineeringProductVersion(selectedId!, activeVersionId!, signal), enabled: Boolean(selectedId && activeVersionId) })
  const approvedProgramVersions = useMemo(() => (products.data?.items ?? []).flatMap((item) => {
    if (item.artifact.kind !== 'program' || item.current_version?.state !== 'approved') return []
    return [{ title: item.artifact.title, artifactId: item.artifact.id, version: item.current_version }]
  }), [products.data])

  const refresh = async (artifactId?: string, versionId?: string) => {
    setError(null)
    await queryClient.invalidateQueries({ queryKey: ['engineering-products', wellId] })
    if (artifactId) {
      await queryClient.invalidateQueries({ queryKey: ['engineering-product-versions', artifactId] })
      if (versionId) await queryClient.invalidateQueries({ queryKey: ['engineering-product-version', artifactId, versionId] })
    }
  }

  const createMutation = useMutation({
    mutationFn: (input: ProductSave) => {
      if (!well.data) throw new Error(t('engineeringProducts.wellNotReady'))
      const body: EngineeringProductCreatePayload = {
        kind,
        title: input.title ?? '',
        code: input.code || null,
        project_id: well.data.project_id,
        wellbore_id: input.wellboreId,
        payload: input.payload,
        linked_program_version_id: input.linkedProgramVersionId ?? null,
      }
      return drillingApi.createEngineeringProduct(wellId, body, globalThis.crypto?.randomUUID?.() ?? `product-${Date.now()}`)
    },
    onSuccess: async (result) => {
      setSelectedId(result.artifact.id)
      setSelectedVersionId(result.version.id)
      setEditorMode(null)
      await refresh(result.artifact.id, result.version.id)
    },
    onError: setError,
  })
  const reviseMutation = useMutation({
    mutationFn: (input: ProductSave) => {
      if (!selectedItem?.current_version) throw new Error(t('engineeringProducts.noCurrentRevision'))
      return drillingApi.reviseEngineeringProduct(selectedItem.artifact.id, {
        expected_current_version: selectedItem.current_version.version,
        change_reason: input.changeReason ?? '',
        payload: input.payload,
        linked_program_version_id: selectedItem.artifact.kind === 'procedure' ? input.linkedProgramVersionId ?? null : null,
      }, globalThis.crypto?.randomUUID?.() ?? `revision-${Date.now()}`)
    },
    onSuccess: async (result) => {
      setSelectedVersionId(result.version.id)
      setEditorMode(null)
      await refresh(result.artifact.id, result.version.id)
    },
    onError: setError,
  })
  const validateMutation = useMutation({
    mutationFn: () => drillingApi.validateEngineeringProduct(selectedId!, activeVersionId!),
    onSuccess: async () => refresh(selectedId ?? undefined, activeVersionId ?? undefined),
    onError: setError,
  })
  const submitMutation = useMutation({
    mutationFn: () => drillingApi.submitEngineeringProduct(selectedId!, activeVersionId!),
    onSuccess: async () => refresh(selectedId ?? undefined, activeVersionId ?? undefined),
    onError: setError,
  })
  const reviewMutation = useMutation({
    mutationFn: () => drillingApi.reviewEngineeringProduct(selectedId!, activeVersionId!, reviewNotes),
    onSuccess: async () => { setReviewNotes(''); await refresh(selectedId ?? undefined, activeVersionId ?? undefined) },
    onError: setError,
  })
  const decisionMutation = useMutation({
    mutationFn: (decision: 'approved' | 'rejected') => {
      const request = detail.data?.approval_request
      if (!request) throw new Error(t('engineeringProducts.noApprovalRequest'))
      return drillingApi.decideEngineeringProduct(selectedId!, activeVersionId!, {
        approval_id: request.id,
        decision,
        note: decisionNote,
      })
    },
    onSuccess: async () => { setDecisionNote(''); await refresh(selectedId ?? undefined, activeVersionId ?? undefined) },
    onError: setError,
  })
  const exportMutation = useMutation({
    mutationFn: async (format: 'json' | 'markdown') => {
      if (!selectedItem || !activeVersionId) throw new Error(t('engineeringProducts.selectVersionFirst'))
      const blob = await drillingApi.exportEngineeringProduct(selectedItem.artifact.id, activeVersionId, format)
      saveBlob(blob, `${selectedItem.artifact.kind}-revision-${detail.data?.version.version ?? 'selected'}.${format === 'json' ? 'json' : 'md'}`)
    },
    onError: setError,
  })

  const selectedArtifact = detail.data?.artifact ?? selectedItem?.artifact
  const selectedVersion = detail.data?.version ?? selectedItem?.current_version ?? undefined
  const currentVersion = selectedItem?.current_version
  const isCurrentVersion = Boolean(currentVersion && selectedVersion && currentVersion.id === selectedVersion.id)
  const validation = selectedVersion?.validation_summary.validation
  const approvalReviewNotes = text(row(detail.data?.approval_request?.request_payload.review).notes)
  const isLoading = products.isLoading || well.isLoading || wellbores.isLoading || unitCatalogue.isLoading

  function selectProduct(id: string) {
    const item = products.data?.items.find((candidate) => candidate.artifact.id === id)
    setSelectedId(id)
    setSelectedVersionId(item?.current_version?.id ?? null)
    setEditorMode(null)
    setError(null)
  }

  function openNew() {
    setSelectedId(null)
    setSelectedVersionId(null)
    setEditorMode('create')
    setError(null)
  }

  function editorSave(input: ProductSave): Promise<void> {
    return (editorMode === 'create' ? createMutation.mutateAsync(input) : reviseMutation.mutateAsync(input)).then(() => undefined)
  }

  const boreOptions = wellbores.data?.items ?? []
  const unitItems = unitCatalogue.data?.items ?? FALLBACK_UNITS
  const selectedKind = editorMode === 'create' ? kind : selectedArtifact?.kind ?? kind
  const editorKey = `${editorMode ?? 'none'}:${selectedId ?? 'new'}:${activeVersionId ?? 'none'}:${selectedKind}`

  return (
    <div className="space-y-4" data-testid="engineering-products-workspace">
      <PageHeader title={t('engineeringProducts.pageTitle')} description={t('engineeringProducts.pageSubtitle')} />
      <p role="note" className="rounded border border-warning/30 bg-amber-50 p-2 text-xs text-graphite-700 dark:bg-amber-950/20 dark:text-graphite-200">{t('engineeringProducts.safetyNotice')}</p>
      <GroundedContextAssistant wellId={wellId} />
      {Boolean(error) && <ErrorState error={error} onRetry={() => setError(null)} />}
      {(products.error || well.error || wellbores.error || unitCatalogue.error) && <ErrorState error={products.error ?? well.error ?? wellbores.error ?? unitCatalogue.error} onRetry={() => { void products.refetch(); void well.refetch(); void wellbores.refetch(); void unitCatalogue.refetch() }} />}
      {isLoading ? <Loading /> : (
        <div className="grid gap-4 xl:grid-cols-[minmax(18rem,0.8fr)_minmax(0,2fr)]">
          <Card title={t('engineeringProducts.productRegister')} subtitle={t('engineeringProducts.productRegisterHint')}>
            <div className="mb-3 flex flex-wrap gap-2">
              <Button size="sm" variant={kind === 'program' ? 'primary' : 'default'} onClick={() => { setKind('program'); setSelectedId(null); setSelectedVersionId(null); setEditorMode(null) }}>{t('engineeringProducts.program')}</Button>
              <Button size="sm" variant={kind === 'procedure' ? 'primary' : 'default'} onClick={() => { setKind('procedure'); setSelectedId(null); setSelectedVersionId(null); setEditorMode(null) }}>{t('engineeringProducts.procedure')}</Button>
              <Button size="sm" onClick={openNew}>{t('engineeringProducts.createNew')}</Button>
            </div>
            <div className="space-y-2" data-testid="engineering-product-list">
              {visibleItems.map((item) => (
                <button key={item.artifact.id} type="button" onClick={() => selectProduct(item.artifact.id)} className={`w-full rounded border p-2 text-start ${selectedId === item.artifact.id ? 'border-signal bg-sky-50/50 dark:bg-sky-950/20' : 'border-graphite-200 dark:border-graphite-800'}`}>
                  <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-sm font-medium">{item.artifact.title}</span><Badge tone={stateTone(item.artifact.status)}>{item.artifact.status}</Badge></div>
                  <div className="mt-1 text-[11px] text-graphite-500">{item.artifact.code ?? t('engineeringProducts.noCode')} · v{item.current_version?.version ?? '—'} · {item.current_version?.state ?? t('engineeringProducts.noRevision')}</div>
                </button>
              ))}
              {visibleItems.length === 0 && <EmptyState message={t('engineeringProducts.noProducts')} hint={t('engineeringProducts.noProductsHint')} />}
            </div>
          </Card>

          <div className="space-y-4">
            {editorMode && (
              <ProductEditor
                key={editorKey}
                kind={selectedKind}
                mode={editorMode}
                artifact={selectedArtifact}
                initialVersion={editorMode === 'revise' ? currentVersion ?? undefined : undefined}
                wellbores={boreOptions}
                unitItems={unitItems}
                approvedProgramVersions={approvedProgramVersions}
                onSave={editorSave}
                saving={createMutation.isPending || reviseMutation.isPending}
                onCancel={() => setEditorMode(null)}
              />
            )}
            {!editorMode && selectedArtifact && selectedVersion && (
              <>
                <Card title={selectedArtifact.title} subtitle={selectedArtifact.code ?? selectedArtifact.id}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={stateTone(selectedVersion.state)}>{selectedVersion.state}</Badge>
                    <Badge tone="neutral">v{selectedVersion.version}</Badge>
                    {validation && <Badge tone={stateTone(validation.state)}>{t('engineeringProducts.validation')}: {validation.state}</Badge>}
                    <span className="break-all text-[10px] text-graphite-500">SHA-256 {selectedVersion.content_hash}</span>
                  </div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    <div><h3 className="text-xs font-semibold">{t('engineeringProducts.scope')}</h3><p className="break-all text-xs">{selectedArtifact.project_id ?? '—'} · {selectedArtifact.well_id ?? '—'} · {selectedArtifact.wellbore_id ?? '—'} · {selectedArtifact.section_id ?? '—'}</p></div>
                    <div><h3 className="text-xs font-semibold">{t('engineeringProducts.revisionLineage')}</h3><p className="text-xs">{t('engineeringProducts.authoredBy')}: {selectedVersion.authored_by ?? '—'} · {selectedVersion.authored_at ?? '—'}</p><p className="text-xs">{t('engineeringProducts.changeReason')}: {selectedVersion.change_reason ?? '—'}</p></div>
                  </div>
                  {detail.data?.linked_program && <div className="mt-3 rounded border border-graphite-200 p-2 text-xs dark:border-graphite-800"><strong>{t('engineeringProducts.linkedProgramRevision')}:</strong> {String(detail.data.linked_program.title ?? '')} · v{String(detail.data.linked_program.version ?? '')} · {String(detail.data.linked_program.version_id ?? '')}<br />{t('engineeringProducts.linkedState')}: {detail.data.linked_program_current_state ?? t('engineeringProducts.unknown')}</div>}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending || !isCurrentVersion || selectedVersion.state !== 'draft'}>{t('engineeringProducts.validateExactRevision')}</Button>
                    <Button size="sm" onClick={() => submitMutation.mutate()} disabled={submitMutation.isPending || !isCurrentVersion || selectedVersion.state !== 'draft' || !validation || validation.state === 'blocked'}>{t('engineeringProducts.submitForReview')}</Button>
                    {isCurrentVersion && !['archived', 'in_review'].includes(selectedVersion.state) && <Button size="sm" onClick={() => setEditorMode('revise')}>{t('engineeringProducts.createRevision')}</Button>}
                    <Button size="sm" onClick={() => exportMutation.mutate('json')} disabled={exportMutation.isPending}>{t('engineeringProducts.exportJson')}</Button>
                    <Button size="sm" onClick={() => exportMutation.mutate('markdown')} disabled={exportMutation.isPending}>{t('engineeringProducts.exportMarkdown')}</Button>
                  </div>
                </Card>
                {selectedVersion.state === 'in_review' && (
                  <Card title={t('engineeringProducts.independentReview')} subtitle={t('engineeringProducts.independentReviewHint')}>
                    {approvalReviewNotes && <div className="mb-2 rounded bg-graphite-50 p-2 text-xs dark:bg-graphite-950">{t('engineeringProducts.reviewRecordedBy')}: {selectedVersion.reviewed_by ?? '—'} · {approvalReviewNotes}</div>}
                    {!selectedVersion.reviewed_by && <div className="space-y-2"><Field label={t('engineeringProducts.reviewNotes')}><textarea className={TEXTAREA} value={reviewNotes} onChange={(event) => setReviewNotes(event.target.value)} /></Field><Button size="sm" onClick={() => reviewMutation.mutate()} disabled={!reviewNotes.trim() || reviewMutation.isPending}>{t('engineeringProducts.recordReview')}</Button></div>}
                    {selectedVersion.reviewed_by && <div className="mt-3 space-y-2"><Field label={t('engineeringProducts.decisionNote')}><textarea className={TEXTAREA} value={decisionNote} onChange={(event) => setDecisionNote(event.target.value)} /></Field><div className="flex gap-2"><Button size="sm" variant="primary" onClick={() => decisionMutation.mutate('approved')} disabled={!decisionNote.trim() || decisionMutation.isPending}>{t('engineeringProducts.approveRevision')}</Button><Button size="sm" variant="danger" onClick={() => decisionMutation.mutate('rejected')} disabled={!decisionNote.trim() || decisionMutation.isPending}>{t('engineeringProducts.rejectRevision')}</Button></div></div>}
                  </Card>
                )}
                {validation && <Card title={t('engineeringProducts.validationFindings')}><ul className="space-y-2 text-xs">{validation.findings.map((finding, index) => <li className="rounded border border-graphite-200 p-2 dark:border-graphite-800" key={`${finding.code}-${index}`}><div className="flex gap-2"><Badge tone={stateTone(finding.severity)}>{finding.severity}</Badge><strong>{finding.code}</strong></div><p>{finding.message}</p>{finding.path && <code className="text-[10px]">{finding.path}</code>}</li>)}</ul><p className="mt-2 text-[11px] text-graphite-500">{t('engineeringProducts.fieldAcceptanceNotEstablished')}</p></Card>}
                <Card title={t('engineeringProducts.revisionHistory')}>
                  {versions.isLoading ? <Loading /> : versions.error ? <ErrorState error={versions.error} onRetry={() => void versions.refetch()} /> : (
                    <div className="flex flex-wrap gap-2">{(versions.data?.items ?? []).map((version) => <Button key={version.id} size="sm" variant={activeVersionId === version.id ? 'primary' : 'default'} onClick={() => setSelectedVersionId(version.id)}>v{version.version} · {version.state}</Button>)}</div>
                  )}
                </Card>
                <Card title={t('engineeringProducts.structuredPayload')}><Json value={selectedVersion.payload} /></Card>
                <Card title={t('engineeringProducts.sourceLineage')}><Json value={selectedVersion.validation_summary.source_lineage ?? []} /></Card>
              </>
            )}
            {!editorMode && !selectedArtifact && <EmptyState message={t('engineeringProducts.selectOrCreate')} hint={t('engineeringProducts.selectOrCreateHint')} />}
            {detail.isLoading && selectedId && <Loading />}
            {detail.error && <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />}
          </div>
        </div>
      )}
    </div>
  )
}
