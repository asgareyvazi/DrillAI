# Mission 14 engineering capability report — interim

**NOT_ACCEPTED.** This index records inherited evidence, not field qualification or comprehensive source-to-report assurance. [Mission report](MISSION14-REPORT-EN.md); [ledger](MISSION14-PHASE-LEDGER.json); [candidate non-registry inventory](MISSION14-NONREGISTRY-CALCULATION-INVENTORY.json).

## Registered engines

| Engine | Source | Evidence and limitations |
|---|---|---|
| `drilling.mse` | `backend/src/drillai/engines/mechanics.py` | Teale axial and rotary work/volume; full model and integration assurance not established. |
| `hydraulics.laminar` | `backend/src/drillai/engines/fluids.py` | Hagen–Poiseuille Newtonian limit; full model and integration assurance not established. |
| `hydraulics.power_law_pipe` | `backend/src/drillai/engines/power_law.py` | independent numerical integration of radial shear profile at n=0.55 and 1.25; full model and integration assurance not established. |
| `integrity.barrier_envelope` | `backend/src/drillai/engines/integrity.py` | No independent vector established; full model and integration assurance not established. |
| `offsets.similarity` | `backend/src/drillai/engines/offsets.py` | No independent vector established; full model and integration assurance not established. |
| `optimization.pareto` | `backend/src/drillai/engines/optimization.py` | No independent vector established; full model and integration assurance not established. |
| `optimization.sweep` | `backend/src/drillai/engines/optimization.py` | No independent vector established; full model and integration assurance not established. |
| `readiness.assessment` | `backend/src/drillai/engines/readiness.py` | No independent vector established; full model and integration assurance not established. |
| `schematic.well_model` | `backend/src/drillai/engines/schematic.py` | No independent vector established; full model and integration assurance not established. |
| `torque_drag.soft_string` | `backend/src/drillai/engines/mechanics.py` | No independent vector established; full model and integration assurance not established. |
| `trajectory.geometric_proximity` | `backend/src/drillai/engines/proximity.py` | No independent vector established; full model and integration assurance not established. |
| `trajectory.minimum_curvature` | `backend/src/drillai/engines/trajectory.py` | great-circle tangent integration; full model and integration assurance not established. |
| `tubulars.api_5c3` | `backend/src/drillai/engines/tubulars.py` | No independent vector established; full model and integration assurance not established. |
| `twin.reconciliation` | `backend/src/drillai/engines/twin.py` | No independent vector established; full model and integration assurance not established. |
| `well_control.kill_sheet` | `backend/src/drillai/engines/wellcontrol.py` | synthetic hydrostatic balance and pressure schedule endpoints; full model and integration assurance not established. |
| `wellbore.capacity` | `backend/src/drillai/engines/fluids.py` | No independent vector established; full model and integration assurance not established. |

## Other capabilities

DDR, NPT, OCR, chronology, trajectory, anti-collision, capacity/hydraulics, Well Design, programs, Advisor/RAG, telemetry/twin and reports require further product and assurance work. The 18-area [history](MISSION14-HISTORICAL-CAPABILITY-MATRIX.json) retains unknown first functional implementations. The [roadmap](MISSION14-CAPABILITY-ROADMAP.json) retains 22 planned items with no claimed implementation or selected reference. The expanded two-well scenario is not implemented.

All synthetic vectors are software checks only. No authorized field evidence is available.
