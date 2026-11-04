# Operational dashboard metric contracts

**Status:** software-defined read models; not field, operational, or production acceptance.

This document defines the numbers shown by the well cockpit and its KPI/NPT endpoints. Every summary is a tenant-scoped read over recorded rows; a missing value remains missing, and no endpoint claims a global multi-well KPI unless it names that population and denominator explicitly.

## Well KPI snapshot

`GET /api/v1/wells/{well_id}/kpis` requires `well.read` plus `context.read` for the requested well and verifies that the well belongs to the authenticated organization. A foreign or unknown well returns not found.

The response includes a metric contract and one row per canonical KPI key. These rows are **current snapshots**, not period averages or forecasts:

- **Population:** the one requested well in the caller's tenant.
- **Window / aggregation:** the latest stored time-series point per channel, ordered by its source timestamp; an eligible, scope-matched extracted `parameter_set` can fill a channel only when no telemetry value is present. Telemetry wins when both exist.
- **Numerator / denominator:** not applicable for a raw latest-value measurement; both are `null`. The UI must not style the measurement as a ratio or a derived KPI.
- **Missing policy:** no imputation or default. An absent channel returns `value: null`, `present: false`, and a reason; a registered silent channel is distinguished from a channel with no eligible source.
- **Freshness:** telemetry freshness and age are computed against configured thresholds. Extracted records have `freshness: unknown`; ingestion/record creation time is never substituted for observation time.
- **Temporal and source lineage:** each present row carries `source`, `source_id`, `evidence_ref` where available, `observed_at`, `observed_at_basis`, `received_at`, quality, age and note. A report-header anchor remains labeled as such; an unknown event/observation time remains null.
- **Snapshot time:** `generated_at` is the API computation time; `as_of` is the newest time-series source timestamp only. A document/header/record date does not advance telemetry `as_of`, and neither time means ingestion time.

The cockpit keeps the unit attached to the recorded channel. Unit conversion is presentation only; it does not change the source value or its evidence.

## NPT summary

`GET /api/v1/wells/{well_id}/npt` verifies tenant ownership and returns two independent NPT sources. They are never added together:

- `basis=events`: numerator is the sum of matching event `npt_hours`, falling back to the recorded event duration only when the dedicated NPT-hours value is absent.
- `basis=operations`: numerator is the sum of matching operation `npt_hours` roll-ups.
- `event_count` is the count for the selected source, not the number of rows in the bounded evidence page.
- `productive_hours` is the sum of `actual_duration_hours` for recorded `operation_class=actual`, `is_productive=true` operations in the same well, section, operation, and explicit time window.
- `denominator_hours = productive_hours + selected numerator`; `percent_of_well_time = 100 * numerator / denominator_hours`, and is `null` when the denominator is zero. This is a scoped fraction of recorded productive-plus-selected-NPT hours, not a claim about elapsed calendar time.
- `since` and `until` must be timezone-aware and `since < until`. Only basis-bearing intervals can match a window; a lifecycle closure timestamp, ingest time, or lone report period end is not an event boundary. Rows are selected when their recorded interval overlaps the window, and their full recorded hours are summed without proration; precise partial-window allocations require source interval detail.
- The case list is bounded (`case_limit`/`cases_truncated`) while totals and breakdowns cover the full matching population. Case rows retain their source IDs, event-time basis, duration basis, unit/category evidence and uncertainty.
- `metric_contract`, `scope`, and `generated_at` travel with the values. No recorded NPT row is not proof that no physical downtime occurred; a zero remains a zero recorded total, not an inferred operating fact.

## Other cockpit summaries

- **Progress:** measured depth comes from `well_sections.actual_bottom_md_si`; planned total depth remains a separate input. Percent planned depth is a ratio of those explicit fields, and is absent unless its required inputs exist. General report depth is not a substitute.
- **Operation window:** “current” is based on recorded actual intervals and may be ambiguous; “next” is labeled planned, inferred, or unknown. Unknown interval ends remain unknown.
- **Risks, alerts and documents:** counts are scoped to the requested tenant and well and describe stored rows. Missing/stale evidence is surfaced separately; these counts do not imply severity incidence rates.
- **Trend and predictive claims:** the live monitor's trend is descriptive from bounded stored samples. It is not a forecast or a causal conclusion.

## Validation evidence and limits

Automated coverage verifies tenant denial, source IDs, freshness, no-value reasons, extraction-time provenance, scoped NPT arithmetic, window bounds and bounded evidence behavior. These are deterministic software checks against synthetic/local rows. They do not validate actual rig records, business definitions agreed by an operator, or production freshness thresholds. Operators must approve the reporting convention and unit policy for a deployment before using these values for operational or commercial decisions.
