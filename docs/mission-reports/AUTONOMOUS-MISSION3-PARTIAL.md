# Mission 3 — partial implementation and acceptance reopening

## Baseline and preservation

Authorized branch `arena/9d8f5568-drillai`, starting remote `0735fc14fe9af35d89d0f26636044b3a458fdb0d`, main `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. The initial checkout was shallow at `bfea530602a23bcc08efca85a136240f61c44a0d`, with 457 modified/untracked entries and no upstream. After fetching full history, all 700 tracked remote files matched the working tree byte-for-byte and by executable/symlink mode, with no missing or different files. The index and branch ref were advanced to the descendant remote commit without changing working-tree files; no reset, clean, force-push or history rewrite was performed. Main was an ancestor, 286 commits behind the baseline. Stash list was empty; the historical stash object `7f13981f35eebc39fb463f84b487fbfa79d0f54e` was unavailable, not recovered.

The following pre-existing untracked evidence is retained (initial content hashes):

| File | Bytes | SHA-256 |
|---|---:|---|
| `frontend/.playwright-report-cp151-71aae4f7f3364cd8bef9f4748c4283fb/index.html` | 927479 | `0bb5b83130d9869fcc36f112ca431268e1c544f4fd94f6321c28d575c12ac00f` |
| `frontend/playwright-report-master-full-7d42a1/index.html` | 926911 | `08bc7c242cee30a0c8c17fb17caf8512dcc42d19aac7eeab2648e8d3fe1feddb` |
| `frontend/test-results-master-auth-c4a891de/.last-run.json` | 45 | `91d1c43004802cd49950d78eb11c8fa7d05da8ffffe219a8b13b2f561bc00903` |
| `frontend/test-results-master-full-7d42a1/.last-run.json` | 45 | `91d1c43004802cd49950d78eb11c8fa7d05da8ffffe219a8b13b2f561bc00903` |
| `frontend/test-results-master-full-7d42a1/e2e-results.json` | 169655 | `555a97d72d99dff75dc7d6eb551d34b5c534f40f68c44eb4924a43afa5216bf9` |

Initial ignored-root inventory was empty in this reconstructed sandbox. This mission's install/build/test runs create ignored `frontend/node_modules`, `frontend/dist`, TypeScript `.tsbuildinfo`, `.ruff_cache`, `.pytest_cache` and Python `__pycache__` directories; they are generated tooling artifacts, not evidence or a clean-tree claim. Temporary logs and environments are under `/tmp` and are not durable repository evidence.

## Mission 2 reopening matrix

| Requirement | Source finding | Evidence needed / status |
|---|---|---|
| Geometry validation | `fluids.py` requires outer coverage and complete string data, rejects overlapping inner tubulars and impossible nesting. | PARTIAL: added independent transition vectors; real designs, tool joints, eccentricity and more topologies not validated. |
| Explicit open bore | Baseline Boolean applied to every missing interval and had no scope/source. | PARTIAL: replaced by scoped human declarations and tests; claimed identity/source are not authenticated or independently verified, and UI entry/review is missing. |
| TVD and datum | Baseline required TVD but compared to MD without a reference contract. | PARTIAL: explicit matching origin, vertical datum, frame, survey/source declarations and positive-down sign required; no datum transformation, authenticated survey linkage or verified origin. |
| Full circulation path | Engine rejects missing geometry to bit, but uses nominal concentric dimensions and declared fluid properties. | PARTIAL: independent full model and transition testing; no field validation. |
| Capacity UI | Baseline had only generic engine runner. | PARTIAL: structured, wellbore-scoped page and saved runs; no design revision, review, comparison or engineering report; one synthetic real-browser persistence journey now passes. |
| Hydraulics UI | Baseline only generic runner/optimisation JSON. | PARTIAL: same page offers separate hydraulic fields and reference declarations; no validated survey linkage, profiles, design workflow, approval or report; one synthetic real-browser persistence journey now passes. |
| OCR | Baseline exact deployment run 44907414500 failed. | FAIL: no accepted improvement or authentic holdout. |
| Holdout | No independently adjudicated field holdout. | FAIL: no authorized data supplied. |
| 14-engine assurance | Registry table is self-declared, not independent per-engine assurance. | PARTIAL: existing CP19/CP142 synthetic software vectors; comprehensive 14-engine independent audit still open. |
| Workspace preservation | Four pre-existing directories and file hashes recorded. | PARTIAL until final after-inventory and remote/CI checks. |

Review of `0735fc1` confirmed selected job, region and record order matches the API and exact IDs plus all original truncation assertions remain; quarantine tests were not removed or altered. LF-normalized hash guard is a source drift check, not validation. Historical Windows `ERR_NO_BUFFER_SPACE`: `HISTORICAL_ROOT_CAUSE_NOT_PROVEN`.

## Mission 3 implementation boundaries

* `wellbore.capacity` 1.4.0: global `verified_open_bore` is rejected; caller must declare each empty MD interval with nonblank identity and basis plus timezone-aware timestamp. Matching declarations cannot overlap tubulars or each other and must cover every missing interval. This is a **caller assertion**, not proof of identity or geometry. Inputs and actor of the engine run are persisted by existing `EngineRun`; they do not constitute a reviewed design revision. Bottoms-up volume is available without a pump or flow; incompatible pump-derived and separately supplied flow rates are rejected. Full model and current-bit volume bases are distinct.
* `hydraulics.laminar` 1.5.0: explicit positive-down reference object requires named matching MD/TVD origins, vertical datum, coordinate frame, survey revision and source. No datum conversion or independent survey validation is performed. TVD must remain positive and no greater than MD in that common declared basis. There is no survey-derived ECD profile; annular slot approximation and turbulent warnings remain.
* `trajectory.geometric_proximity` 1.0.0: a bounded closest-point calculation over explicitly supplied absolute 3-D polylines, with matching declared frame/datum, ordered MD and revision references. Returns `GEOMETRIC_PROXIMITY_ONLY`, not collision probability, risk, ISCWSA compliance, separation factor or a curved trajectory calculation. Near-parallel numerically ill-conditioned segment pairs fail closed. No offset-well authorization/lookup, survey conversion, workspace, review or report is implemented.
* `/wells/:wellId/circulation` provides editable section rows, wellbore selection, capacity and laminar inputs, model/validity disclosures, persisted engine execution and read-only run history. It is intentionally not described as either completed mandatory workspace. It cannot yet author scoped open-bore declarations, save a design revision, compare cases, export an approved report or use a verified survey. A submitted TFA requires an explicitly entered discharge coefficient in this UI; pump entries require an explicit pump count.
* `EngineRegistry` validation errors now omit raw input and non-serializable Pydantic context, preserving type/location/message. Before the fix, mismatched datum/frame custom validator errors caused an HTTP serialization failure rather than the expected 422. New API assertions check 422 without weakening any oracle.
* The English, Persian and JSON capability inventories are source-linked **preliminary** inventories, not the requested exhaustive historical/current/planned product catalogue or independent assurance. The verifier checks consistency with runtime metadata and source hashes; registry statuses and references remain self-declared.

## Evidence and remaining blockers

Focused independent geometric reference vectors cover intersecting, parallel, skew and endpoint cases and mismatched references. Capacity vectors cover casing/liner/drillpipe/HWDP transitions, partial depth, pumps and declaration gaps. Component tests cover structured input and persisted-run call wiring. A new isolated Chromium journey entered both calculations, independently checked the capacity result, and retrieved the two saved runs through the API (1 passed); it is synthetic, not a design approval, report, field evidence, or a full matrix run. The reviewed browser inventory increased from 128 to 129 by adding this test, with all prior tests retained. Exact current CI must be checked after the final commit. Baseline exact run [44907414500](https://github.com/asgareyvazi/DrillAI/actions/runs/44907414500) passed Windows and 128 browser tests on each platform but failed deployed OCR: overall critical 308/384 and Persian 118/192, Persian CER 0.229478 and WER 0.293367. It does not certify this revision.

Complete capacity/hydraulics vertical slices, survey-linked anti-collision and uncertainty, independent 14-engine audit, OCR recovery and holdout, and the requested remaining DDR/operations/Well Design/programs/RAG/agents/twin/reporting/security/competitor work remain open. No field, customer, vendor or ISCWSA acceptance has been obtained. The two previously identified `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`.

Local checks on the worktree: 274 focused backend tests passed before the error-serialization fix; the latter received 7 targeted API/registry checks, and the final collection contains 1,859 tests across 143 files. Full frontend tests on the updated tree passed 415 in 42 files; typecheck, lint, and the 129-test browser shape verifier passed. The 64-case synthetic tesserocr emulator rerun failed: CER 0.107024, WER 0.154639, critical 311/384; Persian CER 0.227241, WER 0.296769, critical 121/192. It is not the deployed CLI or an independent holdout.

The full local SQLite suite finished with **1,844 passed and 15 PostgreSQL-only skips**; it started before the registry error-serialization fix, so the later targeted checks supplement it rather than making it an exact final-tree suite. The final frontend build also succeeded with a large-chunk warning. PostgreSQL, native Windows and deployment OCR for this change require CI evidence.
