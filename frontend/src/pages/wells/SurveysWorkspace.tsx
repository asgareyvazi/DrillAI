/** First-class survey review, revision, visualization and export workspace. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import type {
  ExtractionRecord,
  SurveyCreatePayload,
  SurveyImportPayload,
  SurveyInputStationPayload,
  SurveyTrajectoryDetail,
  SurveyTrajectoryKind,
  SurveyTrajectoryStation,
} from '../../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, Field, Loading, Table } from '../../components/common'
import { PageHeader } from '../../components/layout/AppShell'
import { useI18n } from '../../i18n'
import { formatDateTime, formatNumber } from '../../lib/format'

type Datum = SurveyCreatePayload['datum']
type AzimuthReference = SurveyInputStationPayload['azimuth_reference']
type TargetReference = NonNullable<SurveyCreatePayload['target_azimuth_reference']>
type FormMode = 'manual' | 'import' | null

type Translate = (key: string, vars?: Record<string, string | number>) => string

type StationDraft = {
  key: string
  md: string
  mdUnit: 'm' | 'ft'
  inclination: string
  inclinationUnit: 'deg' | 'rad'
  azimuth: string
  azimuthUnit: 'deg' | 'rad'
  azimuthReference: string
  azimuthReferenceId: string
  gridCorrection: string
  gridCorrectionUnit: 'deg' | 'rad'
  toolface: string
  toolfaceUnit: 'deg' | 'rad'
  source: string
  sourceStationId: string
}

type CalculationDraft = {
  kbElevation: string
  kbElevationUnit: 'm' | 'ft'
  targetReference: string
  targetReferenceId: string
  verticalSectionAzimuth: string
  verticalSectionUnit: 'deg' | 'rad'
  verticalSectionReference: string
  verticalSectionReferenceId: string
  maxDls: string
  maxInclination: string
}

const inputClass =
  'w-full rounded border border-graphite-300 bg-white px-2 py-1.5 text-sm dark:border-graphite-700 dark:bg-graphite-950'
const kindOptions: SurveyTrajectoryKind[] = ['actual', 'plan', 'design', 'revised', 'proposed', 'interpolated', 'anti_collision']
const datumOptions: Datum[] = ['unknown', 'rkb', 'msl', 'gl', 'cf', 'derrick_floor']
const referenceOptions: AzimuthReference[] = ['unknown', 'grid', 'true', 'magnetic']
const targetReferenceOptions: TargetReference[] = ['grid', 'true', 'magnetic']

function isAzimuthReference(value: string): value is AzimuthReference {
  return (referenceOptions as readonly string[]).includes(value)
}

function isTargetReference(value: string): value is TargetReference {
  return (targetReferenceOptions as readonly string[]).includes(value)
}

function newKey(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `station-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function idempotencyKey(): string {
  return `ui-survey-${newKey()}`
}

function emptyStation(): StationDraft {
  return {
    key: newKey(),
    md: '',
    mdUnit: 'm',
    inclination: '',
    inclinationUnit: 'deg',
    azimuth: '',
    azimuthUnit: 'deg',
    azimuthReference: 'unknown',
    azimuthReferenceId: '',
    gridCorrection: '',
    gridCorrectionUnit: 'deg',
    toolface: '',
    toolfaceUnit: 'deg',
    source: '',
    sourceStationId: '',
  }
}

function emptyCalculation(): CalculationDraft {
  return {
    kbElevation: '',
    kbElevationUnit: 'm',
    targetReference: '',
    targetReferenceId: '',
    verticalSectionAzimuth: '',
    verticalSectionUnit: 'deg',
    verticalSectionReference: 'unknown',
    verticalSectionReferenceId: '',
    maxDls: '',
    maxInclination: '',
  }
}

function finiteInput(value: string, label: string, t: Translate, optional?: false): number
function finiteInput(value: string, label: string, t: Translate, optional: true): number | undefined
function finiteInput(value: string, label: string, t: Translate, optional = false): number | undefined {
  if (!value.trim()) {
    if (optional) return undefined
    throw new Error(t('surveys.requiredInput', { label }))
  }
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) throw new Error(t('surveys.finiteInput', { label }))
  return parsed
}

function optionalCalculationFields(calc: CalculationDraft, t: Translate): Pick<
  SurveyCreatePayload,
  | 'kb_elevation_value'
  | 'kb_elevation_unit'
  | 'target_azimuth_reference'
  | 'target_azimuth_reference_id'
  | 'vertical_section_azimuth_value'
  | 'vertical_section_azimuth_unit'
  | 'vertical_section_azimuth_reference'
  | 'vertical_section_azimuth_reference_id'
  | 'max_dls_limit'
  | 'max_inclination_limit'
> {
  const result: ReturnType<typeof optionalCalculationFields> = {
    kb_elevation_unit: calc.kbElevationUnit,
    vertical_section_azimuth_unit: calc.verticalSectionUnit,
  }
  const kb = finiteInput(calc.kbElevation, t('surveys.kbElevation'), t, true)
  if (kb !== undefined) result.kb_elevation_value = kb
  if (calc.targetReference) {
    if (!isTargetReference(calc.targetReference)) {
      throw new Error(t('surveys.errorUnknownTargetFrame', { value: calc.targetReference }))
    }
    if (!calc.targetReferenceId.trim()) throw new Error(t('surveys.errorTargetFrame'))
    result.target_azimuth_reference = calc.targetReference
    result.target_azimuth_reference_id = calc.targetReferenceId.trim()
  }
  const vertical = finiteInput(calc.verticalSectionAzimuth, t('surveys.verticalSectionAzimuth'), t, true)
  if (vertical !== undefined) {
    if (!isAzimuthReference(calc.verticalSectionReference)) {
      throw new Error(t('surveys.errorUnknownAzimuthFrame', { value: calc.verticalSectionReference }))
    }
    if (calc.verticalSectionReference === 'unknown' || !calc.verticalSectionReferenceId.trim()) {
      throw new Error(t('surveys.errorVerticalSectionFrame'))
    }
    result.vertical_section_azimuth_value = vertical
    result.vertical_section_azimuth_reference = calc.verticalSectionReference
    result.vertical_section_azimuth_reference_id = calc.verticalSectionReferenceId.trim()
  }
  const dls = finiteInput(calc.maxDls, t('surveys.maximumDlsLimit'), t, true)
  if (dls !== undefined) result.max_dls_limit = dls
  const inclination = finiteInput(calc.maxInclination, t('surveys.maximumInclinationLimit'), t, true)
  if (inclination !== undefined) result.max_inclination_limit = inclination
  return result
}

function recordValue(record: Record<string, unknown>, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(record, key) ? record[key] : undefined
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function numericText(value: unknown, fallback: number | null): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return fallback !== null && Number.isFinite(fallback) ? String(fallback) : ''
}

function stationDraftFromResult(station: SurveyTrajectoryStation): StationDraft {
  const raw = asRecord(station.raw_source)
  const calculation = asRecord(raw.calculation)
  const accepted = asRecord(calculation.accepted_engine_input)
  const acceptedMd = recordValue(accepted, 'md_si')
  const acceptedInclination = recordValue(accepted, 'inclination_deg')
  const acceptedAzimuth = recordValue(accepted, 'azimuth_deg')
  const reference = recordValue(accepted, 'azimuth_reference')
  const referenceId = recordValue(accepted, 'azimuth_reference_id')
  const correction = recordValue(accepted, 'grid_correction_deg')
  const toolface = recordValue(accepted, 'toolface_deg')
  return {
    key: newKey(),
    md: numericText(acceptedMd, station.md_si),
    mdUnit: 'm',
    inclination: numericText(acceptedInclination, station.inclination_deg),
    inclinationUnit: 'deg',
    azimuth: numericText(acceptedAzimuth, station.input_azimuth_deg ?? station.azimuth_deg),
    azimuthUnit: 'deg',
    azimuthReference:
      typeof reference === 'string' ? reference : station.input_azimuth_reference ?? 'unknown',
    azimuthReferenceId:
      typeof referenceId === 'string' ? referenceId : station.input_azimuth_reference_id ?? '',
    gridCorrection: typeof correction === 'number' && Number.isFinite(correction) ? String(correction) : '',
    gridCorrectionUnit: 'deg',
    toolface: typeof toolface === 'number' && Number.isFinite(toolface) ? String(toolface) : '',
    toolfaceUnit: 'deg',
    source: typeof accepted.source === 'string' ? accepted.source : station.source,
    sourceStationId: station.id,
  }
}

function calculationDraftFromTrajectory(trajectory: SurveyTrajectoryDetail): CalculationDraft {
  const validation = asRecord(trajectory.validation)
  const parameters = asRecord(validation.calculation_parameters)
  const targetReference = parameters.target_azimuth_reference
  const verticalReference = parameters.vertical_section_azimuth_reference
  const verticalAzimuth = parameters.vertical_section_azimuth_deg
  const kbElevation = parameters.kb_elevation_si
  return {
    kbElevation: typeof kbElevation === 'number' && Number.isFinite(kbElevation) ? String(kbElevation) : '',
    kbElevationUnit: 'm',
    targetReference: typeof targetReference === 'string' ? targetReference : '',
    targetReferenceId:
      typeof parameters.target_azimuth_reference_id === 'string' ? parameters.target_azimuth_reference_id : '',
    verticalSectionAzimuth:
      typeof verticalAzimuth === 'number' && Number.isFinite(verticalAzimuth) ? String(verticalAzimuth) : '',
    verticalSectionUnit: 'deg',
    verticalSectionReference: typeof verticalReference === 'string' ? verticalReference : 'unknown',
    verticalSectionReferenceId:
      typeof parameters.vertical_section_azimuth_reference_id === 'string'
        ? parameters.vertical_section_azimuth_reference_id
        : '',
    maxDls:
      typeof parameters.max_dls_limit === 'number' && Number.isFinite(parameters.max_dls_limit)
        ? String(parameters.max_dls_limit)
        : '',
    maxInclination:
      typeof parameters.max_inclination_limit === 'number' && Number.isFinite(parameters.max_inclination_limit)
        ? String(parameters.max_inclination_limit)
        : '',
  }
}

function matchBase(
  summaries: Array<{ id: string; name: string; kind: SurveyTrajectoryKind; wellbore_id: string; version: number; is_current: boolean; datum: string }>,
  wellboreId: string,
  name: string,
  kind: SurveyTrajectoryKind,
) {
  return summaries.find(
    (row) => row.is_current && row.wellbore_id === wellboreId && row.name === name.trim() && row.kind === kind,
  )
}

function fmt(value: number | null | undefined, locale: string, digits = 2): string {
  return value !== null && value !== undefined && Number.isFinite(value) ? formatNumber(value, locale, digits) : '—'
}

function extractedNumber(value: unknown, locale: string, t: Translate): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? formatNumber(value, locale, 2)
    : t('common.unknown')
}

function referenceLabel(value: string, t: Translate): string {
  if (value === 'grid') return t('surveys.referenceGrid')
  if (value === 'true') return t('surveys.referenceTrue')
  if (value === 'magnetic') return t('surveys.referenceMagnetic')
  if (value === 'unknown') return t('surveys.referenceUnknown')
  return t('surveys.unknownEnumValue', { value })
}

function kindLabel(value: string, t: Translate): string {
  if (value === 'actual') return t('surveys.kindActual')
  if (value === 'plan') return t('surveys.kindPlan')
  if (value === 'design') return t('surveys.kindDesign')
  if (value === 'revised') return t('surveys.kindRevised')
  if (value === 'proposed') return t('surveys.kindProposed')
  if (value === 'interpolated') return t('surveys.kindInterpolated')
  if (value === 'anti_collision') return t('surveys.kindAntiCollision')
  return t('surveys.unknownEnumValue', { value })
}

function datumLabel(value: string, t: Translate): string {
  if (value === 'rkb') return t('surveys.datumRkb')
  if (value === 'msl') return t('surveys.datumMsl')
  if (value === 'gl') return t('surveys.datumGl')
  if (value === 'cf') return t('surveys.datumCf')
  if (value === 'derrick_floor') return t('surveys.datumDerrick')
  if (value === 'unknown') return t('surveys.datumUnknown')
  return t('surveys.unknownEnumValue', { value })
}

function datumProvenanceLabel(value: string, t: Translate): string {
  if (value === 'source_document') return t('surveys.datumSourceDocument')
  if (value === 'engine_input') return t('surveys.datumEngineInput')
  if (value === 'legacy_unverified') return t('surveys.datumLegacyUnverified')
  if (value === 'unknown') return t('common.unknown')
  return t('surveys.unknownEnumValue', { value })
}

function wellborePurposeLabel(value: string, t: Translate): string {
  if (value === 'original') return t('surveys.wellborePurposeOriginal')
  if (value === 'sidetrack') return t('surveys.wellborePurposeSidetrack')
  if (value === 'bypass') return t('surveys.wellborePurposeBypass')
  if (value === 'reentry') return t('surveys.wellborePurposeReentry')
  if (value === 'reamed') return t('surveys.wellborePurposeReamed')
  if (value === 'pilot') return t('surveys.wellborePurposePilot')
  if (value === 'contingency') return t('surveys.wellborePurposeContingency')
  if (value === 'unknown') return t('surveys.wellborePurposeUnknown')
  return t('surveys.unknownEnumValue', { value })
}

function calculationStateLabel(value: string, t: Translate): string {
  if (value === 'formula_derived') return t('surveys.calculationFormulaDerived')
  if (value === 'legacy_unclassified') return t('surveys.calculationLegacyUnclassified')
  return t('surveys.unknownEnumValue', { value })
}

function engineeringAcceptanceLabel(value: string, t: Translate): string {
  if (value === 'not_established') return t('surveys.acceptanceNotEstablished')
  return t('surveys.unknownEnumValue', { value })
}

function reviewStateLabel(value: string, t: Translate): string {
  if (value === 'human_validated') return t('surveys.reviewStateHumanValidated')
  if (value === 'extracted') return t('surveys.reviewStateExtracted')
  if (value === 'rule_validated') return t('surveys.reviewStateRuleValidated')
  if (value === 'rejected') return t('surveys.reviewStateRejected')
  if (value === 'superseded') return t('surveys.reviewStateSuperseded')
  return t('surveys.unknownEnumValue', { value })
}

function comparisonStateLabel(value: string, t: Translate): string {
  if (value === 'matched_exact_md') return t('surveys.comparisonMatchedExact')
  if (value === 'added') return t('surveys.comparisonAdded')
  if (value === 'removed') return t('surveys.comparisonRemoved')
  return t('surveys.unknownEnumValue', { value })
}

function unitOptionLabel(value: 'm' | 'ft' | 'deg' | 'rad', t: Translate): string {
  if (value === 'm') return t('surveys.unitMetre')
  if (value === 'ft') return t('surveys.unitFoot')
  if (value === 'deg') return t('surveys.unitDegree')
  return t('surveys.unitRadian')
}

function stationSourceLabel(value: string, t: Translate): string {
  if (value === 'manual') return t('surveys.sourceManual')
  if (value === 'plan') return t('surveys.sourcePlan')
  if (value === 'mwd') return t('surveys.sourceMwd')
  if (value === 'gyro') return t('surveys.sourceGyro')
  if (value === 'tie_in') return t('surveys.sourceTieIn')
  if (value === 'interpolated') return t('surveys.sourceInterpolated')
  if (value === 'unknown') return t('surveys.sourceUnknown')
  return value
}

function surveyWarningLabel(value: unknown, t: Translate, locale: string): string {
  if (typeof value !== 'string') return JSON.stringify(value) ?? String(value)
  if (value === 'TVDSS was not calculated because the vertical datum is unknown') {
    return t('surveys.warningTvdssUnknownDatum')
  }
  if (value === 'TVDSS was not calculated because an RKB-referenced TVD requires KB elevation above MSL') {
    return t('surveys.warningTvdssMissingKb')
  }
  if (value === 'trajectory coordinates are referenced to magnetic north; they have not been converted to grid or true north') {
    return t('surveys.warningMagneticNorth')
  }
  const unsupportedDatum = value.match(/^TVDSS was not calculated because no MSL conversion is defined for the (.+) datum$/)
  if (unsupportedDatum) {
    return t('surveys.warningTvdssUnsupportedDatum', { datum: datumLabel(unsupportedDatum[1] ?? 'unknown', t) })
  }
  const gap = value.match(/^station gap of (-?\d+(?:\.\d+)?) m before MD (-?\d+(?:\.\d+)?) m reduces accuracy$/)
  if (gap) {
    return t('surveys.warningStationGap', {
      gap: formatNumber(Number(gap[1]), locale, 1),
      md: formatNumber(Number(gap[2]), locale, 1),
    })
  }
  const highDls = value.match(/^dogleg severity (-?\d+(?:\.\d+)?) deg\/30m at MD (-?\d+(?:\.\d+)?) m exceeds (-?\d+(?:\.\d+)?) deg\/30m$/)
  if (highDls) {
    return t('surveys.warningHighDls', {
      value: formatNumber(Number(highDls[1]), locale, 2),
      md: formatNumber(Number(highDls[2]), locale, 1),
      limit: formatNumber(Number(highDls[3]), locale, 2),
    })
  }
  return value
}

function UnitHeading({ label, unit }: { label: string; unit: string }) {
  return <span dir="auto">{label} (<bdi dir="ltr">{unit}</bdi>)</span>
}

function PlanView({ stations, locale, title, eastLabel, northLabel, emptyLabel, t }: { stations: SurveyTrajectoryStation[]; locale: string; title: string; eastLabel: string; northLabel: string; emptyLabel: string; t: Translate }) {
  const points = stations.filter(
    (station) => Number.isFinite(station.ew_si) && Number.isFinite(station.ns_si),
  )
  if (points.length < 2) return <p className="text-xs text-graphite-500">{emptyLabel}</p>
  const width = 420
  const height = 250
  const padding = 28
  const east = points.map((station) => station.ew_si)
  const north = points.map((station) => station.ns_si)
  const minEast = Math.min(...east)
  const maxEast = Math.max(...east)
  const minNorth = Math.min(...north)
  const maxNorth = Math.max(...north)
  const spanEast = Math.max(maxEast - minEast, 1)
  const spanNorth = Math.max(maxNorth - minNorth, 1)
  const availableWidth = width - padding * 2
  const availableHeight = height - padding * 2
  const scale = Math.min(availableWidth / spanEast, availableHeight / spanNorth)
  const plotWidth = spanEast * scale
  const plotHeight = spanNorth * scale
  const offsetX = padding + (availableWidth - plotWidth) / 2
  const offsetY = padding + (availableHeight - plotHeight) / 2
  const xy = points.map((station) => ({
    x: offsetX + (station.ew_si - minEast) * scale,
    y: offsetY + (maxNorth - station.ns_si) * scale,
    station,
  }))
  return (
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} className="h-60 w-full rounded border border-graphite-200 bg-white dark:border-graphite-800 dark:bg-graphite-950">
      <line x1={padding} x2={width - padding} y1={height - padding} y2={height - padding} stroke="currentColor" opacity="0.25" />
      <line x1={padding} x2={padding} y1={padding} y2={height - padding} stroke="currentColor" opacity="0.25" />
      <polyline points={xy.map((point) => `${point.x},${point.y}`).join(' ')} fill="none" stroke="currentColor" strokeWidth="2" className="text-signal" />
      {xy.map(({ x, y, station }, index) => (
        <circle key={station.id} cx={x} cy={y} r="4" fill="currentColor" className={index === 0 ? 'text-warning' : 'text-signal-deep'}>
          <title>{t('surveys.stationTooltip', { md: fmt(station.md_si, locale), east: fmt(station.ew_si, locale), north: fmt(station.ns_si, locale) })}</title>
        </circle>
      ))}
      <text x={width / 2} y={height - 6} textAnchor="middle" className="fill-current text-[10px]">{eastLabel} (<tspan direction="ltr" unicodeBidi="isolate">m</tspan>)</text>
      <text x="10" y={height / 2} textAnchor="middle" transform={`rotate(-90 10 ${height / 2})`} className="fill-current text-[10px]">{northLabel} (<tspan direction="ltr" unicodeBidi="isolate">m</tspan>)</text>
    </svg>
  )
}

function DepthView({ stations, locale, title, mdLabel, tvdLabel, emptyLabel, positiveDownLabel, t }: { stations: SurveyTrajectoryStation[]; locale: string; title: string; mdLabel: string; tvdLabel: string; emptyLabel: string; positiveDownLabel: string; t: Translate }) {
  const points = stations.filter(
    (station) => Number.isFinite(station.md_si) && Number.isFinite(station.tvd_si),
  )
  if (points.length < 2) return <p className="text-xs text-graphite-500">{emptyLabel}</p>
  const width = 420
  const height = 250
  const padding = 28
  const md = points.map((station) => station.md_si)
  const tvd = points.map((station) => station.tvd_si)
  const minMd = Math.min(...md)
  const maxMd = Math.max(...md)
  const minTvd = Math.min(...tvd)
  const maxTvd = Math.max(...tvd)
  const spanMd = Math.max(maxMd - minMd, 1)
  const spanTvd = Math.max(maxTvd - minTvd, 1)
  const xy = points.map((station) => ({
    x: padding + ((station.md_si - minMd) / spanMd) * (width - padding * 2),
    y: padding + ((station.tvd_si - minTvd) / spanTvd) * (height - padding * 2),
    station,
  }))
  return (
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} className="h-60 w-full rounded border border-graphite-200 bg-white dark:border-graphite-800 dark:bg-graphite-950">
      <line x1={padding} x2={width - padding} y1={padding} y2={padding} stroke="currentColor" opacity="0.25" />
      <line x1={padding} x2={padding} y1={padding} y2={height - padding} stroke="currentColor" opacity="0.25" />
      <polyline points={xy.map((point) => `${point.x},${point.y}`).join(' ')} fill="none" stroke="currentColor" strokeWidth="2" className="text-signal" />
      {xy.map(({ x, y, station }) => (
        <circle key={station.id} cx={x} cy={y} r="4" fill="currentColor" className="text-signal-deep">
          <title>{t('surveys.depthTooltip', { md: fmt(station.md_si, locale), tvd: fmt(station.tvd_si, locale) })}</title>
        </circle>
      ))}
      <text x={width / 2} y={height - 6} textAnchor="middle" className="fill-current text-[10px]">{mdLabel} (<tspan direction="ltr" unicodeBidi="isolate">m</tspan>)</text>
      <text x="10" y={height / 2} textAnchor="middle" transform={`rotate(-90 10 ${height / 2})`} className="fill-current text-[10px]">{tvdLabel} (<tspan direction="ltr" unicodeBidi="isolate">m</tspan>, {positiveDownLabel})</text>
    </svg>
  )
}

function unitReviewNeeded(record: ExtractionRecord): boolean {
  return record.unit_inference_review_required ?? record.quality_flags.includes('unit_inferred')
}

function hasCurrentUnitReview(record: ExtractionRecord): boolean {
  return !unitReviewNeeded(record) || record.unit_inference_reviewed === true
}

function Eligibility({ record, t }: { record: ExtractionRecord; t: Translate }) {
  const reviewStateEligible =
    record.validation_state === 'human_validated' &&
    Boolean(record.validated_by) &&
    Boolean(record.validated_at) &&
    record.review_version >= 1
  const unitReviewEligible = hasCurrentUnitReview(record)
  const eligible = reviewStateEligible && unitReviewEligible
  const label = !unitReviewEligible ? t('surveys.unitReviewRequired') : reviewStateLabel(record.validation_state, t)
  return <Badge tone={eligible ? 'ok' : 'warning'}>{label}</Badge>
}

export default function SurveysWorkspace() {
  const { t, locale } = useI18n()
  const { wellId } = useParams<{ wellId: string }>()
  const id = wellId as string
  const queryClient = useQueryClient()
  const [wellboreId, setWellboreId] = useState('')
  const [selectedTrajectoryId, setSelectedTrajectoryId] = useState('')
  const [mode, setMode] = useState<FormMode>(null)
  const [formBase, setFormBase] = useState<{ id: string; version: number; name: string; kind: SurveyTrajectoryKind; datum: Datum } | null>(null)
  const [formName, setFormName] = useState('')
  const [formKind, setFormKind] = useState<SurveyTrajectoryKind>('actual')
  const [datum, setDatum] = useState<Datum>('unknown')
  const [reason, setReason] = useState('')
  const [stations, setStations] = useState<StationDraft[]>([emptyStation(), emptyStation()])
  const [calculation, setCalculation] = useState<CalculationDraft>(emptyCalculation())
  const [sourceDocumentId, setSourceDocumentId] = useState('')
  const [selectedRecordIds, setSelectedRecordIds] = useState<string[]>([])
  const [formError, setFormError] = useState<string | null>(null)
  const [savedMessage, setSavedMessage] = useState<string | null>(null)
  const [baselineId, setBaselineId] = useState('')
  const [comparePair, setComparePair] = useState<{ current: string; baseline: string } | null>(null)

  const wellQuery = useQuery({ queryKey: ['well', id], queryFn: ({ signal }) => drillingApi.getWell(id, signal), enabled: Boolean(id) })
  const surveyQuery = useQuery({ queryKey: ['surveys', id], queryFn: ({ signal }) => drillingApi.listSurveys(id, signal), enabled: Boolean(id) })
  const summaries = useMemo(() => surveyQuery.data?.items ?? [], [surveyQuery.data?.items])
  const wellbores = useMemo(() => wellQuery.data?.wellbores ?? [], [wellQuery.data?.wellbores])
  const boreSurveys = useMemo(() => summaries.filter((item) => item.wellbore_id === wellboreId), [summaries, wellboreId])
  const detailQuery = useQuery({
    queryKey: ['survey', id, selectedTrajectoryId],
    queryFn: ({ signal }) => drillingApi.getSurvey(id, selectedTrajectoryId, signal),
    enabled: Boolean(id && selectedTrajectoryId),
  })
  const documentList = useQuery({
    queryKey: ['survey-import-documents', id],
    queryFn: ({ signal }) =>
      drillingApi.listDocuments(
        { well_id: id, doc_types: ['ddr', 'drilling_document', 'program', 'survey_report', 'other'], limit: 500 },
        signal,
      ),
    enabled: Boolean(id && mode === 'import'),
  })
  const documents = (documentList.data?.items ?? []).filter((document) => document.wellbore_id === wellboreId)
  const documentQuery = useQuery({
    queryKey: ['document', sourceDocumentId],
    queryFn: ({ signal }) => drillingApi.getDocument(sourceDocumentId, signal),
    enabled: Boolean(sourceDocumentId && mode === 'import'),
  })
  const sourceRecords = (documentQuery.data?.records ?? []).filter((record) => record.record_type === 'survey_station')
  const eligibleRecords = sourceRecords.filter(
    (record) =>
      record.validation_state === 'human_validated' &&
      Boolean(record.validated_by) &&
      Boolean(record.validated_at) &&
      record.review_version >= 1 &&
      hasCurrentUnitReview(record),
  )
  const eligibleRecordIds = new Set(eligibleRecords.map((record) => record.id))
  const selectedEligibleRecordIds = selectedRecordIds.filter((recordId) => eligibleRecordIds.has(recordId))
  const currentImportBase = matchBase(summaries, wellboreId, formName, formKind)

  useEffect(() => {
    if (mode === 'import' && currentImportBase && currentImportBase.datum !== datum) {
      setDatum(currentImportBase.datum as Datum)
    }
  }, [mode, currentImportBase, datum])

  useEffect(() => {
    if (!wellboreId && wellbores.length === 1) setWellboreId(wellbores[0]?.id ?? '')
  }, [wellboreId, wellbores])

  useEffect(() => {
    if (!wellboreId) {
      setSelectedTrajectoryId('')
      return
    }
    if (!selectedTrajectoryId) {
      setSelectedTrajectoryId(boreSurveys.find((item) => item.is_current)?.id ?? boreSurveys[0]?.id ?? '')
      return
    }
    if (boreSurveys.some((item) => item.id === selectedTrajectoryId)) return
    // Keep a newly-created ID while the invalidated list is still returning the old cache value.
    // The list result will confirm it; discarding it here would strand the UI on the prior revision.
    if (surveyQuery.isPending || surveyQuery.isFetching) return
    setSelectedTrajectoryId(boreSurveys.find((item) => item.is_current)?.id ?? boreSurveys[0]?.id ?? '')
  }, [wellboreId, boreSurveys, selectedTrajectoryId, surveyQuery.isFetching, surveyQuery.isPending])

  const createMutation = useMutation({
    mutationFn: ({ payload, key }: { payload: SurveyCreatePayload; key: string }) => drillingApi.createSurvey(id, payload, key),
    onSuccess: async (result) => {
      setSavedMessage(t('surveys.revisionCreated'))
      setSelectedTrajectoryId(result.trajectory.id)
      setMode(null)
      setFormError(null)
      await queryClient.invalidateQueries({ queryKey: ['surveys', id] })
      await queryClient.invalidateQueries({ queryKey: ['survey', id] })
    },
  })
  const importMutation = useMutation({
    mutationFn: ({ payload, key }: { payload: SurveyImportPayload; key: string }) => drillingApi.importSurvey(id, payload, key),
    onSuccess: async (result) => {
      setSavedMessage(t('surveys.revisionCreated'))
      setSelectedTrajectoryId(result.trajectory.id)
      setMode(null)
      setFormError(null)
      await queryClient.invalidateQueries({ queryKey: ['surveys', id] })
      await queryClient.invalidateQueries({ queryKey: ['survey', id] })
    },
  })
  const exportMutation = useMutation({
    mutationFn: (trajectoryId: string) => drillingApi.exportSurveyCsv(id, trajectoryId),
    onSuccess: (blob, trajectoryId) => {
      const url = URL.createObjectURL(blob)
      const anchor = window.document.createElement('a')
      anchor.href = url
      anchor.download = `trajectory-${trajectoryId}.csv`
      anchor.rel = 'noopener'
      anchor.click()
      URL.revokeObjectURL(url)
    },
  })
  const compareQuery = useQuery({
    queryKey: ['survey-compare', id, comparePair?.current, comparePair?.baseline],
    queryFn: ({ signal }) => drillingApi.compareSurveys(id, comparePair!.current, comparePair!.baseline, signal),
    enabled: Boolean(comparePair?.current && comparePair?.baseline),
  })

  function startManual(base?: { id: string; version: number; name: string; kind: SurveyTrajectoryKind; datum: string }) {
    setSavedMessage(null)
    setFormError(null)
    setMode('manual')
    setFormBase(base ? { ...base, datum: base.datum as Datum } : null)
    setFormName(base?.name ?? '')
    setFormKind(base?.kind ?? 'actual')
    setDatum((base?.datum as Datum) ?? 'unknown')
    setReason('')
    setCalculation(base && detailQuery.data?.trajectory.id === base.id ? calculationDraftFromTrajectory(detailQuery.data.trajectory) : emptyCalculation())
    setStations(
      base && detailQuery.data?.trajectory.id === base.id
        ? detailQuery.data.stations.map(stationDraftFromResult)
        : [emptyStation(), emptyStation()],
    )
    setComparePair(null)
  }

  function startImport() {
    setSavedMessage(null)
    setFormError(null)
    setMode('import')
    setFormBase(null)
    setFormName('')
    setFormKind('actual')
    setDatum('unknown')
    setReason('')
    setCalculation(emptyCalculation())
    setSourceDocumentId('')
    setSelectedRecordIds([])
    setComparePair(null)
  }

  function updateStation(index: number, patch: Partial<StationDraft>) {
    setStations((current) => current.map((station, row) => (row === index ? { ...station, ...patch } : station)))
  }

  function numberOrUndefined(value: string, label: string): number | undefined {
    return finiteInput(value, label, t, true)
  }

  function buildStationInputs(): SurveyInputStationPayload[] {
    if (stations.length < 2) throw new Error(t('surveys.errorStationCount'))
    return stations.map((station, index) => {
      const stationNumber = formatNumber(index + 1, locale, 0)
      const stationLabel = (field: string) => t('surveys.stationField', { station: stationNumber, field })
      const md = finiteInput(station.md, stationLabel(t('surveys.md')), t)
      const inclination = finiteInput(station.inclination, stationLabel(t('surveys.inclination')), t)
      const azimuth = finiteInput(station.azimuth, stationLabel(t('surveys.inputAzimuth')), t)
      if (!isAzimuthReference(station.azimuthReference)) {
        throw new Error(t('surveys.errorUnknownAzimuthFrame', { value: station.azimuthReference }))
      }
      if (station.azimuthReference !== 'unknown' && !station.azimuthReferenceId.trim()) {
        throw new Error(t('surveys.errorAzimuthFrame', { station: stationNumber }))
      }
      return {
        md_value: md,
        md_unit: station.mdUnit,
        inclination_value: inclination,
        inclination_unit: station.inclinationUnit,
        azimuth_value: azimuth,
        azimuth_unit: station.azimuthUnit,
        azimuth_reference: station.azimuthReference,
        azimuth_reference_id: station.azimuthReferenceId.trim() || null,
        grid_correction_value: numberOrUndefined(station.gridCorrection, stationLabel(t('surveys.gridCorrection'))) ?? null,
        grid_correction_unit: station.gridCorrectionUnit,
        toolface_value: numberOrUndefined(station.toolface, stationLabel(t('surveys.toolface'))) ?? null,
        toolface_unit: station.toolfaceUnit,
        source: station.source.trim() || 'manual',
        ...(formBase && station.sourceStationId ? { source_station_id: station.sourceStationId } : {}),
      }
    })
  }

  function formHeaders() {
    const common = {
      wellbore_id: wellboreId,
      name: formName.trim(),
      kind: formKind,
      reason: reason.trim(),
      datum,
      ...optionalCalculationFields(calculation, t),
    }
    return common
  }

  function submitManual(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)
    try {
      if (!wellboreId) throw new Error(t('surveys.errorSelectWellbore'))
      if (!formName.trim()) throw new Error(t('surveys.errorSeriesName'))
      if (!reason.trim()) throw new Error(t('surveys.errorRevisionReason'))
      if (!formBase && matchBase(summaries, wellboreId, formName, formKind)) {
        throw new Error(t('surveys.errorDuplicateSeries'))
      }
      if (formBase && (formBase.name !== formName.trim() || formBase.kind !== formKind)) {
        throw new Error(t('surveys.errorSeriesIdentity'))
      }
      if (formBase && formBase.datum !== datum) throw new Error(t('surveys.errorDatumChange'))
      const payload: SurveyCreatePayload = {
        ...formHeaders(),
        expected_current_version: formBase?.version ?? 0,
        base_trajectory_id: formBase?.id ?? null,
        stations: buildStationInputs(),
      }
      createMutation.mutate({ payload, key: idempotencyKey() })
    } catch (error) {
      setFormError(error instanceof Error ? error.message : t('surveys.errorInvalidForm'))
    }
  }

  function submitImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)
    try {
      if (!wellboreId) throw new Error(t('surveys.errorSelectWellbore'))
      if (!sourceDocumentId) throw new Error(t('surveys.errorSourceDocument'))
      if (selectedEligibleRecordIds.length < 2) throw new Error(t('surveys.errorReviewedRecords'))
      if (!formName.trim()) throw new Error(t('surveys.errorSeriesName'))
      if (!reason.trim()) throw new Error(t('surveys.errorRevisionReason'))
      const base = currentImportBase
      if (base && base.datum !== datum) throw new Error(t('surveys.errorImportDatum'))
      const payload: SurveyImportPayload = {
        ...formHeaders(),
        source_document_id: sourceDocumentId,
        selected_record_ids: selectedEligibleRecordIds,
        expected_current_version: base?.version ?? 0,
        base_trajectory_id: base?.id ?? null,
      }
      importMutation.mutate({ payload, key: idempotencyKey() })
    } catch (error) {
      setFormError(error instanceof Error ? error.message : t('surveys.errorInvalidForm'))
    }
  }

  function toggleRecord(recordId: string) {
    setSelectedRecordIds((current) =>
      current.includes(recordId) ? current.filter((id) => id !== recordId) : [...current, recordId],
    )
  }

  function renderCalculationFields(datumLocked: boolean) {
    return (
      <div className="grid gap-3 md:grid-cols-2">
        <Field label={t('surveys.datum')}>
          <select value={datum} disabled={datumLocked} onChange={(event) => setDatum(event.target.value as Datum)} className={inputClass}>
            {!(datumOptions as readonly string[]).includes(datum) && <option value={datum} dir="auto">{datumLabel(datum, t)}</option>}
            {datumOptions.map((value) => (
              <option key={value} value={value}>
                {datumLabel(value, t)}
              </option>
            ))}
          </select>
        </Field>
        <div className="grid grid-cols-[1fr_5rem] gap-2">
          <Field label={t('surveys.kbElevation')}>
            <input type="number" step="any" value={calculation.kbElevation} onChange={(event) => setCalculation((old) => ({ ...old, kbElevation: event.target.value }))} className={inputClass} />
          </Field>
          <Field label={t('surveys.unit')}>
            <select value={calculation.kbElevationUnit} onChange={(event) => setCalculation((old) => ({ ...old, kbElevationUnit: event.target.value as 'm' | 'ft' }))} className={inputClass}>
              <option value="m">{unitOptionLabel('m', t)}</option><option value="ft">{unitOptionLabel('ft', t)}</option>
            </select>
          </Field>
        </div>
        <Field label={t('surveys.targetFrame')}>
          <select value={calculation.targetReference} onChange={(event) => setCalculation((old) => ({ ...old, targetReference: event.target.value }))} className={inputClass}>
            <option value="">{t('surveys.noTargetConversion')}</option>
            {calculation.targetReference && !isTargetReference(calculation.targetReference) && (
              <option value={calculation.targetReference} dir="auto">{t('surveys.unknownEnumValue', { value: calculation.targetReference })}</option>
            )}
            <option value="grid">{t('surveys.referenceGrid')}</option>
            <option value="true">{t('surveys.referenceTrue')}</option>
            <option value="magnetic">{t('surveys.referenceMagnetic')}</option>
          </select>
        </Field>
        {calculation.targetReference && (
          <Field label={t('surveys.frameIdentity')}>
            <input value={calculation.targetReferenceId} onChange={(event) => setCalculation((old) => ({ ...old, targetReferenceId: event.target.value }))} className={inputClass} />
          </Field>
        )}
        <div className="grid grid-cols-[1fr_5rem] gap-2">
          <Field label={t('surveys.verticalSectionAzimuth')}>
            <input type="number" step="any" value={calculation.verticalSectionAzimuth} onChange={(event) => setCalculation((old) => ({ ...old, verticalSectionAzimuth: event.target.value }))} className={inputClass} />
          </Field>
          <Field label={t('surveys.angleUnit')}>
            <select value={calculation.verticalSectionUnit} onChange={(event) => setCalculation((old) => ({ ...old, verticalSectionUnit: event.target.value as 'deg' | 'rad' }))} className={inputClass}>
              <option value="deg">{unitOptionLabel('deg', t)}</option><option value="rad">{unitOptionLabel('rad', t)}</option>
            </select>
          </Field>
        </div>
        {calculation.verticalSectionAzimuth.trim() && (
          <>
            <Field label={t('surveys.azimuthFrame')}>
              <select value={calculation.verticalSectionReference} onChange={(event) => setCalculation((old) => ({ ...old, verticalSectionReference: event.target.value }))} className={inputClass}>
                {!isAzimuthReference(calculation.verticalSectionReference) && (
                  <option value={calculation.verticalSectionReference} dir="auto">{referenceLabel(calculation.verticalSectionReference, t)}</option>
                )}
                {referenceOptions.map((value) => <option key={value} value={value}>{referenceLabel(value, t)}</option>)}
              </select>
            </Field>
            <Field label={t('surveys.frameIdentity')}>
              <input value={calculation.verticalSectionReferenceId} onChange={(event) => setCalculation((old) => ({ ...old, verticalSectionReferenceId: event.target.value }))} className={inputClass} />
            </Field>
          </>
        )}
        <Field label={`${t('surveys.maximumDlsLimit')} · ${t('surveys.dlsLimitUnit')}`}>
          <input type="number" step="any" min="0" value={calculation.maxDls} onChange={(event) => setCalculation((old) => ({ ...old, maxDls: event.target.value }))} className={inputClass} />
        </Field>
        <Field label={<UnitHeading label={t('surveys.maximumInclinationLimit')} unit="deg" />}>
          <input type="number" step="any" min="0" max="180" value={calculation.maxInclination} onChange={(event) => setCalculation((old) => ({ ...old, maxInclination: event.target.value }))} className={inputClass} />
        </Field>
      </div>
    )
  }

  function renderStations() {
    return (
      <div className="space-y-2">
        {stations.map((station, index) => {
          const stationNumber = formatNumber(index + 1, locale, 0)
          return (
          <fieldset key={station.key} className="rounded-md border border-graphite-200 p-3 dark:border-graphite-800">
            <legend className="px-1 text-xs font-semibold">{t('surveys.station')} {stationNumber}</legend>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <div className="grid grid-cols-[1fr_5rem] gap-1">
                <Field label={`${t('surveys.md')} (${stationNumber})`}>
                  <input aria-label={`${t('surveys.md')} ${stationNumber}`} type="number" step="any" min="0" value={station.md} onChange={(event) => updateStation(index, { md: event.target.value })} className={inputClass} />
                </Field>
                <Field label={t('surveys.unit')}>
                  <select value={station.mdUnit} onChange={(event) => updateStation(index, { mdUnit: event.target.value as 'm' | 'ft' })} className={inputClass}>
                    <option value="m">{unitOptionLabel('m', t)}</option><option value="ft">{unitOptionLabel('ft', t)}</option>
                  </select>
                </Field>
              </div>
              <div className="grid grid-cols-[1fr_5rem] gap-1">
                <Field label={`${t('surveys.inclination')} (${stationNumber})`}>
                  <input aria-label={`${t('surveys.inclination')} ${stationNumber}`} type="number" step="any" value={station.inclination} onChange={(event) => updateStation(index, { inclination: event.target.value })} className={inputClass} />
                </Field>
                <Field label={t('surveys.angleUnit')}>
                  <select value={station.inclinationUnit} onChange={(event) => updateStation(index, { inclinationUnit: event.target.value as 'deg' | 'rad' })} className={inputClass}>
                    <option value="deg">{unitOptionLabel('deg', t)}</option><option value="rad">{unitOptionLabel('rad', t)}</option>
                  </select>
                </Field>
              </div>
              <div className="grid grid-cols-[1fr_5rem] gap-1">
                <Field label={`${t('surveys.inputAzimuth')} (${stationNumber})`}>
                  <input aria-label={`${t('surveys.inputAzimuth')} ${stationNumber}`} type="number" step="any" value={station.azimuth} onChange={(event) => updateStation(index, { azimuth: event.target.value })} className={inputClass} />
                </Field>
                <Field label={t('surveys.angleUnit')}>
                  <select value={station.azimuthUnit} onChange={(event) => updateStation(index, { azimuthUnit: event.target.value as 'deg' | 'rad' })} className={inputClass}>
                    <option value="deg">{unitOptionLabel('deg', t)}</option><option value="rad">{unitOptionLabel('rad', t)}</option>
                  </select>
                </Field>
              </div>
              <Field label={t('surveys.azimuthFrame')}>
                <select value={station.azimuthReference} onChange={(event) => updateStation(index, { azimuthReference: event.target.value })} className={inputClass}>
                  {!isAzimuthReference(station.azimuthReference) && (
                    <option value={station.azimuthReference} dir="auto">{referenceLabel(station.azimuthReference, t)}</option>
                  )}
                  {referenceOptions.map((value) => <option key={value} value={value}>{referenceLabel(value, t)}</option>)}
                </select>
              </Field>
              {station.azimuthReference !== 'unknown' && (
                <Field label={t('surveys.frameIdentity')}>
                  <input value={station.azimuthReferenceId} onChange={(event) => updateStation(index, { azimuthReferenceId: event.target.value })} className={inputClass} />
                </Field>
              )}
              <div className="grid grid-cols-[1fr_5rem] gap-1">
                <Field label={t('surveys.gridCorrection')}>
                  <input type="number" step="any" value={station.gridCorrection} onChange={(event) => updateStation(index, { gridCorrection: event.target.value })} className={inputClass} />
                </Field>
                <Field label={t('surveys.angleUnit')}>
                  <select value={station.gridCorrectionUnit} onChange={(event) => updateStation(index, { gridCorrectionUnit: event.target.value as 'deg' | 'rad' })} className={inputClass}>
                    <option value="deg">{unitOptionLabel('deg', t)}</option><option value="rad">{unitOptionLabel('rad', t)}</option>
                  </select>
                </Field>
              </div>
              <div className="grid grid-cols-[1fr_5rem] gap-1">
                <Field label={t('surveys.toolface')}>
                  <input type="number" step="any" value={station.toolface} onChange={(event) => updateStation(index, { toolface: event.target.value })} className={inputClass} />
                </Field>
                <Field label={t('surveys.angleUnit')}>
                  <select value={station.toolfaceUnit} onChange={(event) => updateStation(index, { toolfaceUnit: event.target.value as 'deg' | 'rad' })} className={inputClass}>
                    <option value="deg">{unitOptionLabel('deg', t)}</option><option value="rad">{unitOptionLabel('rad', t)}</option>
                  </select>
                </Field>
              </div>
              <Field label={t('surveys.stationSource')}>
                <input
                  value={station.source === 'manual' || station.source === 'unknown' ? '' : station.source}
                  placeholder={station.source === 'unknown' ? t('surveys.sourceUnknown') : t('surveys.sourceManual')}
                  onChange={(event) => updateStation(index, { source: event.target.value })}
                  className={inputClass}
                />
              </Field>
              {formBase && (
                <Field label={t('surveys.sourceStation')}>
                  <select value={station.sourceStationId} onChange={(event) => updateStation(index, { sourceStationId: event.target.value })} className={inputClass}>
                    <option value="">{t('surveys.noInheritedSourceStation')}</option>
                    {(detailQuery.data?.stations ?? []).map((source) => {
                      const usedElsewhere = stations.some((other, otherIndex) => otherIndex !== index && other.sourceStationId === source.id)
                      return (
                        <option key={source.id} value={source.id} disabled={usedElsewhere} dir="auto">
                          {t('surveys.sourceStationOption', {
                            station: formatNumber(source.station_index + 1, locale, 0),
                            md: fmt(source.md_si, locale),
                            id: source.id,
                          })}
                        </option>
                      )
                    })}
                  </select>
                </Field>
              )}
              {station.sourceStationId && <code dir="ltr" className="self-end text-[10px] text-graphite-500">{t('surveys.sourceStationId', { id: station.sourceStationId })}</code>}
            </div>
            <div className="mt-2 flex justify-end">
              <Button size="sm" variant="ghost" disabled={stations.length <= 2} onClick={() => setStations((current) => current.filter((_, row) => row !== index))}>
                {t('surveys.removeStation')}
              </Button>
            </div>
          </fieldset>
          )
        })}
        <Button size="sm" onClick={() => setStations((current) => [...current, emptyStation()])}>{t('surveys.addStation')}</Button>
      </div>
    )
  }

  const selectedDocument = documents.find((document) => document.id === sourceDocumentId) ?? null
  const importBaseNote = currentImportBase
    ? `${t('surveys.current')} · ${t('surveys.versionValue', { version: formatNumber(currentImportBase.version, locale, 0) })} → ${t('surveys.versionValue', { version: formatNumber(currentImportBase.version + 1, locale, 0) })}`
    : t('surveys.createNewSeries')

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('surveys.title')}
        description={t('surveys.subtitle')}
        actions={
          <>
            <Button size="sm" onClick={() => startManual()} disabled={!wellboreId}>{t('surveys.newManual')}</Button>
            <Button size="sm" variant="primary" onClick={startImport} disabled={!wellboreId}>{t('surveys.import')}</Button>
          </>
        }
      />

      <p role="note" className="rounded-md border border-warning/40 bg-amber-50/50 p-2 text-xs text-graphite-700 dark:bg-amber-950/20 dark:text-graphite-200">
        {t('surveys.notEstablished')}
      </p>
      {savedMessage && <p role="status" className="rounded border border-ok/30 bg-emerald-50/50 p-2 text-xs text-ok dark:bg-emerald-950/20">{savedMessage}</p>}

      <Card title={t('surveys.wellbore')} dense>
        {wellQuery.error && <ErrorState error={wellQuery.error} onRetry={wellQuery.refetch} />}
        {wellQuery.isPending ? <Loading /> : wellbores.length === 0 ? <EmptyState message={t('surveys.noWellbores')} /> : (
          <Field label={t('surveys.selectWellbore')}>
            <select value={wellboreId} onChange={(event) => { setWellboreId(event.target.value); setSelectedTrajectoryId(''); setBaselineId(''); setMode(null); setFormBase(null); setComparePair(null) }} className={`${inputClass} max-w-xl`}>
              <option value="">{t('surveys.selectWellbore')}</option>
              {wellbores.map((bore) => <option key={bore.id} value={bore.id} dir="auto">{bore.name} · {wellborePurposeLabel(bore.purpose, t)} · {bore.id}</option>)}
            </select>
          </Field>
        )}
      </Card>

      {wellboreId && (
        <div className="grid gap-4 xl:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.6fr)]">
          <Card title={t('surveys.revisions')} subtitle={t('surveys.revisionCount', { count: formatNumber(boreSurveys.length, locale, 0) })} dense>
            {surveyQuery.error && <ErrorState error={surveyQuery.error} onRetry={surveyQuery.refetch} />}
            {surveyQuery.isPending ? <Loading /> : boreSurveys.length === 0 ? <EmptyState message={t('surveys.noTrajectories')} /> : (
              <ul className="space-y-2">
                {boreSurveys.map((item) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={() => { setSelectedTrajectoryId(item.id); setComparePair(null); setSavedMessage(null) }}
                      aria-pressed={item.id === selectedTrajectoryId}
                      className={`w-full rounded-md border p-2 text-start ${item.id === selectedTrajectoryId ? 'border-signal bg-signal/5' : 'border-graphite-200 hover:bg-graphite-50 dark:border-graphite-800 dark:hover:bg-graphite-900'}`}
                    >
                      <span className="flex flex-wrap items-center justify-between gap-1">
                        <span className="font-medium" dir="auto">{item.name}</span>
                        <span className="flex gap-1">
                          {item.is_current && <Badge tone="ok">{t('surveys.current')}</Badge>}
                          <Badge tone="neutral">{t('surveys.versionValue', { version: formatNumber(item.version, locale, 0) })}</Badge>
                        </span>
                      </span>
                      <span className="mt-1 block text-[11px] text-graphite-500" dir="auto">{kindLabel(item.kind, t)} · {formatNumber(item.station_count, locale, 0)} {t('surveys.stations')} · {datumLabel(item.datum, t)}</span>
                      <code dir="ltr" className="mt-1 block truncate text-[10px] text-graphite-500">{item.id}</code>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <div className="space-y-4">
            {!selectedTrajectoryId ? (
              <Card><EmptyState message={t('surveys.selectTrajectory')} /></Card>
            ) : detailQuery.isPending ? <Card><Loading /></Card> : detailQuery.error ? <Card><ErrorState error={detailQuery.error} onRetry={detailQuery.refetch} /></Card> : detailQuery.data ? (
              <>
                <Card
                  title={<span dir="auto">{detailQuery.data.trajectory.name} · {t('surveys.versionValue', { version: formatNumber(detailQuery.data.trajectory.version, locale, 0) })}</span>}
                  subtitle={`${kindLabel(detailQuery.data.trajectory.kind, t)} · ${formatNumber(detailQuery.data.stations.length, locale, 0)} ${t('surveys.stations')}`}
                  actions={
                    <>
                      <Button size="sm" onClick={() => startManual({
                        id: detailQuery.data!.trajectory.id,
                        version: detailQuery.data!.trajectory.version,
                        name: detailQuery.data!.trajectory.name,
                        kind: detailQuery.data!.trajectory.kind,
                        datum: detailQuery.data!.trajectory.datum,
                      })} disabled={!detailQuery.data.trajectory.is_current}>{t('surveys.reviseSelected')}</Button>
                      <Button size="sm" onClick={() => exportMutation.mutate(detailQuery.data!.trajectory.id)} disabled={exportMutation.isPending}>{t('surveys.exportCsv')}</Button>
                    </>
                  }
                >
                  {exportMutation.error && <ErrorState error={exportMutation.error} />}
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                    {[
                      [t('surveys.datum'), `${datumLabel(detailQuery.data.trajectory.datum, t)} · ${datumProvenanceLabel(detailQuery.data.trajectory.datum_provenance, t)}`],
                      [t('surveys.calculationState'), calculationStateLabel(detailQuery.data.trajectory.calculation_state, t)],
                      [t('surveys.acceptance'), engineeringAcceptanceLabel(detailQuery.data.trajectory.engineering_acceptance, t)],
                      [t('surveys.sourceDocument'), detailQuery.data.trajectory.source_document_id ?? t('common.unknown')],
                      [t('surveys.engine'), `${detailQuery.data.trajectory.engine_key ?? t('common.unknown')} · ${detailQuery.data.trajectory.engine_version ?? t('common.unknown')}`],
                      [t('surveys.inputsHash'), detailQuery.data.trajectory.inputs_hash ?? t('common.unknown')],
                      [t('surveys.outputsHash'), detailQuery.data.trajectory.outputs_hash ?? t('common.unknown')],
                      [t('surveys.revisionReason'), detailQuery.data.trajectory.revision_reason ?? t('common.unknown')],
                    ].map(([label, value]) => (
                      <div key={label} className="min-w-0 rounded border border-graphite-100 p-2 dark:border-graphite-800">
                        <div className="text-[10px] uppercase text-graphite-500">{label}</div>
                        <div dir="auto" className="break-words text-xs">{value}</div>
                      </div>
                    ))}
                  </div>
                  <p className="mt-3 text-[11px] text-warning">{t('surveys.notEstablished')}</p>
                  {detailQuery.data.trajectory.warnings.length > 0 && (
                    <ul className="mt-2 list-disc ps-4 text-[11px] text-warning">{detailQuery.data.trajectory.warnings.map((warning, index) => <li key={index} dir="auto">{surveyWarningLabel(warning, t, locale)}</li>)}</ul>
                  )}
                </Card>

                <Card title={t('surveys.plot')} subtitle={t('surveys.plotNote')} dense>
                  <div className="grid gap-3 lg:grid-cols-2">
                    <section aria-label={t('surveys.planView')}>
                      <h3 className="mb-1 text-xs font-medium">{t('surveys.planView')}</h3>
                      <PlanView stations={detailQuery.data.stations} locale={locale} title={t('surveys.planView')} eastLabel={t('surveys.east')} northLabel={t('surveys.north')} emptyLabel={t('surveys.noPlot')} t={t} />
                    </section>
                    <section aria-label={t('surveys.depthView')}>
                      <h3 className="mb-1 text-xs font-medium">{t('surveys.depthView')}</h3>
                      <DepthView stations={detailQuery.data.stations} locale={locale} title={t('surveys.depthView')} mdLabel={t('surveys.md')} tvdLabel={t('surveys.tvd')} emptyLabel={t('surveys.noPlot')} positiveDownLabel={t('surveys.positiveDown')} t={t} />
                    </section>
                  </div>
                </Card>

                <Card title={t('surveys.stationTable')} dense>
                  <div className="overflow-x-auto">
                    <Table
                      rows={detailQuery.data.stations}
                      rowKey={(row) => row.id}
                      empty={<EmptyState message={t('surveys.noTrajectories')} />}
                      columns={[
                        { key: 'index', header: t('surveys.stationNumber'), render: (row) => formatNumber(row.station_index + 1, locale, 0), align: 'end' },
                        { key: 'md', header: <UnitHeading label={t('surveys.md')} unit="m" />, render: (row) => fmt(row.md_si, locale), align: 'end' },
                        { key: 'inclination', header: <UnitHeading label={t('surveys.inclination')} unit="deg" />, render: (row) => fmt(row.inclination_deg, locale), align: 'end' },
                        { key: 'input-azimuth', header: <UnitHeading label={t('surveys.inputAzimuth')} unit="deg" />, render: (row) => <span dir="auto">{fmt(row.input_azimuth_deg, locale)} · {referenceLabel(row.input_azimuth_reference ?? 'unknown', t)}</span>, align: 'end' },
                        { key: 'azimuth', header: <UnitHeading label={t('surveys.azimuth')} unit="deg" />, render: (row) => fmt(row.azimuth_deg, locale), align: 'end' },
                        { key: 'tvd', header: <UnitHeading label={t('surveys.tvd')} unit="m" />, render: (row) => fmt(row.tvd_si, locale), align: 'end' },
                        { key: 'north', header: <UnitHeading label={t('surveys.north')} unit="m" />, render: (row) => fmt(row.ns_si, locale), align: 'end' },
                        { key: 'east', header: <UnitHeading label={t('surveys.east')} unit="m" />, render: (row) => fmt(row.ew_si, locale), align: 'end' },
                        { key: 'dls', header: <UnitHeading label={t('surveys.dls')} unit="deg/30 m" />, render: (row) => fmt(row.dls_deg_per_30m, locale), align: 'end' },
                        { key: 'measured-at', header: t('surveys.measuredAt'), render: (row) => formatDateTime(row.measured_at, locale) },
                        { key: 'source', header: t('surveys.source'), render: (row) => <span dir="auto">{stationSourceLabel(row.source, t)}<code dir="ltr" className="block text-[10px] text-graphite-500">{row.evidence_ref ?? t('surveys.sourceManualUnreferenced')}</code></span> },
                      ]}
                    />
                  </div>
                  <div className="mt-3 grid gap-2 md:grid-cols-[1fr_auto] md:items-end">
                    <Field label={t('surveys.compareWith')} hint={t('surveys.compareHint')}>
                      <select value={baselineId} onChange={(event) => { setBaselineId(event.target.value); setComparePair(null) }} className={inputClass}>
                        <option value="">{t('surveys.compareEmpty')}</option>
                        {detailQuery.data.history.filter((row) => row.id !== detailQuery.data?.trajectory.id).map((row) => <option key={row.id} value={row.id} dir="auto">{t('surveys.versionValue', { version: formatNumber(row.version, locale, 0) })} · {row.name} · {row.id}</option>)}
                      </select>
                    </Field>
                    <Button size="sm" variant="primary" disabled={!baselineId || compareQuery.isFetching} onClick={() => setComparePair({ current: detailQuery.data!.trajectory.id, baseline: baselineId })}>{t('surveys.compare')}</Button>
                  </div>
                  {compareQuery.error && <div className="mt-2"><ErrorState error={compareQuery.error} /></div>}
                  {compareQuery.data && comparePair?.current === detailQuery.data.trajectory.id && (
                    <div className="mt-3 space-y-2">
                      <p className="text-[11px] text-graphite-500">{t('surveys.compareHint')} · {t('surveys.comparisonCounts', {
                        matched: formatNumber(compareQuery.data.matched_count, locale, 0),
                        added: formatNumber(compareQuery.data.added_count, locale, 0),
                        removed: formatNumber(compareQuery.data.removed_count, locale, 0),
                      })}</p>
                      <div className="overflow-x-auto">
                        <Table
                          rows={compareQuery.data.items}
                          rowKey={(row) => `${row.md_si}-${row.state}`}
                          columns={[
                            { key: 'md', header: <UnitHeading label={t('surveys.md')} unit="m" />, render: (row) => fmt(row.md_si, locale), align: 'end' },
                            { key: 'state', header: t('common.status'), render: (row) => <Badge tone={row.state === 'matched_exact_md' ? 'ok' : 'warning'}>{comparisonStateLabel(row.state, t)}</Badge> },
                            { key: 'tvd', header: <UnitHeading label={t('surveys.delta', { field: t('surveys.tvd') })} unit="m" />, render: (row) => fmt(row.delta.tvd_si, locale), align: 'end' },
                            { key: 'ns', header: <UnitHeading label={t('surveys.delta', { field: t('surveys.north') })} unit="m" />, render: (row) => fmt(row.delta.ns_si, locale), align: 'end' },
                            { key: 'ew', header: <UnitHeading label={t('surveys.delta', { field: t('surveys.east') })} unit="m" />, render: (row) => fmt(row.delta.ew_si, locale), align: 'end' },
                            { key: 'inclination', header: <UnitHeading label={t('surveys.delta', { field: t('surveys.inclination') })} unit="deg" />, render: (row) => fmt(row.delta.inclination_deg, locale), align: 'end' },
                            { key: 'azimuth', header: <UnitHeading label={t('surveys.delta', { field: t('surveys.azimuth') })} unit="deg" />, render: (row) => fmt(row.delta.azimuth_deg, locale), align: 'end' },
                          ]}
                        />
                      </div>
                    </div>
                  )}
                </Card>
              </>
            ) : null}
          </div>
        </div>
      )}

      {mode && (
        <Card
          title={mode === 'manual' ? (formBase ? t('surveys.reviseSelected') : t('surveys.newManual')) : t('surveys.import')}
          subtitle={mode === 'import' ? t('surveys.reviewRequired', { humanValidated: t('surveys.reviewStateHumanValidated') }) : undefined}
          actions={<Button size="sm" variant="ghost" onClick={() => { setMode(null); setFormError(null) }}>{t('common.cancel')}</Button>}
        >
          <form onSubmit={mode === 'manual' ? submitManual : submitImport} className="space-y-4" noValidate>
            <div className="grid gap-3 md:grid-cols-2">
              <Field label={t('surveys.name')}>
                <input required maxLength={160} value={formName} readOnly={Boolean(formBase)} onChange={(event) => setFormName(event.target.value)} className={inputClass} />
              </Field>
              <Field label={t('surveys.kind')}>
                <select value={formKind} disabled={Boolean(formBase)} onChange={(event) => setFormKind(event.target.value as SurveyTrajectoryKind)} className={inputClass}>
                  {!(kindOptions as readonly string[]).includes(formKind) && <option value={formKind} dir="auto">{kindLabel(formKind, t)}</option>}
                  {kindOptions.map((kind) => <option key={kind} value={kind}>{kindLabel(kind, t)}</option>)}
                </select>
              </Field>
              <Field label={t('surveys.reason')}>
                <textarea required minLength={1} maxLength={2000} rows={2} value={reason} onChange={(event) => setReason(event.target.value)} className={inputClass} />
              </Field>
              {mode === 'import' && (
                <Field label={t('surveys.surveyReport')}>
                  <select value={sourceDocumentId} onChange={(event) => { setSourceDocumentId(event.target.value); setSelectedRecordIds([]) }} className={inputClass}>
                    <option value="">{t('surveys.selectDocument')}</option>
                    {documents.map((document) => <option key={document.id} value={document.id}>{document.title} · {document.id}</option>)}
                  </select>
                </Field>
              )}
            </div>
            {mode === 'import' && currentImportBase && <p className="text-xs text-info">{importBaseNote} · {datumLabel(currentImportBase.datum, t)}</p>}
            {renderCalculationFields(Boolean(formBase || currentImportBase))}

            {mode === 'manual' ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold">{t('surveys.stations')}</h3>
                  <p className="text-[11px] text-graphite-500">{t('surveys.canonicalUnitNotice')}</p>
                </div>
                {renderStations()}
              </>
            ) : (
              <div className="space-y-3">
                <p className="text-xs text-graphite-600 dark:text-graphite-300">{t('surveys.importNote')}</p>
                {sourceDocumentId && (
                  <Link to={`/wells/${encodeURIComponent(id)}/documents?document=${encodeURIComponent(sourceDocumentId)}`} className="inline-flex rounded border border-signal px-2 py-1 text-xs text-signal-deep hover:bg-signal/5 dark:text-signal-light">
                    {t('surveys.openSourceReview')}
                  </Link>
                )}
                {documentList.error && <ErrorState error={documentList.error} onRetry={documentList.refetch} />}
                {documentQuery.error && <ErrorState error={documentQuery.error} onRetry={documentQuery.refetch} />}
                {documentList.isPending && <Loading />}
                {sourceDocumentId && documentQuery.isPending && <Loading />}
                {sourceDocumentId && sourceRecords.length === 0 && <EmptyState message={t('surveys.noSurveyRecords')} />}
                {sourceRecords.length > 0 && (
                  <div className="overflow-x-auto">
                    <Table
                      rows={sourceRecords}
                      rowKey={(row) => row.id}
                      columns={[
                        {
                          key: 'select', header: t('surveys.selectedRecords'), render: (record) => {
                            const isEligible = eligibleRecords.some((candidate) => candidate.id === record.id)
                            return <input type="checkbox" aria-label={`${t('surveys.sourceRecords')} ${record.id}`} checked={selectedRecordIds.includes(record.id)} disabled={!isEligible} onChange={() => toggleRecord(record.id)} />
                          },
                        },
                        { key: 'id', header: t('surveys.identifier'), render: (record) => <code dir="ltr" className="text-[10px]">{record.id}</code> },
                        { key: 'page', header: t('common.page'), render: (record) => record.page_number === null ? t('common.unknown') : formatNumber(record.page_number, locale, 0), align: 'end' },
                        { key: 'state', header: t('common.status'), render: (record) => <Eligibility record={record} t={t} /> },
                        {
                          key: 'values',
                          header: t('surveys.stations'),
                          render: (record) => (
                            <span dir="auto">
                              {t('surveys.surveyRecordSummary', {
                                md: extractedNumber(record.payload.md_si, locale, t),
                                inclination: extractedNumber(record.payload.inclination_deg, locale, t),
                                azimuth: extractedNumber(record.payload.azimuth_deg, locale, t),
                              })}
                            </span>
                          ),
                        },
                      ]}
                    />
                  </div>
                )}
                {selectedDocument && <p className="text-xs text-graphite-500">{formatNumber(eligibleRecords.length, locale, 0)} {t('surveys.eligible')} · {formatNumber(sourceRecords.length - eligibleRecords.length, locale, 0)} {t('surveys.ineligible')} · {formatNumber(selectedEligibleRecordIds.length, locale, 0)} {t('surveys.selectedRecords')}</p>}
              </div>
            )}

            {formError && <p role="alert" className="rounded border border-danger/40 bg-red-50/50 p-2 text-xs text-danger dark:bg-red-950/20">{formError}</p>}
            {createMutation.error && <ErrorState error={createMutation.error} />}
            {importMutation.error && <ErrorState error={importMutation.error} />}
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" onClick={() => { setMode(null); setFormError(null) }}>{t('common.cancel')}</Button>
              {mode === 'manual' ? (
                <Button type="submit" variant="primary" disabled={createMutation.isPending}>{createMutation.isPending ? t('common.loading') : t('surveys.createManual')}</Button>
              ) : (
                <Button type="submit" variant="primary" disabled={importMutation.isPending || selectedEligibleRecordIds.length < 2}>{importMutation.isPending ? t('common.loading') : t('surveys.importCalculate')}</Button>
              )}
            </div>
          </form>
        </Card>
      )}
    </div>
  )
}
