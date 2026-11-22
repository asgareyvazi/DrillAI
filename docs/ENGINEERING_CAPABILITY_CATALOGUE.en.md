# Engineering capability inventory — preliminary

This source-linked inventory is not the requested comprehensive historical/product catalogue or an independent engineering audit. No field, vendor or customer acceptance is implied.

| Engine | Version | Status | Source |
|---|---|---|---|
| `drilling.mse` | 1.1.1 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/mechanics.py` |
| `hydraulics.laminar` | 1.6.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/fluids.py` |
| `hydraulics.power_law_pipe` | 1.0.0 | `INDEPENDENT_SOFTWARE_REFERENCE_CHECKED` | `backend/src/drillai/engines/power_law.py` |
| `integrity.barrier_envelope` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/integrity.py` |
| `offsets.similarity` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/offsets.py` |
| `optimization.pareto` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/optimization.py` |
| `optimization.sweep` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/optimization.py` |
| `readiness.assessment` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/readiness.py` |
| `schematic.well_model` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/schematic.py` |
| `torque_drag.soft_string` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/mechanics.py` |
| `trajectory.geometric_proximity` | 1.0.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/proximity.py` |
| `trajectory.minimum_curvature` | 1.3.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/trajectory.py` |
| `tubulars.api_5c3` | 1.1.0 | `NEEDS_FIELD_VALIDATION` | `backend/src/drillai/engines/tubulars.py` |
| `twin.reconciliation` | 1.2.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/twin.py` |
| `well_control.kill_sheet` | 1.2.0 | `NEEDS_FIELD_VALIDATION` | `backend/src/drillai/engines/wellcontrol.py` |
| `wellbore.capacity` | 1.4.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/fluids.py` |

## Historical milestones (selected, not exhaustive)
- `bfa066b` — Initial repository commit; not current product capability evidence.
- `09bef03` — Early engine pack, including trajectory and fluid calculations.
- `80aee9f` — CP19 analytical software vectors; not field validation.
- `658c4f0` — Survey datum and station-time provenance.
- `d7526a1` — Governed engineering products and OCR evidence.
- `11e6cd2` — OCR TSV quote and synthetic renderer correction; OCR acceptance remained open.
- `85396bc` — Hydraulics bracketing and bottoms-up geometry safeguards.
- `e212fa1` — Capacity full-path validation and explicit TVD; incomplete design workflows.
- `0735fc1` — Evidence pagination test setup matches API ordering.

## Product workflow status
- Capacity/circulation: PARTIAL — structured input and persisted runs; no approved revisions, comparisons or reports.
- Hydraulics/ECD: PARTIAL — structured input and declared references; no verified survey linkage or ECD depth profile.
- Anti-collision: PARTIAL — `GEOMETRIC_PROXIMITY_ONLY`; no uncertainty model, integrated survey selection or dedicated workflow.
- Persian OCR: NOT ACCEPTED on the unchanged deployed gate; independent holdout absent.

## Future / incomplete advanced scopes
turbulent hydraulics, Power Law full-well and annular hydraulics, Herschel-Bulkley rheology, hole cleaning, cuttings transport, uncertainty-based anti-collision, advanced torque and drag, surge/swab, casing design, tubular connection capacity, cementing, wellbore stability, kick tolerance, MAASP, managed pressure drilling, drillstring design, casing-seat planning, pressure management, well integrity, well economics, lifecycle performance, advanced optimization.

See the JSON catalogue for input/output schemas, units, assumptions, limitations and self-declared references. The source SHA normalizes CRLF to LF.
