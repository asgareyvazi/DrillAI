# CP152 — Run 438110 evidence reconciliation and recovery baseline

**Phase status:** `DONE` for evidence reconciliation and repository-state recovery. The tested candidate itself **failed**; this report does not certify it.

## Repository identity and workspace

- Authorized branch: `arena/9d8f5568-drillai`.
- The local branch initially pointed at `bfea530602a23bcc08efca85a136240f61c44a0d`, while the authoritative remote branch was `3a556c441b086666bfadc1651a6afb7d953f1dcc`. The current branch was an ancestor of the remote SHA. Before moving the local branch pointer, a content comparison found that all 404 modified/untracked paths represented in the remote target tree had identical Git blob content; the sole extra path was the existing CP151 Playwright report. A non-hard mixed index/branch reconciliation moved only the authorized local branch and index to the already-present remote commit; it did not overwrite worktree content.
- Reconciled local HEAD, local remote-tracking ref, and `git ls-remote` all equal `3a556c441b086666bfadc1651a6afb7d953f1dcc`. HEAD message: `ci(cp151): certify full matrix and isolate browser pilots`; parent: `89e47f67adcc41e8d559a3e07a61365664c2d185` (`feat(cp135): route RTL OCR and retain v7 benchmark evidence`), whose parent is `481ce4f6527bea73c5f62070e2045db89a6a2c4e` (`docs(cp145): clarify stash audit and report dates`).
- Workspace after reconciliation: no staged or modified tracked files. One untracked existing report is preserved: `frontend/.playwright-report-cp151-71aae4f7f3364cd8bef9f4748c4283fb/index.html`. `git status --ignored --short` showed no ignored entries; no `.e2e*`, SQLite database, or blob-store paths were present in this checkout. No absent artifact was recreated or deleted.
- Current stash state: `git stash list` is empty. Historical object `7f13981f35eebc39fb463f84b487fbfa79d0f54e` is unavailable to `git cat-file`; `git fsck --no-reflogs --unreachable` produced no recoverable objects. This is a forward-only audit; no historical stash was applied, popped, reconstructed, or claimed recovered.
- `main` remained `bfa066b28e0071880cb9191a4f1d47fdaa143e04` locally and remotely. GitHub branch metadata returned `protected: false`; the detailed protection endpoint returned HTTP 403, so no additional protection configuration is claimed.

## Exact-SHA Actions result

GitHub API metadata reports exact-SHA run [43811089178](https://github.com/asgareyvazi/DrillAI/actions/runs/43811089178), workflow `CI`, event `push`, branch `arena/9d8f5568-drillai`, head `3a556c441b086666bfadc1651a6afb7d953f1dcc`, overall conclusion **`failure`**. Both jobs checked out that exact SHA and concluded `failure`:

| Platform job | Job ID | Conclusion | Key results |
|---|---:|---|---|
| Linux — `install, verify, and run the real end-to-end suite` | `133112693448` | `failure` | Backend inventory, lint, migration, stack, standard Playwright, and CP150 pilot steps succeeded. Docker Compose deployment smoke failed at its OCR-quality sub-gate. |
| Native Windows — `native Windows PowerShell install, backend, stack, and Playwright E2E certification` | `133112693587` | `failure` | Frontend/tooling, supply-chain, lint, migration, inventory, and Playwright steps succeeded. Backend SQLite suite failed; dependent stack and vendor-harness steps were skipped. |

### Linux observed outcomes

- Backend: **1,780 passed, zero skipped**; inventory verifier, lint, fresh migration/drift check, and multi-process stack verification passed.
- Stack: **16/16 scenarios** and the **32-cycle bounded soak** passed. This is local software/harness evidence, not production RPO/RTO or vendor acceptance.
- Standard Playwright: **128 passed across 20 spec files**, zero skipped, unexpected, flaky, or retried tests.
- CP150 dedicated isolated browser/API/database pilot: **1 expected, 1 passed, 0 skipped, 0 retries**. It remains the narrower synthetic manual-DDR flow described in the CP150 report; it is not the CP165 integrated engineering pilot.
- Docker Compose smoke: **failed**. Compose configuration, startup, and readiness had progressed far enough for the OCR command to execute against the backend deployment image. The OCR report failed its unchanged quality thresholds. This is not evidence of a PostgreSQL startup or general Compose boot failure.
- The step combined readiness, OCR, HTTP/security/header and connector-profile probes. The OCR branch exited the shell before the later response-header checks, connector profile/viewer checks, and final `docker compose ps`; those probes therefore did **not** execute. The exit trap attempted Compose logs and cleanup. The failed OCR quality gate must not be described as a passed deployment smoke.

### Native-Windows observed outcomes

- Backend inventory: **1,763 passed, 1 failed, 1 identified POSIX-only skip, 15 PostgreSQL deselections; 1,780 collected**, with **2 warnings**.
- Exact failed test: `tests/operations/test_ddr_idempotency.py::test_report_period_is_not_promoted_as_a_survey_station_measurement_time`, assertion at line 1170. Expected `report["counts"]["trajectory_stations"] == 2`; actual was `0`.
- The preserved test scenario has two survey CSV rows: the first has `2026-03-15T12:00:00+02:00` and must normalize to `2026-03-15T10:00:00Z`; the second has no station timestamp and must remain `measured_at=None` with unknown timestamp provenance. The explicit DDR period start `2026-03-10T00:00:00+00:00` must not become either station timestamp. The regression's root cause was **not yet established at CP152**; CP153 owns full-path tracing and repair. No test expectation was changed.
- Standard Playwright: **128 passed across 20 files**, zero skips, unexpected, flaky, or retries. Windows stack and vendor-harness steps were skipped after the backend failure. There is **no Windows CP150 pilot result** for this run.

## Deployment-image OCR measurement

The deployment-image measurement below is the run evidence supplied with this mission order. GitHub API metadata confirms that its report artifact exists; the artifact bytes could not be downloaded in this environment, so the report JSON itself was not independently opened here.

- Corpus: synthetic CP135 DDR v3, **64 cases** (32 English / 32 Persian; four layouts; four scan conditions), dataset SHA-256 `31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`.
- Deployment runtime: Python **3.11.18**; Tesseract **5.5.0**; PDFium **5.15.2**; configured and available languages `eng` and `fas`; adapter `tesseract-cli-tsv-v7`. All **64/64** cases completed. This was a measured quality failure, not an incomplete-run result.

| Metric | Deployment image | Unchanged requirement | Result |
|---|---:|---:|---|
| Global CER | **0.3368180333** | ≤ 0.15 | fail |
| Global WER | **0.4652061856** | ≤ 0.20 | fail |
| Critical-field exact | **0.4973958333** | ≥ 0.90 | fail |
| English critical-field exact | **0.994792** | ≥ 0.90 | pass |
| Persian CER | **0.719340** | ≤ 0.15 | fail |
| Persian WER | **0.913265** | ≤ 0.20 | fail |
| Persian critical-field exact | **0/192 = 0.000000** | ≥ 0.90 | fail |

The reported failure is overwhelmingly Persian recognition, despite `fas` being configured and available. It cannot be fixed by merely naming the language. The prior local adapter-v7 result is a different environment: Python **3.11.2**, Tesseract **5.5.2-23-g397887**, local `tessdata_fast` commit `87416418657359cb625c412a48b6e1d6d41c29bd`; it measured global CER **0.1161058478**, WER **0.1649484536**, and critical exact **0.7473958333** (Persian **0.5052083333**). It also failed acceptance and is not a deployment-image substitute. CP154 must identify and pin the runtime/model data and preserve all thresholds and strata.

## Artifacts, logs, and retrieval limits

GitHub Actions API metadata lists the following non-expired artifacts; sizes and SHA-256 digests are from the metadata response:

| Artifact | Size | SHA-256 |
|---|---:|---|
| `cp135-synthetic-ocr-report-43811089178` | 7,480 bytes | `a64b6808250e77967cb1da4ded924b329c0a982e53b3c2adaf0730f9b947ae67` |
| `linux-backend-tests-43811089178-1` | 279 bytes | `3116af7a8c395bbca54aa4a120c4c0d9ec138c4d0fbaffda3191b8c6239d05d8` |
| `windows-backend-tests-43811089178-1` | 1,513 bytes | `41eea0dcbd878fc6c4894ab72ef24d5addb544f797e6237044131bb81eaca9cf` |
| `playwright-evidence-43811089178` | 530,353 bytes | `70850c9e9a454cedf58e68511fbe799ebf3304157b255f6033de7c5eee659a9e` |
| `windows-playwright-evidence-43811089178` | 531,241 bytes | `9e92541f65b4a0e2ba2ea1dfd75a5f6504eb70a419cad7067d21e66ae7f44fd7` |

`gh run view --log --job` failed with `EOF` at GitHub's Actions results receiver. `gh run download` failed with `EOF` at the signed `productionresultssa8.blob.core.windows.net` artifact host. Thus artifact names, sizes, digests, job/step conclusions and the mission-supplied failure evidence are available, but the log/artifact contents could not be inspected in this sandbox. No artifact-content claim is inferred from its presence or digest.

## CP145 provenance and overall boundary

The historical stash remains unavailable as recorded above. The reported snapshot date `2026-11-15`, the CP145 report's phase date `2026-10-20`, the commit timestamps, and any actual review date are distinct provenance fields. The stash snapshot is not present for independent date verification; do not derive an audit date from it or rewrite the unavailable stash history.

Run 43811089178 is a failed exact-SHA run, not a release certification. It proves the Linux OCR gate and Windows backend regression occurred, while Linux and Windows standard E2E passed and the Linux CP150 pilot passed. It does not establish a green CI matrix, Windows CP150, field OCR accuracy, field validation, customer/vendor acceptance, qualified engineering results, notification delivery, production recovery objectives, or operational readiness. Keep `well_control.kill_sheet` and `tubulars.api_5c3` at `NEEDS_FIELD_VALIDATION`; no autonomous rig control is authorized.
