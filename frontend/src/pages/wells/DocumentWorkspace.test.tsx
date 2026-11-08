import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DocumentDetail, DocumentRow, ExtractionRecord } from '../../api/types'
import { I18nProvider } from '../../i18n'
import DocumentWorkspace from './DocumentWorkspace'

const apiMock = vi.hoisted(() => ({
  listDocuments: vi.fn(),
  listWellbores: vi.fn(),
  listSections: vi.fn(),
  listOperations: vi.fn(),
  getDocument: vi.fn(),
  getExtractedRecord: vi.fn(),
  documentProvenance: vi.fn(),
  listEvidence: vi.fn(),
  uploadDocument: vi.fn(),
  processDocument: vi.fn(),
  reviewExtractedRecord: vi.fn(),
  documentFileUrl: vi.fn((documentId: string) => `/api/v1/documents/${documentId}/file`),
}))

vi.mock('../../api/endpoints', () => ({
  drillingApi: {
    listDocuments: apiMock.listDocuments,
    listWellbores: apiMock.listWellbores,
    listSections: apiMock.listSections,
    listOperations: apiMock.listOperations,
    getDocument: apiMock.getDocument,
    getExtractedRecord: apiMock.getExtractedRecord,
    documentProvenance: apiMock.documentProvenance,
    listEvidence: apiMock.listEvidence,
    uploadDocument: apiMock.uploadDocument,
    processDocument: apiMock.processDocument,
    reviewExtractedRecord: apiMock.reviewExtractedRecord,
    documentFileUrl: apiMock.documentFileUrl,
  },
}))

const documentRow: DocumentRow = {
  id: 'doc_review_1',
  project_id: null,
  well_id: 'well_review',
  wellbore_id: null,
  section_id: null,
  operation_id: null,
  doc_type: 'ddr',
  title: 'Daily drilling report',
  document_number: null,
  revision: null,
  issue_date: null,
  period_start: null,
  period_end: null,
  language: 'en',
  page_count: 1,
  status: 'indexed',
  extraction_summary: {
    pages: 1,
    regions: 1,
    chunks: 1,
    records: 1,
    evidence_links: 0,
    extractors: { ddr_header: '1.1.0' },
    doc_type: 'ddr',
  },
  has_tables: false,
  has_figures: false,
  ocr_required: false,
  raw_artifact_id: 'artifact_review_1',
  logical_key: null,
  is_demo_fixture: false,
  created_at: '2026-11-01T10:00:00Z',
}

const record: ExtractionRecord = {
  id: 'rec_review_1',
  document_id: documentRow.id,
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

const detail: DocumentDetail = {
  document: documentRow,
  ingestion_jobs: [],
  region_count: 1,
  chunks: [],
  records: [record],
  evidence_links: [],
}

function renderWorkspace() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[`/wells/${documentRow.well_id}/documents?document=${documentRow.id}`]}>
            <Routes>
              <Route path="/wells/:wellId/documents" element={children} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </I18nProvider>
    )
  }
  return render(<DocumentWorkspace />, { wrapper: Wrapper })
}

beforeEach(() => {
  apiMock.listDocuments.mockReset().mockResolvedValue({ items: [documentRow], total: 1 })
  apiMock.listWellbores.mockReset().mockResolvedValue({ items: [], total: 0 })
  apiMock.listSections.mockReset().mockResolvedValue({ items: [], total: 0 })
  apiMock.listOperations.mockReset().mockResolvedValue({ items: [], total: 0 })
  apiMock.getDocument.mockReset().mockResolvedValue(detail)
  apiMock.getExtractedRecord.mockReset().mockResolvedValue(record)
  apiMock.documentProvenance.mockReset().mockResolvedValue({ document_id: documentRow.id, record_count: 0, chain: [] })
  apiMock.listEvidence.mockReset().mockResolvedValue({ items: [], total: 0 })
  apiMock.reviewExtractedRecord.mockReset().mockResolvedValue(record)
  apiMock.uploadDocument.mockReset().mockResolvedValue({
    document: { ...documentRow, id: 'doc_uploaded_xlsx', title: 'operations.xlsx' },
    job: { id: 'job_upload_xlsx', status: 'queued' },
  })
  apiMock.documentFileUrl.mockClear()
})

describe('DocumentWorkspace extracted-record review', () => {
  it('offers supported multi-format uploads and starts the scoped ingestion pipeline', async () => {
    const file = new File(['synthetic operations table'], 'operations.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    renderWorkspace()

    const upload = await screen.findByTestId('document-upload-file')
    expect(upload).toHaveAttribute('accept', '.pdf,.docx,.xlsx,.xlsm,.csv,.tsv,.txt,.md,.log')
    expect(screen.getByText('PDF, DOCX, XLSX/XLSM, CSV/TSV, TXT, Markdown or LOG. Scanned PDF OCR is opt-in.')).toBeInTheDocument()
    expect(screen.getByTestId('document-upload-type')).toContainElement(screen.getByRole('option', { name: 'Drilling document' }))
    fireEvent.change(upload, { target: { files: [file] } })

    await waitFor(() => expect(apiMock.uploadDocument).toHaveBeenCalledOnce())
    expect(apiMock.uploadDocument).toHaveBeenCalledWith(file, {
      well_id: documentRow.well_id,
      doc_type: 'ddr',
    })
  })

  it('uploads with explicit wellbore and section scope from the selected hierarchy', async () => {
    const user = userEvent.setup()
    apiMock.listWellbores.mockResolvedValue({
      items: [{ id: 'wb_review_1', well_id: documentRow.well_id, name: 'Main bore' }],
      total: 1,
    })
    apiMock.listSections.mockResolvedValue({
      items: [{ id: 'sec_review_1', wellbore_id: 'wb_review_1', name: '12-1/4 inch section' }],
      total: 1,
    })
    renderWorkspace()

    await screen.findByRole('option', { name: /Main bore/ })
    await user.selectOptions(screen.getByTestId('document-upload-wellbore'), 'wb_review_1')
    await screen.findByRole('option', { name: /12-1\/4 inch section/ })
    await user.selectOptions(screen.getByTestId('document-upload-section'), 'sec_review_1')
    const file = new File(['synthetic source'], 'scoped-ddr.csv', { type: 'text/csv' })
    fireEvent.change(await screen.findByTestId('document-upload-file'), { target: { files: [file] } })

    await waitFor(() => expect(apiMock.uploadDocument).toHaveBeenCalledOnce())
    expect(apiMock.uploadDocument).toHaveBeenCalledWith(file, {
      well_id: documentRow.well_id,
      wellbore_id: 'wb_review_1',
      section_id: 'sec_review_1',
      doc_type: 'ddr',
    })
  })

  it('opens a bilingual review surface and submits an optimistic, audited correction through the API wrapper', async () => {
    const user = userEvent.setup()
    apiMock.listEvidence.mockResolvedValue({
      items: [{
        id: 'evidence_record_review_1',
        subject_kind: 'extracted_record.depth_reading',
        subject_id: record.id,
        evidence_kind: 'document_region',
        evidence_id: 'reg_review_1',
        document_id: documentRow.id,
        region_id: 'reg_review_1',
        page_number: 1,
        well_id: documentRow.well_id,
        locator: {
          source: { format: 'xlsx', kind: 'table_row', sheet: 'Shift report', row: 12 },
          field_locations: { bit_depth_si: { format: 'xlsx', sheet: 'Shift report', cell: 'D12' } },
        },
        excerpt: 'Bit depth: 1000 m',
        confidence: 0.7,
        relevance: 1,
        weight: 1,
        method: 'label_value',
        quote_verified: true,
        quote_check: 'region_text',
        created_at: '2026-11-01T10:00:00Z',
      }],
      total: 1,
    })
    renderWorkspace()

    await user.click(await screen.findByTestId(`extracted-record-review-open-${record.id}`))
    expect(await screen.findByRole('heading', { name: 'Review extracted record' })).toBeInTheDocument()
    const sourceEvidence = await screen.findByTestId('extracted-record-review-evidence')
    expect(sourceEvidence).toHaveTextContent('Source locator')
    expect(sourceEvidence).toHaveTextContent('field_locations')
    expect(sourceEvidence).toHaveTextContent('D12')
    fireEvent.change(screen.getByTestId('extracted-record-review-payload'), {
      target: { value: '{\n  "bit_depth_si": 1001.5\n}' },
    })
    await user.type(screen.getByTestId('extracted-record-review-reason'), 'Checked against the source page')
    await user.click(screen.getByTestId('extracted-record-review-submit'))

    await waitFor(() => expect(apiMock.reviewExtractedRecord).toHaveBeenCalledOnce())
    expect(apiMock.reviewExtractedRecord).toHaveBeenCalledWith(
      documentRow.id,
      record.id,
      {
        expected_version: 1,
        decision: 'validate',
        reason: 'Checked against the source page',
        changes: { payload: { bit_depth_si: 1001.5 } },
      },
      expect.stringMatching(/^ui-record-review-rec_review_1-v1-/),
    )
  })

  it('does not offer editing of a payload still truncated by the bounded review endpoint', async () => {
    const user = userEvent.setup()
    apiMock.getExtractedRecord.mockResolvedValue({ ...record, payload_truncated: true })
    renderWorkspace()

    await user.click(await screen.findByTestId(`extracted-record-review-open-${record.id}`))

    expect(await screen.findByRole('alert')).toHaveTextContent('exceeds the safe review payload limit')
    expect(screen.queryByTestId('extracted-record-review-form')).not.toBeInTheDocument()
  })

  it('renders the review form in Persian with RTL layout and an LTR JSON editor', async () => {
    const user = userEvent.setup()
    window.localStorage.setItem('drillai.locale', 'fa')
    renderWorkspace()

    await user.click(await screen.findByTestId(`extracted-record-review-open-${record.id}`))

    expect(await screen.findByRole('heading', { name: 'بازبینی رکورد استخراج‌شده' })).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getByTestId('extracted-record-review-payload')).toHaveAttribute('dir', 'ltr')
    expect(screen.getByText('استخراج‌شده')).toBeInTheDocument()
  })
})
