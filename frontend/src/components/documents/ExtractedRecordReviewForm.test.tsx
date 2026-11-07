import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ExtractionRecord } from '../../api/types'
import { I18nProvider } from '../../i18n'
import { ExtractedRecordReviewForm } from './ExtractedRecordReviewForm'

const record: ExtractionRecord = {
  id: 'rec_review_1',
  document_id: 'doc_review_1',
  record_type: 'depth_reading',
  payload: { bit_depth_si: 1000 },
  payload_truncated: false,
  payload_schema_key: 'extracted.depth_reading',
  payload_schema_version: 1,
  page_number: 1,
  region_id: 'reg_review_1',
  depth_md_si: null,
  depth_md_basis: "unknown",
  depth_tvd_si: null,
  depth_tvd_basis: "unknown",
  observed_at: null,
  observed_at_basis: "unknown",
  method: 'label_value',
  method_version: '1.1.0',
  confidence: 0.7,
  validation_state: 'extracted',
  review_version: 1,
  validated_by: null,
  validated_at: null,
  validation_notes: '',
  validation_notes_truncated: false,
  region_unknown: false,
  validation_rule: null,
  quality_flags: [],
  unit_context: { bit_depth_si: 'm' },
  promoted_to_kind: null,
  promoted_to_id: null,
  is_demo_fixture: false,
}

function renderForm(onSubmit = vi.fn(), currentRecord = record, locale: 'en' | 'fa' = 'en') {
  window.localStorage.setItem('drillai.locale', locale)
  const onCancel = vi.fn()
  const onChanged = vi.fn()
  const onRefresh = vi.fn()
  render(
    <I18nProvider>
      <ExtractedRecordReviewForm
        record={currentRecord}
        onSubmit={onSubmit}
        onCancel={onCancel}
        onChanged={onChanged}
        onRefresh={onRefresh}
        pending={false}
        error={null}
      />
    </I18nProvider>,
  )
  return { onSubmit, onCancel, onChanged, onRefresh }
}

describe('ExtractedRecordReviewForm', () => {
  it('shows survey-specific provenance guidance without treating unknown values as compatible', () => {
    const surveyRecord: ExtractionRecord = {
      ...record,
      record_type: 'survey_station',
      payload: {
        md_si: 500,
        inclination_deg: 12,
        azimuth_deg: 180,
        azimuth_reference: 'unknown',
        azimuth_reference_id: null,
        datum: 'unknown',
      },
    }
    renderForm(vi.fn(), surveyRecord)
    expect(screen.getByText(/Keep unknown values unknown/)).toBeInTheDocument()
  })

  it('renders survey-frame guidance in Persian while keeping JSON in LTR order', () => {
    const surveyRecord: ExtractionRecord = {
      ...record,
      record_type: 'survey_station',
      payload: { azimuth_reference: 'unknown', azimuth_reference_id: null, datum: 'unknown' },
    }
    renderForm(vi.fn(), surveyRecord, 'fa')
    expect(screen.getByText(/برای ایستگاه‌های برداشت/)).toBeInTheDocument()
    expect(document.documentElement).toHaveAttribute('dir', 'rtl')
    expect(screen.getByTestId('extracted-record-review-payload')).toHaveAttribute('dir', 'ltr')
    window.localStorage.setItem('drillai.locale', 'en')
  })

  it('submits a versioned human validation with corrected payload and an audit reason', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderForm()
    fireEvent.change(screen.getByTestId('extracted-record-review-payload'), {
      target: { value: '{\n  "bit_depth_si": 1001.5\n}' },
    })
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'Checked against page one')
    await user.click(screen.getByTestId('extracted-record-review-submit'))

    expect(onSubmit).toHaveBeenCalledWith(
      {
        expected_version: 1,
        decision: 'validate',
        reason: 'Checked against page one',
        changes: { payload: { bit_depth_si: 1001.5 } },
      },
      expect.stringMatching(/^ui-record-review-rec_review_1-v1-/),
    )
  })

  it('sends explicit nulls when a reviewer clears existing observed time and canonical depth', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    renderForm(onSubmit, {
      ...record,
      observed_at: '2026-11-01T10:30:00Z',
      depth_md_si: 1200,
    })
    fireEvent.change(screen.getByTestId('extracted-record-review-observed-at'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('extracted-record-review-depth-md'), { target: { value: '' } })
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'Cleared unsupported metadata')
    await user.click(screen.getByTestId('extracted-record-review-submit'))

    expect(onSubmit).toHaveBeenCalledWith(
      {
        expected_version: 1,
        decision: 'validate',
        reason: 'Cleared unsupported metadata',
        changes: { payload: { bit_depth_si: 1000 }, observed_at: null, depth_md_si: null },
      },
      expect.stringMatching(/^ui-record-review-rec_review_1-v1-/),
    )
  })

  it('refuses invalid JSON before sending a review mutation', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderForm()
    fireEvent.change(screen.getByTestId('extracted-record-review-payload'), { target: { value: '{broken' } })
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'Checked source')
    await user.click(screen.getByTestId('extracted-record-review-submit'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Payload is not valid JSON.')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('shows inferred-unit context and requires explicit acknowledgement before validation', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const inferredSurvey: ExtractionRecord = {
      ...record,
      record_type: 'survey_station',
      payload: {
        md_si: 500,
        md_source_value: 500,
        md_source_unit: null,
        inclination_deg: 12,
        azimuth_deg: 180,
        azimuth_reference: 'unknown',
        datum: 'unknown',
        unit_provenance: { md: 'inferred_default', inclination: 'inferred_default' },
      },
      quality_flags: ['unit_inferred'],
      unit_context: { md_si: 'm', inclination_deg: 'deg' },
    }
    renderForm(onSubmit, inferredSurvey)

    expect(screen.getByText(/inferred unit defaults/i)).toBeInTheDocument()
    expect(screen.getByText(/"md_si": "m"/)).toBeInTheDocument()
    const submit = screen.getByTestId('extracted-record-review-submit')
    expect(submit).toBeDisabled()
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'Checked the source page')
    fireEvent.submit(screen.getByTestId('extracted-record-review-form'))
    expect(await screen.findByRole('alert')).toHaveTextContent(/explicitly review and acknowledge inferred units/i)
    expect(onSubmit).not.toHaveBeenCalled()

    await user.click(screen.getByRole('checkbox', { name: /i inspected the source/i }))
    expect(submit).toBeEnabled()
    await user.click(submit)
    expect(onSubmit).toHaveBeenCalledWith(
      {
        expected_version: 1,
        decision: 'validate',
        reason: 'Checked the source page',
        changes: { payload: inferredSurvey.payload, unit_inference_reviewed: true },
      },
      expect.stringMatching(/^ui-record-review-rec_review_1-v1-/),
    )
  })

  it('submits an explicit rejection with a reason and the observed payload', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    renderForm(onSubmit)
    await user.selectOptions(screen.getByTestId('extracted-record-review-decision'), 'reject')
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'The source page does not support this value')
    await user.click(screen.getByTestId('extracted-record-review-submit'))

    expect(onSubmit).toHaveBeenCalledWith(
      {
        expected_version: 1,
        decision: 'reject',
        reason: 'The source page does not support this value',
        changes: { payload: { bit_depth_si: 1000 } },
      },
      expect.stringMatching(/^ui-record-review-rec_review_1-v1-/),
    )
  })

  it('allows a versioned correction and revalidation of an already human-validated row', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const humanValidated = { ...record, validation_state: 'human_validated' }
    renderForm(onSubmit, humanValidated)

    expect(screen.getByTestId('extracted-record-review-decision')).toHaveValue('validate')
    expect(screen.getByRole('option', { name: 'Validate this record' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Reject this record' })).toBeInTheDocument()
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'Second reviewer confirmed the field')
    await user.click(screen.getByTestId('extracted-record-review-submit'))
    expect(onSubmit).toHaveBeenCalledWith(
      {
        expected_version: 1,
        decision: 'validate',
        reason: 'Second reviewer confirmed the field',
        changes: { payload: { bit_depth_si: 1000 } },
      },
      expect.stringMatching(/^ui-record-review-rec_review_1-v1-/),
    )
  })
})
