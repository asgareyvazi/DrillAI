# CP150 — Isolated Two-Well Synthetic Browser Pilot

**Date:** 2026-10-20

**Status:** The dedicated local Chromium journey passed. It created one manual daily-report draft through the real browser per seeded synthetic well, then read both reports back from the product API and an independent read-only SQLite verifier. This is software-integration evidence only; it is not field data, engineering qualification, customer acceptance, or deployment certification.

**Evidence class:** One deterministic Playwright execution against the local Vite frontend, the real FastAPI application, an invocation-owned fresh SQLite database, and synthetic-only well/report values. No external provider, vendor endpoint, SMTP relay, or customer/field system was contacted.

## Isolation and fixture contract

- `scripts/seed_cp150_pilot.py` refuses production, symlinked/non-dedicated paths, a non-owned run marker, an existing database/blob directory, and any data directory containing files other than the two invocation markers. It creates a project, exactly two clearly named synthetic wells, and one wellbore per well through local product API routes. It performs no reset or reuse and creates no reports, measurements, surveys, or engineering outputs.
- `frontend/playwright.cp150.config.ts` reserves unique direct-child `frontend/.e2e-cp150-*` and `frontend/.e2e-cp150-results-*` paths with fresh per-invocation markers. It never falls back to `.e2e`, never reuses an old run ID, starts only its own API/Vite servers, and uses one Chromium worker with zero retries.
- `frontend/e2e/start-api.mjs` rejects an existing CP150 database or blob path before the seed, keeps the CP150-only seed separate from the demo and CP131 seeders, and starts the API under the fixture's synthetic tenant identity.
- The setup readback checks the one project, exactly two wells, each well's one expected wellbore, and no pre-existing daily reports before opening the browser.

## Browser journey and independent readback

The single journey, `creates and independently reads back one scoped synthetic daily-report draft per well`, verifies both names on the product wells page, then creates one draft per well through `DailyReportsWorkspace`. Each report contains only explicitly labelled synthetic content, one synthetic milestone activity with a reported duration, and one synthetic mud-weight measurement entered as `ppg`; the API preserves source value/unit and returns canonical value/unit separately. The browser's actual POST response and selected detail view are asserted.

The test then verifies through the real API that each report is visible only under its own well/wellbore, that creating the first report leaves the second well empty, and that fetching the second report through the first well's route returns a non-disclosing `404` without its private revision/content fields. Finally, `scripts/verify_daily_report_db_readback.py` opens the same database read-only (`mode=ro`, `PRAGMA query_only=ON`) for each report and checks its scoped report/revision, content digest, source and canonical unit values, activity duration and manual resolution state against the API result.

## Observed local result

- Dedicated CP150 Playwright JSON summary for the final wrapper invocation: **1 expected, 1 passed, 0 skipped, 0 unexpected, 0 flaky; retry 0**. The test took **8.169 seconds**; the runner invocation took **17.565 seconds**.
- Both seeded wells and wellbores were read back from the API before the journey; both browser-created reports were read back from the API and the scoped read-only SQLite verifier. The cross-well 404 assertion passed.
- Exact retained artifact run ID: `20261115103504080-70dc927f-a5a4-4a20-87c5-e5769480c8d1` (opaque runner-generated identifier; not a mission report date). The database/fixture artifacts remain in `frontend/.e2e-cp150-20261115103504080-70dc927f-a5a4-4a20-87c5-e5769480c8d1/`; the Playwright JSON report remains in `frontend/.e2e-cp150-results-20261115103504080-70dc927f-a5a4-4a20-87c5-e5769480c8d1/`. These are ignored, local-only generated artifacts and are not committed.
- A real headless Chromium browser was used. In this restricted Linux container, the run used the repository's declared `@sparticuz/chromium` development dependency and its bundled Linux libraries. No browser `--disable-web-security` flag was used. This local browser result is not a native-Windows run or exact-SHA CI certification.
- Earlier harness/test iterations did fail: the first config placed its output ownership marker where Playwright cleans output, an attempt lacked an installed browser, and an initial cross-well assertion incorrectly treated the caller-supplied report ID in a 404 message as leaked data. The path guard was moved outside Playwright's managed output folder, the supported repository browser-preparation wrapper was used, and the assertion now checks for private revision/content fields rather than the ID already present in the request. Those earlier failures remain failures; they are not counted as retries or erased by the final fresh invocation.
- Static validation for the new seed and E2E spec passed: targeted Ruff, Python compile, Node syntax check, Playwright collection (one dedicated CP150 test), and `tsconfig.e2e.json` TypeScript check. A deliberate seed attempt against an earlier CP150 run directory failed closed before app/database startup; the SQLite file's SHA-256 was identical before and after.
- The dedicated CP150 spec is excluded from the standard 128-test/20-file Playwright inventory and checked by its own config. The backend CP32/CP33 source-contract modules include this extra spec in their parameterized review, adding six actual collected backend cases; full collection is **1,780 tests across 138 files**. The reviewed manifest expects Linux 1,780/0 skips, default SQLite 1,765/15 PostgreSQL-only skips, and Windows 1,764/one identified POSIX-only skip/15 PostgreSQL deselections. These are collection arithmetic, not observed results for the current exact SHA; CP151 owns final acceptance and exact-SHA certification.

## Limits and disposition

The test intentionally writes only labelled synthetic records. It does not establish actual operational report quality, semantic retrieval, AI behavior, survey calculations, engineering fitness, vendor interoperability, external delivery, production security, field acceptance, or RPO/RTO. The cross-well route refusal exercises this product endpoint, not an independent penetration test. No full backend/frontend matrix, native-Windows execution, or current exact-final-SHA Actions run is claimed. CP151 remains responsible for the complete same-SHA matrix and Linux/native-Windows jobs; only explicitly failure-only uploads may be skipped on success.
