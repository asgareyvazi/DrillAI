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
  sections: { facts: 0, calculations: 0, evidence: 0, inference: 0, recommendation: 0, unknown: 0 },
  sources: [],
})

describe('Advisor copy-only response contract', () => {
  it('renders text from the generated narrative envelope instead of treating the envelope as React text', () => {
    const statement = 'Recorded measured depth is 1,500 m.'
    render(
      <I18nProvider>
        <AnswerView
          answer={answer({ status: 'generated', text: statement, model: 'stub-model', provider: 'stub-provider' })}
          onEvidence={() => undefined}
          llmRequested
        />
      </I18nProvider>,
    )

    expect(screen.getByText(statement)).toBeInTheDocument()
    expect(screen.getByText(/گزاره‌های رونوشت‌شده از stub-model/)).toBeInTheDocument()
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

describe('Advisor calculation safety status', () => {
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
