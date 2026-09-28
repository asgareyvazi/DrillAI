#!/usr/bin/env bash
# Prepare every dependency the DrillAI test suites need, from nothing.
#
# This script exists because "it works on my machine after I ran four undocumented commands" is not a
# reproducible build. It creates the backend virtualenv, installs the frontend packages, and — when the
# browser is not present — materialises the Playwright Chromium build together with the shared
# libraries it needs.
#
# Usage:
#   scripts/bootstrap.sh            # backend + frontend + browser
#   scripts/bootstrap.sh --no-browser
#
# Everything is idempotent: running it twice is a no-op.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
FRONTEND="$REPO_ROOT/frontend"
PYTHON_BIN="${DRILLAI_PYTHON_BIN:-python3}"
WITH_BROWSER=1

for arg in "$@"; do
  case "$arg" in
    --no-browser) WITH_BROWSER=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$1"; }

# --------------------------------------------------------------------------- backend
if [ ! -x "$BACKEND/.venv/bin/python" ]; then
  say "creating backend virtualenv"
  "$PYTHON_BIN" -m venv "$BACKEND/.venv"
fi
say "installing the backend package (editable, with dev extras)"
"$BACKEND/.venv/bin/pip" install --quiet --upgrade pip
"$BACKEND/.venv/bin/pip" install --quiet -e "$BACKEND[dev]"

# --------------------------------------------------------------------------- frontend
if [ ! -d "$FRONTEND/node_modules" ]; then
  say "installing frontend packages"
  (cd "$FRONTEND" && npm install --no-audit --no-fund)
else
  say "frontend packages already installed"
fi

# --------------------------------------------------------------------------- browser
if [ "$WITH_BROWSER" -eq 1 ]; then
  say "preparing the Playwright Chromium build"
  (cd "$FRONTEND" && node scripts/prepare-chromium.mjs)
fi

say "bootstrap complete"
