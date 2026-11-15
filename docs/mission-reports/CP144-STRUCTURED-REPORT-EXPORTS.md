# CP144 — End-to-End Structured Report Exports

**Date:** 2026-10-20

**Status:** Generic JSON/PDF/XLSX export implementation, focused backend checks and the full local frontend unit suite passed. The full backend suite, real-browser download, exact-SHA CI, and field/customer acceptance remain separate and are not established by these checks.

**Evidence class:** deterministic software checks against synthetic report/API fixtures. No provider, vendor, field or customer systems were contacted.

## Scope and behavior

The generic report endpoint is `GET /api/v1/wells/{well_id}/reports/{kind}/export`, with `format=json|pdf|xlsx` and `locale=en|fa`. It composes the already-scoped `ReportingService` payload and exports its existing facts, calculations, evidence, recommendations and assumptions as separate categories. It does not calculate or translate source values in the renderer.

- JSON remains a normalized, finite, size-bounded representation of the report contract.
- PDF is generated through ReportLab with bundled DejaVu Sans fonts, escaped text, Persian shaping/bidi handling, bounded section/item/row/value sizes and pagination-safe tables. The document is parsed and its Persian text/data are checked with pypdf.
- XLSX is generated through openpyxl with an overview and one worksheet per report kind. Persian sheets set `rightToLeft`; formula-like user strings are kept as text. Large identifiers and floats with more than Excel's 15 significant digits are kept as decimal text rather than silently rounded. Oversized cells fail closed rather than being truncated.
- The route authorizes `report.export`, builds only within the authenticated organization and well scope, writes a `report.export` audit record containing format, locale, byte length, SHA-256 and period bounds, and only then returns an attachment. Responses are `no-store`, `nosniff`, and carry the SHA-256 as the ETag. An audit-write failure returns no PDF/XLSX body.
- The frontend offers JSON/PDF/Excel downloads, sends the selected English/Persian locale, and uses the server Blob unchanged. It does not reformat report facts or perform calculations.

The renderer validates the computed report again at export time and is bounded by payload and row/cell limits. It performs no database reads or network requests. The separate immutable daily-report revision export path is unchanged; generic reports are rebuilt from currently scoped records at download time and are **not** persisted as immutable report revisions. The audit digest identifies each exact export byte stream, but two exports are not represented as one revision. Generic DOCX rendering remains unsupported.

## Verification observed

- Backend collection: **1,766 tests across 137 files**, confirmed by `pytest --collect-only` on 2026-10-20. CP144 added nine collected cases across two files (seven renderer cases, a cross-organization export test, and an audit-failure case). The PostgreSQL node-ID inventory is unchanged.
- Focused renderer/API/two-tenant selection: **27 passed in 35.38 seconds**. This includes actual PDF generation and pypdf parsing, XLSX generation and openpyxl readback, all five report categories, Persian PDF glyph/text and Persian XLSX RTL assertions, exact numeric/formula-cell behavior, tenant isolation, audit success/failure, permission refusal and attachment headers.
- Ruff passed for the renderer, report API/service and focused tests.
- A backend wheel built successfully and contains the PDF renderer, both bundled DejaVu font files and their license notice (`drillai_backend-0.1.0-py3-none-any.whl`, 1,683,726 bytes). This verifies package inclusion, not a deployment-image or production installation.
- Report workspace selection: **5 Vitest tests passed**. Tests cover exact Blob download, expected filename, selected Persian locale and format-specific request parameters.
- Full local Vitest suite: **405 passed across 40 files**. Frontend TypeScript typecheck, ESLint and production build passed; Vite emitted its existing >500 kB chunk-size advisory.
- The reviewed backend manifest now expects Linux 1,766 passed/0 skipped, default SQLite 1,751 passed/15 PostgreSQL-only skips, and Windows 1,750 passed/1 identified POSIX-only skip/15 PostgreSQL deselections. These are inventory expectations, **not** observed full CP144 backend-suite results.

## Limits and disposition

The focused API tests and renderer fixture are synthetic. PDF text extraction and worksheet direction/readback do not establish customer-approved layout, visual accessibility, legal/electronic signature acceptance, field-report fidelity or qualified engineering approval. The downloads neither deliver mail nor prove that a recipient received/opened an attachment. No vendor/provider requests, credentials, signatures, delivery receipts, field measurements or customer acceptance are claimed.

This phase does not change the CP142 report-date follow-up; it remains unresolved pending verification. Keep `well_control.kill_sheet` and `tubulars.api_5c3` at `NEEDS_FIELD_VALIDATION`. No autonomous rig control or operational-readiness claim is introduced. Full backend/frontend, Linux/native-Windows, real-browser and final-SHA assurance remains CP151 work.
