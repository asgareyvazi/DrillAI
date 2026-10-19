#Requires -Version 5.1
<#
.SYNOPSIS
    Idempotent Windows PowerShell bootstrap for the DrillAI workspace.

.DESCRIPTION
    Safe to run repeatedly on native Windows PowerShell (5.1 or 7+):
      - Verifies Python (>=3.11) and Node.js (>=20) are installed
      - Creates backend\.venv if missing and installs backend editable package + dev extras
      - Runs npm ci (or npm install) in frontend\ if node_modules is missing or stale
      - Ensures Playwright Chromium browser is installed and ready for E2E verification
#>

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"

Write-Host "==> Checking Python (>=3.11) and Node.js (>=20) toolchain..."
$PythonCmd = $null
foreach ($candidate in @("python", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $PythonCmd = $candidate
        break
    }
}
if (-not $PythonCmd) {
    throw "Python (>=3.11) is required on PATH."
}

if (-not (Get-Command "node" -ErrorAction SilentlyContinue)) {
    throw "Node.js (>=20) is required on PATH."
}
if (-not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
    throw "npm is required on PATH."
}

$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating backend\.venv..."
    if ($PythonCmd -eq "py") {
        & py -3 -m venv (Join-Path $BackendDir ".venv")
    } else {
        & $PythonCmd -m venv (Join-Path $BackendDir ".venv")
    }
}

Write-Host "==> Installing backend (editable + dev extras)..."
& $VenvPython -m pip install --quiet --upgrade pip "setuptools>=83.0.0"
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in backend\.venv" }
& $VenvPython -m pip install --quiet -e "${BackendDir}[dev]"
if ($LASTEXITCODE -ne 0) { throw "Failed to install backend[dev] in backend\.venv" }

$NodeModulesBin = Join-Path $FrontendDir "node_modules\.bin"
$PkgLock = Join-Path $FrontendDir "package-lock.json"
$InstallMarker = Join-Path $FrontendDir "node_modules\.package-lock.json"

$NeedNpmInstall = (-not (Test-Path $NodeModulesBin))
if (-not $NeedNpmInstall -and (Test-Path $PkgLock) -and (Test-Path $InstallMarker)) {
    if ((Get-Item $PkgLock).LastWriteTimeUtc -gt (Get-Item $InstallMarker).LastWriteTimeUtc) {
        $NeedNpmInstall = $true
    }
}

if ($NeedNpmInstall) {
    Write-Host "==> Installing frontend dependencies..."
    Push-Location $FrontendDir
    try {
        if (Test-Path $PkgLock) {
            & npm ci --no-audit --no-fund
        } else {
            & npm install --no-audit --no-fund
        }
        if ($LASTEXITCODE -ne 0) { throw "Frontend npm install failed." }
    } finally {
        Pop-Location
    }
}

Write-Host "==> Preparing headless Chromium for Playwright E2E..."
Push-Location $FrontendDir
try {
    & node scripts/prepare-chromium.mjs
    if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium preparation failed." }
} finally {
    Pop-Location
}

Write-Host "==> Windows workspace ready."
Write-Host "    Backend Python : $VenvPython"
Write-Host "    Frontend       : $FrontendDir"
