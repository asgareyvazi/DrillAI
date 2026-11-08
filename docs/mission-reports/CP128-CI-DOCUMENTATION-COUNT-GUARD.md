# CP128 — Maintained Frontend Count Guard

**Repository correction: DONE. CP126 exact-SHA CI: FAILED and not certified.**

## Correction

CP126 raised the maintained frontend unit-test total in README and the frontend testing guide from 390 to 391. The CP16 documentation/contract consistency guard still required the old literal, so the Linux backend job at CP126 SHA `24fe365b07dcb24a236a909e1e9b22e52460ace0` failed on that assertion. Updated the guard to require the current 391-test statement in both maintained documents. This is a descendant correction; it does not change the result for the failed CP126 SHA.

The exact-SHA CI record and the platform-specific evidence are in [CP126's report](CP126-WELL-COCKPIT-TIMELINE-LOCALIZATION.md): Actions run `42359789735` reported 1,712 passed/1 failed on Linux, identifying the stale CP16 assertion. The native-Windows job reported 4 failed, 1,694 passed, 1 skipped and 14 PostgreSQL deselections but exposed no failed node IDs. Its artifact was not retrievable from this sandbox, so the Windows causes remain unknown.

Also recorded that CP124 run `42359053299` and CP125 run `42359313648` are **cancelled**, not passing CI results. Their reports retain the individual SHA identities; neither receives certification from a later workflow.

## Verification

- `backend/.venv/bin/python -m pytest -o addopts= tests/test_cp16_adversarial_audit.py::test_cp16_10_documentation_and_contract_consistency_guard -q` — **1 passed** against the maintained README, frontend guide and CP127 inventory.
- This correction adds no test cases; the reviewed inventory remains **1,718 tests across 128 files**, with **14 PostgreSQL node IDs across 11 files**.
- It does not establish a passing full backend suite, a Windows diagnosis, or Linux/native-Windows certification. The later CP132 exact-final-SHA result is recorded in `CP132-FINAL-RELEASE-ASSURANCE.md`; it certifies only its cited integrated SHA.

## CP128 exact-SHA CI status update

Actions run [42362293307](https://github.com/asgareyvazi/DrillAI/actions/runs/42362293307) tested CP128 SHA `9d3e7ddd56b4e14ac459517b0ca8beded080dea4` and **failed; it is not certified**.

- Linux passed frontend typecheck, lint, unit/component tests, production build, supply-chain verification, backend tests/lint, migration, stack, Docker Compose and E2E-inventory gates. The real-browser E2E step failed with a generic `expect(received).toBe(expected)` assertion. The log request and Playwright artifact `playwright-evidence-42362293307` (artifact ID `13570885839`) both returned EOF through the sandbox's GitHub artifact-storage redirect, so the failing assertion/test remains **unknown**.
- Native Windows passed its preceding frontend, supply-chain, lint and migration checks. Its backend suite reported **2 failed, 1,701 passed, 1 skipped, 14 deselected**. No failed node IDs were exposed in the annotation. The Windows test log artifact `windows-backend-tests-42362293307-1` (artifact ID `13570625713`) also returned EOF from the redirected artifact host, so the causes remain **unknown**. Downstream Windows stack and browser gates were skipped.

These failures are recorded without guessing a root cause or retroactively changing CP128's result. Its count correction remains useful, but CP128 is not exact-SHA certified. CP129 and later descendants still require their own exact-SHA evidence; the integrated CP130–CP132 SHA was later certified by run `42481107515`, which does not retroactively change CP128's result.
