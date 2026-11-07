# CP128 — Maintained Frontend Count Guard

**Repository correction: DONE. CP126 exact-SHA CI: FAILED and not certified.**

## Correction

CP126 raised the maintained frontend unit-test total in README and the frontend testing guide from 390 to 391. The CP16 documentation/contract consistency guard still required the old literal, so the Linux backend job at CP126 SHA `24fe365b07dcb24a236a909e1e9b22e52460ace0` failed on that assertion. Updated the guard to require the current 391-test statement in both maintained documents. This is a descendant correction; it does not change the result for the failed CP126 SHA.

The exact-SHA CI record and the platform-specific evidence are in [CP126's report](CP126-WELL-COCKPIT-TIMELINE-LOCALIZATION.md): Actions run `42359789735` reported 1,712 passed/1 failed on Linux, identifying the stale CP16 assertion. The native-Windows job reported 4 failed, 1,694 passed, 1 skipped and 14 PostgreSQL deselections but exposed no failed node IDs. Its artifact was not retrievable from this sandbox, so the Windows causes remain unknown.

Also recorded that CP124 run `42359053299` and CP125 run `42359313648` are **cancelled**, not passing CI results. Their reports retain the individual SHA identities; neither receives certification from a later workflow.

## Verification

- `backend/.venv/bin/python -m pytest -o addopts= tests/test_cp16_adversarial_audit.py::test_cp16_10_documentation_and_contract_consistency_guard -q` — **1 passed** against the maintained README, frontend guide and CP127 inventory.
- This correction adds no test cases; the reviewed inventory remains **1,718 tests across 128 files**, with **14 PostgreSQL node IDs across 11 files**.
- It does not establish a passing full backend suite, a Windows diagnosis, or Linux/native-Windows certification. CP132 still owns exact-final-SHA certification.
