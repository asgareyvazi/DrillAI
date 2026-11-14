import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import type { AdvisorAnswer } from '../../api/types'
import { I18nProvider } from '../../i18n'
import { AdvisorItemCard, AnswerView } from './AdvisorWorkspace'

beforeEach(() => {
  window.localStorage.setItem('drillai.locale', 'fa')
})

const answer = (narrative: AdvisorAnswer['narrative']): AdvisorAnswer => ({
  well_id: 'wel_test',
  question: 'where_are_we',
  question_text: 'Current depth, section drilled and progress against plan',
  generated_at: '2026-10-27T00:00:00Z',
  facts: [],
  calculations: [],
  evidence: [],
  inference: [],
  recommendation: [],
  unknown: [],
  narrative,
  context_used: {},
  limitations: [],
  grounding: {},
  sections: { facts: 0, calculations: 0, evidence: 0, inference: 0, recommendation: 0, unknown: 0 },
  sources: [],
})

describe('Advisor copy-only response contract', () => {
  it('renders text from the generated narrative envelope instead of treating the envelope as React text', () => {
    const statement = 'Recorded measured depth is 1,500 m.'
    render(
      <I18nProvider>
        <AnswerView
          answer={answer({
            status: 'generated',
            text: statement,
            model: 'stub-model',
            provider: 'stub-provider',
            grounding_refs: [{ section: 'facts', key: 'measured.wob', citation_ids: ['source:measured.wob:measurement_point:tsp_synthetic'] }],
          })}
          onEvidence={() => undefined}
          llmRequested
        />
      </I18nProvider>,
    )

    expect(screen.getByText(statement)).toBeInTheDocument()
    expect(screen.getByText(/گزاره‌های رونوشت‌شده از stub-model/)).toBeInTheDocument()
    expect(screen.getByTestId('advisor-narrative-grounding')).toHaveTextContent('facts: measured.wob')
    expect(screen.getByTestId('advisor-narrative-grounding')).toHaveTextContent('source:measured.wob:measurement_point:tsp_synthetic')
  })

  it('shows a localized withheld state and never renders unsafe provider text', () => {
    const unsafeText = 'This is an unsupported paraphrase.'
    render(
      <I18nProvider>
        <AnswerView
          answer={answer({
            status: 'blocked_by_guardrail',
            text: null,
            model: 'stub-model',
            provider: 'stub-provider',
            note: 'unsafe narration was withheld',
          })}
          onEvidence={() => undefined}
          llmRequested
        />
      </I18nProvider>,
    )

    expect(screen.getByText('پاسخ ارائه‌دهنده با هیچ گزارهٔ کامل منبع تطبیق دقیق نداشت و نمایش داده نشد.')).toBeInTheDocument()
    expect(screen.queryByText(unsafeText)).not.toBeInTheDocument()
  })
})

describe('Advisor claim grounding display', () => {
  it('shows an explicit measurement citation, source scope and stale freshness in Persian', () => {
    render(
      <I18nProvider>
        <ul>
          <AdvisorItemCard
            item={{
              key: 'measured.wob',
              statement: 'Weight on bit = 80,000 N',
              source: 'time_series:wob',
              source_id: 'tms_synthetic',
              evidence_ref: 'tsp_synthetic',
              citations: [
                {
                  citation_id: 'source:measured.wob:measurement_point:tsp_synthetic',
                  kind: 'measurement_point',
                  source: 'time_series:wob',
                  source_id: 'tsp_synthetic',
                  scope: { organization_id: 'org_synthetic', well_id: 'wel_synthetic', wellbore_id: 'wbr_synthetic' },
                },
              ],
              grounding: {
                status: 'cited',
                scope: { organization_id: 'org_synthetic', well_id: 'wel_synthetic', wellbore_id: 'wbr_synthetic' },
                freshness: {
                  status: 'stale',
                  as_of: '2026-10-19T00:00:00Z',
                  age_seconds: 3600,
                  basis: 'source_timestamp',
                },
              },
            }}
          />
        </ul>
      </I18nProvider>,
    )

    expect(screen.getByTestId('advisor-freshness')).toHaveTextContent('تازگی منبع: کهنه')
    expect(screen.getByText('استنادها')).toBeInTheDocument()
    expect(screen.getByTestId('advisor-citation')).toHaveTextContent('نقطهٔ اندازه‌گیری')
    expect(screen.getByTestId('advisor-citation')).toHaveTextContent('tsp_synthetic')
    expect(screen.getByTestId('advisor-citation')).toHaveTextContent('well_id=wel_synthetic')
  })
})

describe('Advisor calculation safety status', () => {
  it('renders stored outputs and violations with explicit unknown units when historical metadata is absent', () => {
    render(
      <I18nProvider>
        <ul>
          <AdvisorItemCard
            item={{
              engine_key: 'well_control.kill_sheet',
              engine_version: 'historical-version',
              statement: 'The engine ran with a result',
              outputs: { kill_rate_si: 0.02 },
              violations: [{ limit: 1_000_000, actual: 1_200_000, unit: 'Pa' }],
              units: {},
              validation_status: 'unknown',
              field_validation_required: true,
              source: 'operations.actual_start',
              source_id: 'opr_test',
              evidence_ref: 'evd_test',
              provenance: {
                actual_start: '2026-11-07T00:00:00+00:00',
                actual_start_basis: 'source_record',
                actual_end: null,
                actual_end_basis: 'unknown',
              },
            }}
          />
          <AdvisorItemCard
            item={{
              key: 'npt.case.evt_synthetic',
              value: 6,
              unit: 'h',
              statement: 'Recorded NPT case SYNTHETIC TEST DATA stuck pipe: 6 h, category well control',
              source: 'npt.event',
              source_id: 'evt_synthetic',
              provenance: { hours_basis: 'reported', started_at_basis: 'unknown' },
            }}
          />
        </ul>
      </I18nProvider>,
    )

    expect(screen.getByText('Recorded NPT case SYNTHETIC TEST DATA stuck pipe: 6 h, category well control')).toBeInTheDocument()
    expect(screen.getByText('شناسهٔ منبع: opr_test')).toBeInTheDocument()
    expect(screen.getByText('ارجاع شاهد: evd_test')).toBeInTheDocument()
    expect(screen.getAllByText('پیشینهٔ ثبت‌شده')).toHaveLength(2)
    expect(screen.getByText(/actual_end_basis/)).toBeInTheDocument()
    expect(screen.getByText('خروجی‌ها')).toBeInTheDocument()
    expect(screen.getByText('نقض محدودیت‌ها')).toBeInTheDocument()
    expect(screen.getAllByText('یکا نامعلوم')).toHaveLength(3)
  })

  it('keeps the field-validation warning in Persian when a historical safety run has unknown status', () => {
    render(
      <I18nProvider>
        <ul>
          <AdvisorItemCard
            item={{
              engine_key: 'well_control.kill_sheet',
              engine_version: 'historical-version',
              statement: 'The engine ran with a feasible result',
              validation_status: 'unknown',
              field_validation_required: true,
            }}
          />
        </ul>
      </I18nProvider>,
    )

    expect(screen.getByText('وضعیت اعتبارسنجی: نامعلوم')).toBeInTheDocument()
    expect(screen.getByTestId('field-validation-warning')).toHaveTextContent('اعتبارسنجی میدانی لازم است')
    expect(screen.getByTestId('field-validation-warning')).toHaveTextContent('پروندهٔ ایمنی')
    expect(screen.getByTestId('field-validation-warning')).toHaveTextContent('مجوز عملیات')
  })

  it('renders the explicit field-validation status in Persian', () => {
    render(
      <I18nProvider>
        <ul>
          <AdvisorItemCard
            item={{
              engine_key: 'tubulars.api_5c3',
              engine_version: '1.0.0',
              statement: 'The engine ran with a result',
              validation_status: 'needs_field_validation',
              field_validation_required: true,
            }}
          />
        </ul>
      </I18nProvider>,
    )

    expect(screen.getByText('وضعیت اعتبارسنجی: نیازمند اعتبارسنجی میدانی')).toBeInTheDocument()
  })

  it('derives the field-validation warning from status when the optional boolean is absent', () => {
    render(
      <I18nProvider>
        <ul>
          <AdvisorItemCard
            item={{
              engine_key: 'well_control.kill_sheet',
              engine_version: '1.0.0',
              statement: 'The engine ran with a result',
              validation_status: 'needs_field_validation',
            }}
          />
        </ul>
      </I18nProvider>,
    )

    expect(screen.getByTestId('field-validation-warning')).toHaveTextContent('اعتبارسنجی میدانی لازم است')
    expect(screen.getByTestId('field-validation-warning')).toHaveTextContent('مجوز عملیات')
  })
})
