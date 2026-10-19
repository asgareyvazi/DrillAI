# CP23 — Well-Control & Engineering Correctness Audit Report

**Repository**: `asgareyvazi/DrillAI`  
**Branch**: `arena/01a0dca0-drillai`  
**Phase**: CP23 (Well-Control Method Contract, Kick-Tolerance Semantics & Engineering Safety Boundaries)  
**Date**: 2026-10-19  

---

## 1. Executive Summary

CP23 audited the safety-critical and physics-governing engines in `backend/src/drillai/engines/` (`wellcontrol.py`, `fluids.py`, `tubulars.py`, `mechanics.py`, `service.py`) and the frontend Engineering Workspace (`frontend/src/pages/engineering/EngineeringWorkspace.tsx`), remediating every semantic ambiguity between declared engineering methods and computed outputs.

| Sub-Phase | Scope | Status | Verification |
|---|---|---|---|
| **CP23-A** | Explicit `wait_and_weight` (single-circulation Engineer's Method) vs `drillers_method` (two-circulation Driller's Method) kill-sheet contract | **PASS** | `test_cp23_wait_and_weight_vs_drillers_method_schedule` |
| **CP23-B** | Separation of static shoe `mud_weight_margin_si` (`kg/m3`) from preliminary single-bubble static `volumetric_kick_tolerance_si` (`m3`) | **PASS** | `test_cp23_mud_weight_margin_vs_volumetric_kick_tolerance` |
| **CP23-C** | Kill-rate pressure (`KRP`) friction assumption disclosure, input provenance verification, `hydraulics.laminar` `regime_validity`, `tubulars.api_5c3` pipe-body scope flags, and UI unit inference | **PASS** | `test_cp23_kill_sheet_input_provenance_and_friction_assumptions`, `test_cp23_hydraulics_turbulent_regime_validity_and_tubulars_scope`, `test_cp23_output_unit_inference_consistency` |
| **CP23-D** | Independent reference-backed validation suite (`backend/tests/engines/test_cp23_wellcontrol_and_engineering_audit.py`) | **PASS** | `145 passed` across `backend/tests/engines/` |

---

## 2. CP23-A — Kill-Sheet Method Contract (`wait_and_weight` vs `drillers_method`)

### 2.1 Finding Prior to CP23
Prior to CP23, `backend/src/drillai/engines/wellcontrol.py` declared `parameters={"method": {"value": "drillers_method"}}` and `"Driller's method pressure schedule"` in `notes`, while its `compute_kill_sheet` implementation computed a single-circulation linear pressure step-down from `ICP = KRP + SIDPP` to `FCP = KRP * (KMW / OMW)` over `0 -> strokes_to_bit` — which is the **Wait-and-Weight (Engineer's) Method**, not the two-circulation **Driller's Method**.

### 2.2 Remediation
`KillSheetInput` and `KillSheetOutput` now expose an explicit `method: Literal["wait_and_weight", "drillers_method"] = "wait_and_weight"` and `circulation_cycles: int`:
1. **`method="wait_and_weight"` (Engineer's Method, `circulation_cycles=1`)**:
   - Steps drillpipe pressure linearly from `ICP = KRP + SIDPP` down to `FCP = KRP * (KMW / OMW)` over `0 -> strokes_to_bit` as kill mud (`KMW`) fills the drillstring (`phase="initial"` -> `"pump_kill_mud"` -> `"final"`).
   - When `annulus_strokes > 0` is supplied, appends `phase="displace_annulus_at_fcp"` at `strokes = strokes_to_bit + annulus_strokes` holding `pressure_si = FCP` with `mud_weight_si = KMW`.
2. **`method="drillers_method"` (Driller's Method, `circulation_cycles=2`)**:
   - **Circulation 1**: Circulates the influx out of the well using original mud (`current_mud_weight_si`) while holding drillpipe pressure constant at `ICP = KRP + SIDPP` across `0 -> strokes_to_bit -> strokes_to_bit + annulus_strokes` (`phase="circulation_1_initial"`, `"circulation_1_hold_icp_to_bit"`, `"circulation_1_complete_influx_out"`).
   - **Circulation 2**: Displaces the drillstring with kill mud (`kill_mud_weight_si`), stepping drillpipe pressure linearly from `ICP` down to `FCP` over `c1_end -> c1_end + strokes_to_bit` (`phase="circulation_2_pump_kill_mud"` -> `"circulation_2_kill_mud_at_bit"`), and then holding `FCP` up the annulus (`phase="circulation_2_displace_annulus_at_fcp"`).

---

## 3. CP23-B — Static Mud-Weight Margin vs Volumetric Kick Tolerance

- **`mud_weight_margin_si` (`kg/m3`)**: Primary output field for the static casing-shoe mud-weight margin (`shoe_fracture_mud_weight_si - current_mud_weight_si`). `kick_tolerance_mud_weight_si` is retained solely as a backward-compatible alias and is explicitly documented in `notes` and `limitations` as a static mud-weight margin, not a volumetric kick tolerance.
- **`volumetric_kick_tolerance_si` (`m3`) & `max_allowable_influx_height_at_shoe_si` (`m`)**: Computed when `casing_shoe_tvd_si`, `shoe_fracture_mud_weight_si`, `influx_density_si`, and `annulus_capacity_at_shoe_si` (`m2`) are provided, using a single-bubble unmigrated hydrostatic balance at the casing shoe (`volumetric_kick_tolerance_basis="preliminary_single_bubble_unmigrated_at_shoe"`). When influx density or annular geometry is absent, `volumetric_kick_tolerance_si` is `None` and `volumetric_kick_tolerance_basis="not_computed_requires_influx_density_and_annulus_capacity"`. Multi-phase real-gas (`Z`-factor) kick tolerance remains explicitly outside scope.

---

## 4. CP23-C — Friction Assumptions, Input Provenance & Engineering Safety Boundaries

1. **Kill-Rate Pressure (`KRP`) Friction Assumption & Input Provenance (`well_control.kill_sheet`)**:
   - `KillSheetOutput.friction_assumption` explicitly records `"drillstring_dominated_krp_annular_friction_neglected"`.
   - `KillSheetInput.input_provenance` accepts source tags per critical input (`"recorded_scr_test"`, `"lot_fit_test"`, `"estimated"`, `"manual_override"`). `KillSheetOutput.input_provenance_verified` is `True` only when verified provenance is supplied without `"estimated"` / `"manual_override"` overrides, and emits an explicit warning whenever estimated or manual-override provenance is present.
2. **Laminar vs Turbulent Validity Boundary (`hydraulics.laminar`)**:
   - `HydraulicsEngineOutput.regime_validity` is `"laminar_valid"` when all intervals satisfy `Re <= 2100`, and transitions to `"non_certified_turbulent_exceedance"` (alongside the `laminar_assumption` warning violation) whenever any interval exceeds `CRITICAL_REYNOLDS = 2100`.
3. **Pipe-Body vs Connection Scope Boundary (`tubulars.api_5c3`)**:
   - `TubularDesignOutput.scope_classification` is `"pipe_body_only_connections_not_rated"` and `connection_verification_required` is `True`, ensuring consumers cannot mistake API TR 5C3 pipe-body ratings for connection/coupling, sour-service (`H2S`), or bending/buckling qualification.
4. **Frontend Output Unit Inference (`frontend/src/pages/engineering/EngineeringWorkspace.tsx`)**:
   - Fixed `OutputTable` in `EngineeringWorkspace.tsx`, which previously labelled every `*_si` scalar output as `'Pa'`. Added `inferOutputUnit(key)` aligned with `backend/src/drillai/engines/service.py:_infer_si_unit_for_field_name` so `mud_weight`/`ecd`/`density` render as `kg/m3`, volumes/kick tolerance as `m3`, rates as `m3/s`, forces as `N`, torques as `N.m`, times as `s`, and depths/lengths as `m`.
