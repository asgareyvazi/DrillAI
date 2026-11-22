# Mission 4 — engine inventory and incomplete assurance review

This is a source/test inventory, **not a completed independent source-to-report audit**. The runtime list is regenerated from the catalogue; primary-test associations and limitations are self-declared. CP19 includes synthetic analytical vectors for the original 14 engines; those selected checks do not validate every formula or operating range. Proximity has separate synthetic segment vectors. No field/vendor/customer evidence is asserted. The two safety engines remain NEEDS_FIELD_VALIDATION.

| Engine / version | Source | Existing software evidence | Important limitation / status |
|---|---|---|---|
| `drilling.mse` 1.1.1 | `backend/src/drillai/engines/mechanics.py` | `backend/tests/engines/test_mechanics.py` | surface-measured values include friction losses and vibration effects; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `hydraulics.laminar` 1.6.0 | `backend/src/drillai/engines/fluids.py` | `backend/tests/engines/test_fluids.py` | turbulent flow is detected (Reynolds) but not modelled: results are optimistic in that regime; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `integrity.barrier_envelope` 1.1.0 | `backend/src/drillai/engines/integrity.py` | `backend/tests/engines/test_safety_engines.py` | does not size barriers against actual pressure loads (use the tubular/cement design engines); `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `offsets.similarity` 1.1.0 | `backend/src/drillai/engines/offsets.py` | `backend/tests/engines/test_planning_engines.py` | no learned similarity (geological or trajectory embedding) — factors are declared and inspectable; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `optimization.pareto` 1.1.0 | `backend/src/drillai/engines/optimization.py` | `backend/tests/engines/test_twin_optimization_engines.py` | no surrogate or gradient-based search; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `optimization.sweep` 1.1.0 | `backend/src/drillai/engines/optimization.py` | `backend/tests/engines/test_twin_optimization_engines.py` | no surrogate modelling; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `readiness.assessment` 1.1.0 | `backend/src/drillai/engines/readiness.py` | `backend/tests/engines/test_planning_engines.py` | no probabilistic lead-time modelling; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `schematic.well_model` 1.1.0 | `backend/src/drillai/engines/schematic.py` | `backend/tests/engines/test_planning_engines.py` | no deviated-schematic projection (a TVD view requires the trajectory); `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `torque_drag.soft_string` 1.1.0 | `backend/src/drillai/engines/mechanics.py` | `backend/tests/engines/test_mechanics.py` | no buckling (sinusoidal/helical) or lock-up analysis; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `trajectory.geometric_proximity` 1.0.0 | `backend/src/drillai/engines/proximity.py` | `backend/tests/engines/test_proximity.py` | GEOMETRIC_PROXIMITY_ONLY; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `trajectory.minimum_curvature` 1.3.0 | `backend/src/drillai/engines/trajectory.py` | `backend/tests/engines/test_trajectory.py` | does not model bending moments, toolface history or BHA sag; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `tubulars.api_5c3` 1.1.0 | `backend/src/drillai/engines/tubulars.py` | `backend/tests/engines/test_safety_engines.py` | pipe body only: no connection, coupling or thread rating; `NEEDS_FIELD_VALIDATION` |
| `twin.reconciliation` 1.2.0 | `backend/src/drillai/engines/twin.py` | `backend/tests/engines/test_twin_optimization_engines.py` | no positional uncertainty (ISCWSA) model; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |
| `well_control.kill_sheet` 1.2.0 | `backend/src/drillai/engines/wellcontrol.py` | `backend/tests/engines/test_safety_engines.py` | does not model multi-phase gas expansion, real-gas Z-factor compressibility, or oil-based mud gas solubility; `NEEDS_FIELD_VALIDATION` |
| `wellbore.capacity` 1.4.0 | `backend/src/drillai/engines/fluids.py` | `backend/tests/engines/test_fluids.py` | no eccentricity or washout; `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` |

Hydraulics additionally has independent Newtonian pipe/slot and orifice checks in `backend/tests/engines/test_mission4_hydraulics_safety.py`, and original CP19 checks. These are software reference checks, not validation of the full hydraulics model. The additional declared pressure terms are user assertions; no reference-flow extrapolation is allowed without the explicitly declared ranges, and no density extrapolation is allowed by the optimizer.
