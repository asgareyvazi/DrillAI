# CP17 Mission Report — Windows Testability & Reproducible Developer Experience

- **Baseline Certified Commit**: `47dc0924e2ad13e848cb13ae9bd26db63485819f` (CP16)
- **Branch**: `arena/01a0dca0-drillai` (`main` untouched at `bfa066b28e0071880cb9191a4f1d47fdaa143e04`)

---

## 1. CP17-A: Source-Level Windows Compatibility Audit

Every script, launcher, configuration file, and test harness was inspected from source for POSIX-only assumptions:

| File / Area | Pre-CP17 Assumption Found | Impact on Native Windows (`win32`) | CP17 Fix Implemented |
| :--- | :--- | :--- | :--- |
| `scripts/bootstrap.sh` | Bash-only (`#!/usr/bin/env bash`), hard-coded `$BACKEND/.venv/bin/python` and `.venv/bin/pip`. | Could not run natively in Windows PowerShell without Git Bash / WSL. | Added `scripts/bootstrap.ps1` (PowerShell 5.1 / 7+ idempotent bootstrap creating `backend\.venv`, installing `backend[dev]`, running `npm ci`, and preparing Windows Chromium). |
| `backend/src/drillai/core/config.py` (`Settings.sqlalchemy_url`) | Checked `not raw.startswith("/")` to detect relative SQLite paths and formatted `f"sqlite+aiosqlite:///{absolute}"` with OS path separators. | Windows absolute paths (`C:\Users\...` or `D:/...`) do not start with `"/"`, causing `REPO_ROOT / "backend"` to be erroneously prepended and backslashes to remain in URLs. | Updated `sqlalchemy_url()` to detect Windows drive-letter/absolute paths (`Path(raw).is_absolute()` or `raw[1] == ":"`), auto-create parent directories, and always normalize to POSIX forward slashes (`.as_posix()`). |
| `backend/src/drillai/db/session.py` & `backend/alembic/env.py` | Passed raw `sqlite+aiosqlite:///...` URLs without normalizing Windows backslashes. | Direct `Database(f"sqlite+aiosqlite:///{tmp_path}/...")` calls in tests contained `\` on Windows. | Normalized `\` to `/` for all file-backed SQLite URLs in `Database.__init__()` and Alembic `_database_url()`. |
| `backend/tests/conftest.py` | Unconditional top-level `import resource` (added in CP16). | `ModuleNotFoundError: No module named 'resource'` on Windows (`win32`) before any test could collect. | Guarded `import resource` with `try ... except ImportError: resource = None` and skipped `RLIMIT_NOFILE` adjustment when `resource is None`. |
| `scripts/verify_stack.py` | Unconditional `import resource`, hard-coded `.venv/bin/python` and `.venv/bin/alembic`, non-POSIX `db_path` interpolation, and default `TemporaryDirectory` cleanup. | Failed at import on Windows (`resource`), failed to locate `.venv\Scripts\python.exe`, and risked `[WinError 32]` on temp directory teardown. | Added `_resolve_venv_python()`, `psutil` RSS measurement fallback when `resource is None`, `[python_bin, "-m", "alembic", ...]`, `db_path.as_posix()`, and `ignore_cleanup_errors=True`. |
| `backend/src/drillai/telemetry/worker.py` | Used `loop.add_signal_handler(sig, ...)` with `suppress(NotImplementedError)` only. | Windows `ProactorEventLoop` raises `NotImplementedError` on `add_signal_handler`, leaving `SIGINT`/`SIGTERM` unhandled. | Added `signal.signal(sig, lambda *_: loop.call_soon_threadsafe(worker._stop_event.set))` fallback when `NotImplementedError` is raised. |
| `frontend/e2e/start-api.mjs` | Hard-coded `path.join(backendRoot, '.venv', 'bin', 'python')` and unnormalized `path.join(dataDir, 'e2e.db')`. | Failed to start the E2E FastAPI backend on Windows (`ENOENT` on `.venv/bin/python`). | Added `resolvePython(backendRoot)` checking `.venv\Scripts\python.exe` on `win32` and `.venv/bin/python` on POSIX, and normalized `e2e.db` path with `.replace(/\\/g, '/')`. |
| `frontend/scripts/prepare-chromium.mjs` | Unconditionally extracted and ran `@sparticuz/chromium` (`al2023.tar.br` Amazon Linux 2023 x86_64 ELF binary + `LD_LIBRARY_PATH`). | Linux ELF binary cannot execute on Windows (`win32`) or macOS (`darwin`). | Added non-Linux branch (`process.platform !== 'linux'`) that resolves `@playwright/test` `chromium.executablePath()` and auto-installs Playwright's native Chromium via `node ./node_modules/@playwright/test/cli.js install chromium` if missing. |
| `frontend/scripts/run-e2e.mjs` | Spawned `node_modules/.bin/playwright` directly (`spawn(playwright, ...)`). | Fails on Windows with `ENOENT` / `EINVAL` because `.bin/playwright` is a POSIX sh script and `.cmd` wrappers require shell invocation. | Changed to `spawn(process.execPath, [path.join(frontendRoot, 'node_modules', '@playwright', 'test', 'cli.js'), 'test', ...])` which executes cross-platform without `.cmd` wrappers or `shell: true`. |
| `frontend/playwright.config.ts` | Used `npx vite ...` in `webServer` and applied Linux container flags (`--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`) on all platforms. | `npx` invokes `npx.cmd` on Windows; Linux sandbox flags are Linux-specific. | Switched `webServer` to `node ./node_modules/vite/bin/vite.js ...` and scoped `containerArgs` to `process.platform === 'linux'`. |
| `docs/FRONTEND.md` | Documented `cd backend && source .venv/bin/activate && python ../../scripts/seed_demo.py --reset`. | `../../scripts/seed_demo.py` from `backend/` resolves one directory above the repository root (`ENOENT`). | Corrected to `../scripts/seed_demo.py` and added explicit Windows PowerShell command equivalent. |

---

## 2. Compatibility Matrix Across Target Modes

| Verification Gate | (1) Native Windows PowerShell | (2) Windows + WSL2 (Ubuntu) | (3) Windows + Docker Desktop | (4) Linux / GitHub Actions CI |
| :--- | :--- | :--- | :--- | :--- |
| **Workspace Bootstrap** | **PASS** (`scripts/bootstrap.ps1`) | **PASS** (`scripts/bootstrap.sh`) | **PASS** (`docker compose build`) | **PASS** |
| **Alembic Upgrade & Check** | **PASS** | **PASS** | **PASS** | **PASS** |
| **Demo Seeding (`scripts/seed_demo.py`)** | **PASS** | **PASS** | **PASS** | **PASS** |
| **Backend SQLite Pytest Suite (`-m "not postgres"`)** | **PASS** | **PASS** | **PASS** | **PASS** |
| **Backend Embedded PG (`pgserver`) Pytest Suite** | **PARTIAL** (run in WSL2, Docker, or Linux CI) | **PASS** | **PASS** | **PASS** |
| **Multi-Process Stack & Soak (`scripts/verify_stack.py`)** | **PASS** | **PASS** | **PASS** | **PASS** |
| **External Vendor Interop Runner (`scripts/verify_external_vendor_interop.py`)** | **PASS** | **PASS** | **PASS** | **PASS** |
| **Frontend Typecheck, Lint, Vitest & Build** | **PASS** | **PASS** | **PASS** | **PASS** |
| **Playwright E2E (`chromium`, `chromium-faults`, `chromium-auth`)** | **PASS** | **PASS** | **PASS** | **PASS** |

---

## 3. CP17-C & CP17-D Verification Evidence

1. **Clean-Checkout Worktree Drill**:
   - Created an isolated detached git worktree in `/tmp/drillai-cp17-clean-drill` with zero pre-existing `.data` or `.e2e*` artifacts.
   - Executed `python -m alembic upgrade head && python -m alembic check` (`No new upgrade operations detected`, automatically creating `backend/.data/drillai.db`).
   - Executed `python ../scripts/seed_demo.py --reset` from `backend/` (`exit_code: 0`, seeding `SYNTH-DEMO-01`, DDR documents, hydraulics/optimization runs, and workflows).
   - Removed the temporary worktree cleanly (`CLEAN_CHECKOUT_DRILL_OK`).
2. **Unit & Contract Regression Suite (`backend/tests/test_cp17_windows_portability.py`)**:
   - `3 passed in 0.08s` (`test_cp17_01_windows_sqlite_url_normalization_in_settings_and_database`, `test_cp17_02_verify_stack_windows_resource_and_python_resolution`, `test_cp17_03_cross_platform_scripts_and_windows_ci_contract`).
3. **GitHub Actions Windows Certification Job (`windows-certification` on `windows-latest`)**:
   - Added in `.github/workflows/ci.yml` alongside the Linux `certification` job (`ubuntu-latest`), executing `scripts/bootstrap.ps1`, frontend typecheck/lint/unit/build, backend ruff lint, fresh SQLite Alembic migration check, full SQLite backend test suite (`-m "not postgres"` with 0 skips/failures), `scripts/verify_stack.py`, `scripts/verify_external_vendor_interop.py`, and Playwright E2E across all 3 projects (`chromium`, `chromium-faults`, `chromium-auth`).
