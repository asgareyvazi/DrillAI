#!/usr/bin/env python3
"""Supply-chain, GitHub Actions SHA-pinning, and secret-leakage verification gate (CP24).

Executed in CI and locally to certify:
1. All GitHub Actions `uses:` directives in `.github/workflows/ci.yml` are pinned to full 40-char commit SHAs.
2. Zero `continue-on-error: true` directives exist in `.github/workflows/ci.yml`.
3. No private keys, cloud/API credentials, or frontend bundle tokens appear in git-tracked files or `frontend/dist`.
4. `pip-audit` reports zero known vulnerabilities in the Python environment.
5. `npm audit --omit=dev --audit-level=high` reports zero high/critical vulnerabilities in frontend production dependencies.
6. `SECURITY.md` and `docs/SECURITY_MODEL.md` are present and complete.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SHA_PIN_RE = re.compile(r"^\s*-?\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})\s+#\s*v\d+")
USES_LINE_RE = re.compile(r"^\s*-?\s*uses:\s*(.+)$")

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\b(?:ghp_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{50,})\b")),
    ("openai_live_key", re.compile(r"\bsk-proj-[0-9A-Za-z_-]{32,}\b")),
    ("anthropic_live_key", re.compile(r"\bsk-ant-api03-[0-9A-Za-z_-]{32,}\b")),
]

BUNDLE_FORBIDDEN_TOKENS = (
    "drillai-e2e-token",
    "VITE_API_TOKEN",
    "VITE_SECRET",
    "VITE_PRIVATE",
    "VITE_MASTER_KEY",
    "DRILLAI_SECRET_MASTER_KEY",
)


def verify_workflow_sha_pinning() -> list[str]:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    if not workflow_path.is_file():
        raise RuntimeError(f"Missing workflow file: {workflow_path}")
    pinned: list[str] = []
    for lineno, line in enumerate(workflow_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "continue-on-error:" in stripped and "true" in stripped.lower():
            raise RuntimeError(f"Forbidden continue-on-error: true at ci.yml:{lineno}: {line}")
        m_uses = USES_LINE_RE.match(line)
        if m_uses:
            m_pin = SHA_PIN_RE.match(line)
            if not m_pin:
                raise RuntimeError(
                    f"Unpinned or uncommented GitHub Action at ci.yml:{lineno}: {line.strip()} "
                    "(must use owner/repo@<40-char-sha> # vX.Y.Z)"
                )
            pinned.append(f"{m_pin.group(1)}@{m_pin.group(2)}")
    if not pinned:
        raise RuntimeError("No `uses:` directives found in .github/workflows/ci.yml")
    return pinned


def verify_no_leaked_secrets() -> int:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_files = [REPO_ROOT / rel for rel in proc.stdout.splitlines() if rel.strip()]
    scanned = 0
    for path in tracked_files:
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ico", ".pdf"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for rule_name, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                raise RuntimeError(
                    f"Secret scan violation ({rule_name}) in tracked file {path.relative_to(REPO_ROOT)}"
                )

    dist_dir = REPO_ROOT / "frontend" / "dist"
    if dist_dir.is_dir():
        for asset in dist_dir.rglob("*"):
            if not asset.is_file() or asset.suffix.lower() not in {".js", ".mjs", ".css", ".html"}:
                continue
            content = asset.read_text(encoding="utf-8", errors="ignore")
            for forbidden in BUNDLE_FORBIDDEN_TOKENS:
                if forbidden in content:
                    raise RuntimeError(
                        f"Forbidden token {forbidden!r} found in production bundle {asset.relative_to(REPO_ROOT)}"
                    )
    return scanned


def verify_pip_audit() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--progress-spinner", "off"],
        cwd=str(REPO_ROOT / "backend"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip-audit failed (exit={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


def verify_npm_audit() -> None:
    npm_cmd = shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
    npm_bin = npm_cmd or "npm"
    proc = subprocess.run(
        [npm_bin, "audit", "--omit=dev", "--audit-level=high"],
        cwd=str(REPO_ROOT / "frontend"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"npm audit --omit=dev --audit-level=high failed (exit={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


def verify_security_docs() -> None:
    for rel in ("SECURITY.md", "docs/SECURITY_MODEL.md"):
        p = REPO_ROOT / rel
        if not p.is_file() or p.stat().st_size < 200:
            raise RuntimeError(f"Required security documentation missing or incomplete: {rel}")


def main() -> int:
    pinned = verify_workflow_sha_pinning()
    scanned = verify_no_leaked_secrets()
    verify_security_docs()
    verify_pip_audit()
    verify_npm_audit()
    summary = {
        "status": "SUPPLY_CHAIN_AND_SECRET_SCAN_OK",
        "pinned_actions_count": len(pinned),
        "scanned_tracked_files": scanned,
        "pip_audit": "pass",
        "npm_audit_prod_high": "pass",
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
