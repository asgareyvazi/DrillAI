/** Read-only test-side SQLite readback for persisted synthetic DDR assertions. */

import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { fixtures } from './fixtures'

export interface DailyReportDatabaseSnapshot {
  report: {
    id: string
    well_id: string
    wellbore_id: string
    report_date: string
    report_number: string | null
    latest_revision_number: number
    latest_approved_revision_number: number | null
  }
  revision: {
    id: string
    revision_number: number
    edit_version: number
    status: string
    source_kind: string
    source_document_id: string | null
    source_record_id: string | null
    supersedes_revision_id: string | null
    summary_sha256: string
    content_hash: string | null
  }
  activity: {
    title_sha256: string
    operation_kind: string | null
    duration_seconds: number | null
    duration_source_value: number | null
    duration_source_unit: string | null
    source_document_id: string | null
    source_record_id: string | null
    resolution_state: string
  }
  measurement: {
    parameter_key: string
    source_value: number
    source_unit: string
    accepted_input_value: number
    accepted_input_unit: string
    value_si: number
    canonical_unit: string
    source_document_id: string | null
    source_record_id: string | null
    resolution_state: string
  }
  reconciliation: {
    total: number
    accepted: number
    source_documents: number
    source_records: number
  }
}

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')

function resolvePython(): string {
  const configured = process.env.DRILLAI_PYTHON
  if (configured && existsSync(configured)) return configured
  const backend = path.join(repoRoot, 'backend')
  const candidates = process.platform === 'win32'
    ? [path.join(backend, '.venv', 'Scripts', 'python.exe'), path.join(backend, '.venv', 'bin', 'python')]
    : [path.join(backend, '.venv', 'bin', 'python'), path.join(backend, '.venv', 'Scripts', 'python.exe')]
  return candidates.find(existsSync) ?? (process.platform === 'win32' ? 'python' : 'python3')
}

/** The helper opens SQLite with `mode=ro` and query-only enabled; it cannot mutate the E2E database. */
export function readDailyReportDatabaseSnapshot(
  reportId: string,
  wellId: string,
  wellboreId: string,
  revisionNumber?: number,
): DailyReportDatabaseSnapshot {
  const databaseUrl = fixtures().database_url
  const script = path.join(repoRoot, 'scripts', 'verify_daily_report_db_readback.py')
  const args = [script, databaseUrl, reportId, wellId, wellboreId]
  if (revisionNumber !== undefined) {
    if (!Number.isSafeInteger(revisionNumber) || revisionNumber < 1) {
      throw new Error('Read-only daily-report database readback failed for the isolated E2E fixture.')
    }
    args.push('--revision-number', String(revisionNumber))
  }
  try {
    const stdout = execFileSync(
      resolvePython(),
      args,
      { cwd: repoRoot, encoding: 'utf8', timeout: 15_000, maxBuffer: 256 * 1024, windowsHide: true },
    )
    return JSON.parse(stdout) as DailyReportDatabaseSnapshot
  } catch {
    // Child-process errors can contain command arguments or database paths. Keep test output scoped.
    throw new Error('Read-only daily-report database readback failed for the isolated E2E fixture.')
  }
}
