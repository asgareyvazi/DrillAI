# CP138 — Daily-Report Tenant and Wellbore Scope Isolation

**Status:** repository software fix and focused verification complete. This is local synthetic API/domain evidence, not field/customer acceptance or exact-SHA cross-platform certification.

## Finding and implementation

A daily-report list could accept a `wellbore_id` belonging to a different well in the same organization and silently return an empty list. The query predicates did prevent data leakage, but the input scope disagreement was not rejected. That behavior was ambiguous for operators: an invalid scope looked like a valid scope with no reports.

`DailyReportService.list_reports` now resolves any supplied wellbore against the path well before issuing the report query, using the shared organization-scoped `DocumentScopeResolver`. A wellbore from another well is rejected as a validation error (HTTP 422); missing/cross-organization scope remains non-disclosing. Existing valid wellbore-filter behavior and pagination are unchanged.

The regression extends the existing two-tenant daily-report API test without increasing the maintained test inventory. It verifies:

- Reports on two wellbores of the same well can share the same date/shift identity, but their wellbore-filtered lists contain only their matching report.
- A report id used under another same-tenant well cannot be read via the current, history, revision or activity-metrics paths; attempted submit and correction writes are refused, and the original report remains an unchanged draft at revision 1.
- Creating or listing with a wellbore from a different well is rejected rather than silently accepted as an empty result.
- A source document from another wellbore cannot be attached to the report, even within the same organization.
- A second tenant can create and list its own report while neither tenant's list exposes the other's report id; the existing foreign-tenant read/write and foreign/quarantined-source checks continue to run.

The existing API test that had asserted an empty result for a foreign wellbore filter was updated to the explicit 422 contract. It now asserts the resolver's scope-mismatch message. This is a deliberate fail-closed behavior correction, not a skip or relaxed assertion.

## Verification

- Daily-report API, tenant, domain, and scoped DB-readback selection: **30 passed, 1 existing PostgreSQL-marked skip**. The skip is reported as skipped and was not changed.
- Backend Ruff for the changed service/tests and `git diff --check`: passed.
- No schema, test inventory, retry behavior, role permission, or unrelated report state was changed.

## Evidence boundary

The two organizations, wells, wellbores, reports and source documents are generated fixture records. The test exercises the real API and database-backed scope resolver, but it is not a production authorization review, independent penetration test, external evidence validation, field report, or customer acceptance. CP151 still requires fresh exact-final-SHA Linux and native-Windows certification.
