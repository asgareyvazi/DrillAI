/** Structured entry for the existing capacity and laminar hydraulics engines.
 * A persisted engine run is not an approved design revision or a field validation.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { drillingApi } from '../../api/endpoints'
import { Button, Card, ErrorState, Field, Loading } from '../../components/common'
import { UnitAwareResult } from '../../components/engineering/UnitAwareResult'
import { PageHeader } from '../../components/layout/AppShell'
import { useActionGate } from '../../hooks/useActionGate'
import { useI18n } from '../../i18n'

type Row = { kind: string; top: string; bottom: string; od: string; id: string }
const blank = (): Row => ({ kind: 'hole', top: '', bottom: '', od: '', id: '' })
const kinds = ['hole', 'open_hole', 'casing', 'liner', 'riser', 'drillpipe', 'hwdp', 'drillcollar', 'tubing', 'bha']
const cls = 'w-full rounded border border-graphite-300 bg-white p-1 text-sm dark:border-graphite-700 dark:bg-graphite-900'

export function requiredNumber(value: string): number {
  if (!value.trim() || !Number.isFinite(Number(value))) throw new Error('Missing or invalid numeric input / مقدار عددی نامعتبر یا مفقود')
  return Number(value)
}

export default function CirculationWorkspace() {
  const { wellId = '' } = useParams<{ wellId: string }>()
  const { locale } = useI18n()
  const tr = (en: string, fa: string) => locale === 'fa' ? fa : en
  const client = useQueryClient()
  const gate = useActionGate('engine.run')
  const wellbores = useQuery({ queryKey: ['wellbores', wellId], queryFn: ({ signal }) => drillingApi.listWellbores(wellId, signal) })
  const [wellbore, setWellbore] = useState('')
  const [mode, setMode] = useState<'capacity' | 'hydraulics'>('capacity')
  const [rows, setRows] = useState<Row[]>([blank()])
  const [values, setValues] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [selected, setSelected] = useState('')
  const key = mode === 'capacity' ? 'wellbore.capacity' : 'hydraulics.laminar'
  const history = useQuery({ queryKey: ['circulation-runs', wellId, key], queryFn: ({ signal }) => drillingApi.wellEngineRuns(wellId, { engine_key: key, limit: 50 }, signal) })
  const run = useMutation({
    mutationFn: (inputs: Record<string, unknown>) => drillingApi.runEngine(key, { well_id: wellId, wellbore_id: wellbore, inputs }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['circulation-runs', wellId] }) },
  })
  function changed() { setError(''); setSelected(''); run.reset() }
  function field(name: string, en: string, fa: string, numeric = false) {
    return <Field label={tr(en, fa)}><input className={cls} type={numeric ? 'number' : 'text'} step={numeric ? 'any' : undefined} value={values[name] ?? ''} onChange={(e) => { changed(); setValues({ ...values, [name]: e.target.value }) }} /></Field>
  }
  function setRow(index: number, name: keyof Row, value: string) {
    changed(); setRows(rows.map((row, i) => i === index ? { ...row, [name]: value } : row))
  }
  function submit() {
    try {
      if (!wellbore) throw new Error(tr('Select a wellbore.', 'یک شاخه چاه انتخاب کنید.'))
      const elements = rows.map((row) => ({ kind: row.kind, from_depth_si: requiredNumber(row.top), to_depth_si: requiredNumber(row.bottom), od_si: requiredNumber(row.od), ...(row.id.trim() ? { id_si: requiredNumber(row.id) } : {}) }))
      const num = (name: string) => requiredNumber(values[name] ?? '')
      const payload: Record<string, unknown> = { elements }
      if (mode === 'capacity') {
        payload.current_depth_si = num('depth')
        if (values.flow?.trim()) payload.flow_rate_si = num('flow')
        if (values.pump?.trim() || values.spm?.trim() || values.count?.trim()) payload.pump = {
          output_per_stroke_si: num('pump'), ...(values.spm?.trim() ? { strokes_per_minute: num('spm') } : {}),
          count: num('count'),
        }
      } else {
        if (!['origin', 'datum', 'frame', 'survey', 'source'].every((name) => values[name]?.trim())) throw new Error(tr('All depth reference fields are required.', 'همه فیلدهای مرجع عمق الزامی است.'))
        Object.assign(payload, { bit_depth_si: num('depth'), bit_tvd_si: num('tvd'), flow_rate_si: num('flow'),
          mud_weight_si: num('density'), plastic_viscosity_si: num('pv'), yield_point_si: num('yp'),
          depth_reference: { md_origin: values.origin, tvd_origin: values.origin, vertical_datum: values.datum, coordinate_reference: values.frame, survey_reference: values.survey, source_reference: values.source, depth_positive: 'down' },
          ...(values.tfa?.trim() ? { tfa_si: num('tfa'), discharge_coefficient: num('discharge') } : {}) })
      }
      setError(''); setSelected(''); run.mutate(payload)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const old = history.data?.items.find((item) => item.id === selected && item.wellbore_id === wellbore)
  const current = run.data
  const result = old ? { id: old.id, version: old.engine_version, outputs: old.outputs, units: old.units, warnings: old.warnings, assumptions: old.assumptions, limitations: old.limitations, violations: old.constraint_violations, inputsHash: old.inputs_hash, outputsHash: old.outputs_hash }
    : current ? { id: current.engine_run_id, version: current.engine.version, outputs: current.outputs, units: current.units, warnings: current.warnings, assumptions: current.assumptions, limitations: current.limitations, violations: current.constraint_violations, inputsHash: current.inputs_hash, outputsHash: current.outputs_hash } : null
  return <div className="space-y-4">
    <PageHeader title={tr('Capacity, circulation & laminar hydraulics', 'ظرفیت، گردش و هیدرولیک لایه‌ای')} description={tr('Structured calculation entry; saved runs are not approved design revisions.', 'ورود ساخت‌یافته محاسبه؛ اجراهای ذخیره‌شده نسخه طراحی تأییدشده نیستند.')} />
    <p className="text-sm text-warning" role="note">{tr('Nominal concentric geometry only; no washout or tool joints. Hydraulics uses laminar Bingham pipe flow and an annular slot approximation; turbulent results are not certified. Depth references are caller declarations, not verified surveys. This is not a complete design, report or approval workflow.', 'فقط هندسه اسمی هم‌مرکز؛ بدون شستگی یا اتصالات. هیدرولیک از جریان لایه‌ای بینگهام و تقریب شکاف حلقوی استفاده می‌کند؛ نتایج آشفته معتبرسازی نشده‌اند. مراجع عمق اظهار کاربرند و برداشت تأییدشده نیستند. این صفحه گردش‌کار کامل طراحی، گزارش یا تأیید نیست.')}</p>
    <Card title={tr('Scope and model', 'محدوده و مدل')}>
      {wellbores.isPending && <Loading />}{wellbores.isError && <ErrorState error={wellbores.error} />}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={tr('Wellbore', 'شاخه چاه')}><select disabled={run.isPending} className={cls} value={wellbore} onChange={(e) => { changed(); setWellbore(e.target.value) }}><option value="">{tr('Select a wellbore', 'انتخاب شاخه چاه')}</option>{wellbores.data?.items.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></Field>
        <Field label={tr('Calculation', 'محاسبه')}><select disabled={run.isPending} className={cls} value={mode} onChange={(e) => { changed(); setMode(e.target.value as 'capacity' | 'hydraulics') }}><option value="capacity">{tr('Capacity & circulation', 'ظرفیت و گردش')}</option><option value="hydraulics">{tr('Laminar hydraulics & bit ECD', 'هیدرولیک لایه‌ای و ECD مته')}</option></select></Field>
      </div>
    </Card>
    <Card title={tr('Geometry sections (SI)', 'بخش‌های هندسه (SI)')} subtitle={tr('Every interval must be explicit. Missing inner geometry is not assumed to be empty; scoped open-bore declarations are not yet supported in this form.', 'هر بازه باید صریح باشد. هندسه داخلی ناموجود خالی فرض نمی‌شود؛ اظهار بازه چاه خالی در این فرم هنوز پشتیبانی نمی‌شود.')}>
      <fieldset disabled={run.isPending} className="space-y-3">
        {rows.map((row, i) => <div key={i} className="grid gap-2 rounded border p-2 sm:grid-cols-6">
          <Field label={`${tr('Kind', 'نوع')} ${i + 1}`}><select className={cls} value={row.kind} onChange={(e) => setRow(i, 'kind', e.target.value)}>{kinds.map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></Field>
          {(['top', 'bottom', 'od', 'id'] as const).map((name) => <Field key={name} label={`${({ top: tr('Top MD', 'عمق بالایی'), bottom: tr('Bottom MD', 'عمق پایینی'), od: tr('OD / bore', 'قطر بیرونی / چاه'), id: tr('ID', 'قطر داخلی') })[name]} ${i + 1} (m)`}><input type="number" step="any" className={cls} value={row[name]} onChange={(e) => setRow(i, name, e.target.value)} /></Field>)}
          <Button onClick={() => { changed(); setRows(rows.filter((_, index) => index !== i)) }} disabled={rows.length === 1}>{tr('Remove', 'حذف')}</Button>
        </div>)}
        <Button onClick={() => { changed(); setRows([...rows, blank()]) }}>{tr('Add section', 'افزودن بخش')}</Button>
      </fieldset>
    </Card>
    <Card title={tr('Operating inputs (SI)', 'ورودی‌های عملیاتی (SI)')}>
      <fieldset disabled={run.isPending} className="grid gap-3 sm:grid-cols-3">
        {field('depth', 'Current bit MD (m)', 'عمق اندازه‌گیری مته (m)', true)}{field('flow', 'Flow (m³/s)', 'دبی (m³/s)', true)}
        {mode === 'capacity' ? <>{field('pump', 'Pump output (m³/stroke)', 'خروجی پمپ (m³/stroke)', true)}{field('spm', 'Strokes/minute per pump', 'ضربه در دقیقه هر پمپ', true)}{field('count', 'Active pump count (required with pump)', 'تعداد پمپ فعال (الزامی با پمپ)', true)}</> : <>
          {field('tvd', 'Bit TVD from same origin (m)', 'عمق قائم مته از همان مبدأ (m)', true)}{field('density', 'Density (kg/m³)', 'چگالی (kg/m³)', true)}{field('pv', 'Plastic viscosity (Pa·s)', 'گرانروی پلاستیک (Pa·s)', true)}{field('yp', 'Yield point (Pa)', 'نقطه تسلیم (Pa)', true)}{field('tfa', 'TFA (m²; optional)', 'سطح نازل (m²؛ اختیاری)', true)}{field('discharge', 'Discharge coefficient (required with TFA)', 'ضریب تخلیه (الزامی با TFA)', true)}
          {field('origin', 'Common MD/TVD origin ID', 'شناسه مبدأ مشترک MD/TVD')}{field('datum', 'Vertical datum', 'مبنای قائم')}{field('frame', 'Coordinate reference', 'مرجع مختصات')}{field('survey', 'Survey revision reference', 'مرجع نسخه برداشت')}{field('source', 'Source / provenance', 'منبع / منشأ')}
        </>}
      </fieldset>
      <div className="mt-3"><Button variant="primary" onClick={submit} disabled={run.isPending || gate.suggestDisabled || !wellbore || wellbores.isError}>{tr('Calculate and save run', 'محاسبه و ذخیره اجرا')}</Button></div>
      {gate.state !== 'ready' && <p>{gate.reason}</p>}{error && <p role="alert">{error}</p>}{run.isError && <ErrorState error={run.error} />}{run.isPending && <Loading />}
    </Card>
    <Card title={tr('Saved calculation records', 'سوابق محاسبه ذخیره‌شده')} subtitle={tr('Selecting a record shows its saved result; it does not modify the editor.', 'انتخاب سابقه نتیجه ذخیره‌شده را نشان می‌دهد و ویرایشگر را تغییر نمی‌دهد.')}>
      {history.isError && <ErrorState error={history.error} />}
      <Field label={tr('Run history', 'تاریخچه اجرا')}><select className={cls} value={selected} onChange={(e) => { run.reset(); setSelected(e.target.value) }}><option value="">{tr('Select a run', 'انتخاب اجرا')}</option>{history.data?.items.filter((item) => item.wellbore_id === wellbore).map((item) => <option key={item.id} value={item.id}>{item.created_at} · {item.id} · v{item.engine_version}</option>)}</select></Field>
      {result && <div className="mt-3 space-y-2"><p dir="ltr">{result.id} · {key} v{result.version}</p><p dir="ltr">input SHA: {result.inputsHash}<br />output SHA: {result.outputsHash}</p><UnitAwareResult value={result.outputs} unitFields={result.units.output_fields} />{[result.warnings, result.assumptions, result.limitations].map((items, i) => <ul key={i}>{items.map((item) => <li key={item}>{item}</li>)}</ul>)}{result.violations.length > 0 && <UnitAwareResult value={{ violations: result.violations }} unitFields={result.units.violation_fields ?? {}} />}</div>}
    </Card>
  </div>
}
