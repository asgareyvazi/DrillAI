# Windows Setup, Development & Verification Guide (`docs/WINDOWS-SETUP.md`)

This guide provides exact, copy-pasteable instructions for running, testing, and verifying **DrillAI** on Windows across three supported developer modes:

1. **Mode A — Native Windows PowerShell** (Windows 10/11 or Windows Server 2022, PowerShell 5.1 or PowerShell 7+, no WSL2 or Docker required for SQLite development, unit tests, multi-process stack verification, and Playwright E2E)
2. **Mode B — Windows + WSL2 (Ubuntu)** (recommended when running embedded `pgserver` Linux binaries locally without Docker)
3. **Mode C — Windows + Docker Desktop** (recommended for running the full 5-service containerized PostgreSQL 16 + Alembic + FastAPI + Connector Worker + Nginx/SPA deployment stack)

---

## 1. Compatibility Matrix (Source-Audited & CI-Certified)

| Capability / Verification Gate | Native Windows PowerShell | Windows + WSL2 (Ubuntu) | Windows + Docker Desktop | Linux / GitHub Actions CI |
| :--- | :--- | :--- | :--- | :--- |
| **Bootstrap (`scripts/bootstrap.ps1` / `scripts/bootstrap.sh`)** | **PASS** (`scripts/bootstrap.ps1`) | **PASS** (`scripts/bootstrap.sh`) | **PASS** (`docker compose build`) | **PASS** (`ubuntu-latest` + `windows-latest`) |
| **Backend Editable Install (`pip install -e "backend[dev]"`)** | **PASS** (`.venv\Scripts\python.exe`) | **PASS** (`.venv/bin/python`) | **PASS** (container build) | **PASS** |
| **Alembic Migrations (`alembic upgrade head && alembic check`)** | **PASS** (POSIX-normalized SQLite & PG URLs) | **PASS** | **PASS** (`migrate` service) | **PASS** |
| **Backend SQLite Test Suite (`pytest -m "not postgres"`)** | **PASS** (`windows-certification` CI job) | **PASS** | **PASS** | **PASS** |
| **Embedded PostgreSQL (`pgserver`) Tests (`DRILLAI_TEST_POSTGRES=1`)** | **PARTIAL** (use external/Docker PG via `DRILLAI_TEST_DATABASE_URL` or WSL2; `pgserver` uses Unix socket / POSIX process conventions) | **PASS** (`DRILLAI_TEST_POSTGRES=1`) | **PASS** (`postgres:16-alpine`) | **PASS** (`ubuntu-latest` CI job) |
| **Multi-Process Stack & 32-Cycle Soak (`scripts/verify_stack.py`)** | **PASS** (uses `psutil` RSS fallback & `.venv\Scripts\python.exe`) | **PASS** | **PASS** | **PASS** (`ubuntu-latest` & `windows-latest`) |
| **External Vendor Interop Runner (`scripts/verify_external_vendor_interop.py`)** | **PASS** (`--mode harness` & `--mode external`) | **PASS** | **PASS** | **PASS** |
| **Frontend Typecheck, Lint, Unit Tests & Build (`npm test`, `npm run build`)** | **PASS** | **PASS** | **PASS** | **PASS** (`ubuntu-latest` & `windows-latest`) |
| **Full Playwright E2E (`npm run e2e` — main, faults, auth stacks)** | **PASS** (uses Playwright native Windows Chromium + `node cli.js`) | **PASS** | **PASS** | **PASS** (`ubuntu-latest` & `windows-latest`) |

---

## 2. Prerequisites for Native Windows PowerShell

1. **Git for Windows** (`>= 2.40`):
   - Recommended line-ending config so shell scripts (`*.sh`) retain LF while PowerShell scripts work natively:
     ```powershell
     git config --global core.autocrlf input
     git config --global core.longpaths true
     ```
2. **Python 3.11 or 3.12 (64-bit)** from [python.org](https://www.python.org/downloads/windows/) or `winget install Python.Python.3.11` (ensure **"Add python.exe to PATH"** is checked).
3. **Node.js 20 LTS or 22 LTS (64-bit)** from [nodejs.org](https://nodejs.org/) or `winget install OpenJS.NodeJS.LTS`.
4. **PowerShellExecutionPolicy**: allow local scripts in your current user session:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   ```

---

## 3. Clean Checkout & One-Command Bootstrap (PowerShell)

Open **PowerShell** at the repository root and run:

```powershell
# 1. Verify toolchain
python --version
node --version
npm --version

# 2. Run the idempotent Windows PowerShell bootstrap script
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

What `.\scripts\bootstrap.ps1` does automatically:
- Creates `backend\.venv` using your Windows Python (`>=3.11`).
- Upgrades `pip` and installs `backend[dev]` in editable mode (`backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"`).
- Runs `npm ci` in `frontend\`.
- Invokes `node scripts/prepare-chromium.mjs` inside `frontend\`, which detects `win32` and automatically installs Playwright's native Windows Chromium binary if not already present.

---

## 4. Environment Configuration & Database Initialization (PowerShell)

```powershell
# Optional: copy .env.example to .env at the repository root
Copy-Item .env.example .env -Force

# Run Alembic migrations against the default local SQLite database (backend\.data\drillai.db)
Push-Location backend
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic check

# Seed the local development database with the labelled synthetic demo well & workflows
.\.venv\Scripts\python.exe ..\scripts\seed_demo.py --reset
Pop-Location
```

> **Note on relative paths**: When inside `backend\`, the scripts directory is one level up (`..\scripts\seed_demo.py`). From the repository root, run `.\backend\.venv\Scripts\python.exe .\scripts\seed_demo.py --reset --database-url sqlite+aiosqlite:///./backend/.data/drillai.db`.

---

## 5. Starting Backend, Worker & Frontend Locally (PowerShell)

Open two (or three) PowerShell terminals at the repository root:

### Terminal 1 — FastAPI Backend (`http://127.0.0.1:8099`)

```powershell
cd backend
$env:DRILLAI_ENVIRONMENT = "development"
$env:DRILLAI_AUTH_ENABLED = "false"
.\.venv\Scripts\python.exe -m uvicorn drillai.api.app:create_app --factory --host 127.0.0.1 --port 8099 --reload
```

Verify readiness in another PowerShell tab:
```powershell
Invoke-RestMethod http://127.0.0.1:8099/api/v1/health
Invoke-RestMethod http://127.0.0.1:8099/api/v1/health/ready -Headers @{ "X-Dev-Roles" = "viewer" }
```

### Terminal 2 — Frontend Vite Dev Server (`http://localhost:5173`)

```powershell
cd frontend
npm run dev
```

`frontend/vite.config.ts` automatically proxies `/api` to `http://127.0.0.1:8099` (configurable via `$env:DRILLAI_API_URL`).

### Terminal 3 (Optional) — Standalone Telemetry Connector Worker

```powershell
cd backend
$env:DRILLAI_ENVIRONMENT = "development"
$env:DRILLAI_AUTH_ENABLED = "false"
.\.venv\Scripts\python.exe -m drillai.telemetry.worker
```

---

## 6. Running the Verification Suite on Windows PowerShell

### 6.1 Backend Lint & SQLite Test Suite

```powershell
cd backend
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -m "not postgres" -o addopts=""
```

### 6.2 Multi-Process Stack & 32-Cycle Soak Verification

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\verify_stack.py
```

Expected final line:
```text
STACK_VERIFICATION_OK: all 16 multi-process scenarios and 32-cycle bounded soak verified.
```

### 6.3 External Vendor Interoperability & Conformance Runner

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\verify_external_vendor_interop.py --mode harness
.\backend\.venv\Scripts\python.exe .\scripts\verify_external_vendor_interop.py --mode external
```

### 6.4 Frontend Typecheck, Lint, Unit Tests & Production Build

```powershell
cd frontend
npm run typecheck
npm run lint
npm test
npm run build
```

### 6.5 Playwright End-to-End Browser Suite

Make sure ports `8099`, `8098`, `8097`, `5199`, `5198`, and `5197` are free, then run from `frontend\`:

```powershell
cd frontend
# Run the full E2E suite across main, faults, and auth stacks:
npm run e2e

# Or run specific E2E spec files:
npm run e2e -- e2e/app.spec.ts e2e/connectors-platform.spec.ts e2e/error-faults.spec.ts e2e/error-auth.spec.ts
```

---

## 7. Running with Docker Desktop on Windows (PostgreSQL 16 + Full Stack)

When Docker Desktop for Windows (WSL2 backend enabled) is running:

```powershell
$env:POSTGRES_PASSWORD = "local_dev_pg_password_change_me"
$env:DRILLAI_AUTH_ENABLED = "false"
docker compose up -d --build

# Check readiness of Backend (8000) and Nginx/Frontend (8080)
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
Invoke-RestMethod http://127.0.0.1:8080/api/v1/health/ready

# Tear down cleanly
docker compose down -v --remove-orphans
```

---

## 8. Troubleshooting Common Windows Issues

1. **Port already in use (`8099`, `8098`, `8097`, `5199`, `5198`, `5197`)**:
   Find and stop the process holding the port in PowerShell:
   ```powershell
   Get-NetTCPConnection -LocalPort 8099 -ErrorAction SilentlyContinue |
     Select-Object -ExpandProperty OwningProcess -Unique |
     ForEach-Object { Stop-Process -Id $_ -Force }
   ```
2. **Windows Defender / Antivirus file locking on temporary SQLite databases**:
   `backend/tests/conftest.py` disposes all active SQLAlchemy `Database` engines (`await dispose_all_databases()`) after every test, and `scripts/verify_stack.py` terminates child processes before cleaning temporary directories with `ignore_cleanup_errors=True`.
3. **PostgreSQL (`postgres`-marked) tests on native Windows**:
   The `postgres`-marked pytest tests use `pgserver` on Linux/WSL2 (`DRILLAI_TEST_POSTGRES=1`). On native Windows PowerShell, run `pytest -m "not postgres"` for the SQLite suite, or run inside WSL2 Ubuntu / Docker Desktop for PostgreSQL integration tests.
