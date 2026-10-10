/**
 * Journey 2 — a document through ingestion to records and evidence.
 *
 * The point of this journey is the chain, not the screen: a file goes in through the browser, the
 * backend's extractors run, and a value can be traced back to the page it came from. The spec uploads
 * the same synthetic Daily Drilling Report the backend's ingestion tests use, then asks the API what
 * it extracted — so a UI that renders a plausible table while the pipeline produced nothing fails.
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { apiGet, test, expect, wellPath } from './fixtures'

const here = path.dirname(fileURLToPath(import.meta.url))
const UPLOAD_NAME = 'synthetic_program_upload.txt'
const UPLOAD_PATH = path.join(here, 'assets', UPLOAD_NAME)

type DocumentRow = {
  id: string
  title: string
  doc_type: string
  page_count: number
  status: string
  extraction_summary: {
    pages: number
    regions: number
    chunks: number
    records: number
    evidence_links: number
    extractors: Record<string, string>
  }
}

type DocumentPage = { items: DocumentRow[]; total: number }

type ExtractionRecord = {
  id: string
  document_id: string
  record_type: string
  method: string | null
  method_version: string | null
  confidence: number | null
  validation_state: string
  page_number: number | null
  payload: Record<string, unknown>
}

type DocumentDetail = {
  document: DocumentRow
  ingestion_jobs: { id: string; status: string; trigger: string; stats: DocumentRow['extraction_summary'] }[]
  region_count: number
  records: ExtractionRecord[]
  evidence_links: { id: string; evidence_kind: string; page_number: number | null }[]
}

type EvidenceItem = {
  id: string
  subject_kind: string
  document_id: string
  page_number: number | null
  excerpt: string | null
  method: string | null
  confidence: number | null
  quote_verified: boolean
}

type DocumentProvenance = {
  record_count: number
  chain: { record: ExtractionRecord; region: { page_number: number | null; region_kind: string } | null }[]
}

test.describe('documents → ingestion → records → evidence', () => {
  test('shows the extraction the backend actually performed on the seeded documents', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const documents = await apiGet<DocumentPage>(request, `/documents?well_id=${wellId}`)
    expect(documents.total, 'the seeder must have produced documents').toBeGreaterThan(0)
    const seeded = documents.items[0]

    await page.goto(wellPath('/documents'))
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()

    // The document list shows each seeded document with the record count the API reports.
    for (const document of documents.items.slice(0, 3)) {
      const entry = page.getByRole('button').filter({ hasText: document.title })
      await expect(entry).toBeVisible()
      await expect(entry).toContainText(`${document.extraction_summary.records} records`)
    }

    // Opening a document shows the extractors that claimed it, which is how a reader knows whether
    // the numbers below were read by a table extractor or guessed from prose.
    await page.getByRole('button').filter({ hasText: seeded.title }).first().click()
    await expect(page.getByRole('heading', { name: seeded.title })).toBeVisible()
    const extractorNames = Object.keys(seeded.extraction_summary.extractors)
    expect(extractorNames.length, 'the seeded DDR must be claimed by extractors').toBeGreaterThan(0)
    await expect(page.getByText(new RegExp(`extractor set: ${extractorNames.join(', ')}`))).toBeVisible()

    const detail = await apiGet<DocumentDetail>(request, `/documents/${seeded.id}`)
    expect(detail.records.length).toBe(seeded.extraction_summary.records)

    // Every extracted row is listed with the method that produced it — never as a bare "record".
    for (const record of detail.records) {
      await expect(page.getByText(record.record_type.replace(/_/g, ' '), { exact: false }).first()).toBeVisible()
    }
    await expect(page.getByText(/method not recorded/)).toHaveCount(0)
    await expect(page.getByText('record', { exact: true })).toHaveCount(0)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('an extracted value can be traced back to its page and its evidence', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const documents = await apiGet<DocumentPage>(request, `/documents?well_id=${wellId}`)
    const target = documents.items.find((row) => row.extraction_summary.records > 0)
    expect(target, 'a document with extracted records is required for this journey').toBeTruthy()
    const documentId = target?.id as string

    const provenance = await apiGet<DocumentProvenance>(request, `/documents/${documentId}/provenance`)
    const evidence = await apiGet<{ items: EvidenceItem[]; total: number }>(
      request,
      `/evidence?document_id=${documentId}&limit=200`,
    )
    expect(provenance.record_count).toBeGreaterThan(0)
    expect(evidence.total).toBeGreaterThan(0)

    await page.goto(wellPath(`/documents?document=${documentId}`))
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()

    // Provenance tab: region → extraction → record, as the backend built it.
    await page.getByRole('tab', { name: /region/i }).click()
    const chainRecord = provenance.chain[0].record
    await expect(
      page.getByText(chainRecord.record_type.replace(/_/g, ' '), { exact: false }).first(),
    ).toBeVisible()

    // Evidence tab: the quotes the API returned, each traceable to its page.
    await page.getByRole('tab', { name: /evidence/i }).click()
    const quotes = evidence.items.filter((item) => (item.excerpt ?? '').length > 0).slice(0, 3)
    expect(quotes.length, 'the seeded extraction must keep excerpts').toBeGreaterThan(0)
    for (const item of quotes) {
      const firstLine = (item.excerpt as string).split('\n')[0].slice(0, 60)
      await expect(page.getByText(firstLine, { exact: false }).first()).toBeVisible()
    }

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })

  test('uploading a new report runs the real pipeline, and identical bytes are not ingested twice', async ({
    page,
    request,
    wellId,
    consoleErrors,
  }) => {
    const before = await apiGet<DocumentPage>(request, `/documents?well_id=${wellId}`)

    await page.goto(wellPath('/documents'))
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
    // 1. New content: a Well Drilling Program that the seeder did not ingest for this well. The
    // document type is chosen in the UI, exactly as an engineer would before uploading.
    await page.getByLabel(/document type/i).selectOption('well_program')
    await page.locator('input[type="file"]').setInputFiles(UPLOAD_PATH)
    await expect(page.getByText(/Uploaded /)).toBeVisible({ timeout: 30_000 })
    await expect
      .poll(async () => (await apiGet<DocumentPage>(request, `/documents?well_id=${wellId}`)).total, {
        timeout: 60_000,
        message: 'the uploaded document must appear through the API',
      })
      .toBeGreaterThan(before.total)

    const after = await apiGet<DocumentPage>(request, `/documents?well_id=${wellId}`)
    const newDocument = after.items.find((row) => !before.items.some((b) => b.id === row.id))
    expect(newDocument, 'the upload must have created exactly one new document').toBeTruthy()
    const newId = newDocument?.id as string
    const detail = await apiGet<DocumentDetail>(request, `/documents/${newId}`)
    expect(detail.ingestion_jobs[0]?.status).toBe('succeeded')

    // The UI reports the API's own numbers rather than a plausible-looking summary.
    await expect(page.getByRole('heading', { name: detail.document.title })).toBeVisible()
    await expect(page.getByText(`records ${detail.document.extraction_summary.records}`)).toBeVisible()
    await expect(page.getByText(`pages ${detail.document.extraction_summary.pages}`)).toBeVisible()

    if (detail.records.length === 0) {
      // A program is not a daily report: nothing extractable was found, and the product says so
      // instead of showing an empty table that looks like success.
      await expect(page.getByText('no records extracted')).toBeVisible()
      await expect(page.getByText(/No structured record was extracted/)).toBeVisible()
    } else {
      for (const record of detail.records.slice(0, 3)) {
        await expect(page.getByText(record.record_type.replace(/_/g, ' '), { exact: false }).first()).toBeVisible()
      }
    }

    // 2. Identical bytes: the pipeline reuses the existing document instead of creating a twin.
    // The input is cleared first — otherwise the browser sees the same selection again and fires no
    // change event, which would make this assertion pass without an upload ever happening.
    await page.locator('input[type="file"]').setInputFiles([])
    await page.locator('input[type="file"]').setInputFiles(UPLOAD_PATH)
    await expect(page.getByText(/Identical content was already ingested/)).toBeVisible({ timeout: 30_000 })
    const afterDuplicate = await apiGet<DocumentPage>(request, `/documents?well_id=${wellId}`)
    expect(afterDuplicate.total, 'a duplicate upload must not add a document').toBe(after.total)

    expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
  })
})

test("the upload assets are the backend's own synthetic fixtures, not hand-written copies", () => {
  const fixture = readFileSync(path.resolve(here, '..', '..', 'backend', 'tests', 'fixtures', 'synthetic_ddr.py'), 'utf8')
  const pythonBytes = (name: string): string => {
    const marker = `${name} = b"""`
    const body = fixture.slice(fixture.indexOf(marker) + marker.length)
    return body.slice(0, body.indexOf('"""'))
  }

  // The journey uploads these two files. They are the backend's own ingestion fixtures, so the
  // browser sends exactly the text the extractors are tested against.
  expect(readFileSync(UPLOAD_PATH, 'utf8')).toBe(pythonBytes('SYNTHETIC_PROGRAM_TEXT'))
  expect(readFileSync(UPLOAD_PATH, 'utf8')).toContain('SYNTHETIC TEST DATA')
  expect(readFileSync(path.join(here, 'assets', 'synthetic_ddr_upload.txt'), 'utf8')).toBe(
    pythonBytes('SYNTHETIC_DDR_TEXT'),
  )
})
