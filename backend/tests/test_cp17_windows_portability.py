"""CP17 Windows Portability & Developer Experience Regression Suite.

Verifies:
1. Windows drive-letter & backslash SQLite URL normalization in Settings.sqlalchemy_url(),
   Database.__init__(), and Alembic _database_url().
2. Cross-platform Python interpreter resolution and RSS measurement fallback in scripts/verify_stack.py
   when POSIX `resource` module is unavailable (as on native Windows `win32`).
3. Cross-platform E2E launcher and Chromium resolution scripts (`scripts/bootstrap.ps1`,
   `frontend/e2e/start-api.mjs`, `frontend/scripts/prepare-chromium.mjs`,
   `frontend/scripts/run-e2e.mjs`, `frontend/playwright.config.ts`).
4. Windows setup documentation (`docs/WINDOWS-SETUP.md`), corrected seed path in `docs/FRONTEND.md`,
   and GitHub Actions `windows-certification` job in `.github/workflows/ci.yml`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

from drillai.core.config import Settings
from drillai.db.session import Database

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cp17_01_windows_sqlite_url_normalization_in_settings_and_database(tmp_path: Path) -> None:
    """Windows drive-letter paths (C:\\... and C:/...) must never be prefixed with REPO_ROOT/backend
    and must always be normalized to forward slashes for SQLAlchemy aiosqlite."""
    win_backslash_url = r"sqlite+aiosqlite:///C:\Users\runneradmin\AppData\Local\Temp\drillai_win.db"
    s1 = Settings(database_url=win_backslash_url)
    resolved1 = s1.sqlalchemy_url()
    assert resolved1 == "sqlite+aiosqlite:///C:/Users/runneradmin/AppData/Local/Temp/drillai_win.db"
    assert "\\" not in resolved1

    win_forward_url = "sqlite+aiosqlite:///D:/DrillAI/data/test.db"
    s2 = Settings(database_url=win_forward_url)
    resolved2 = s2.sqlalchemy_url()
    assert resolved2 == "sqlite+aiosqlite:///D:/DrillAI/data/test.db"

    # Relative path should still resolve under REPO_ROOT/backend with forward slashes
    s3 = Settings(database_url="sqlite+aiosqlite:///./.data/cp17_rel.db")
    resolved3 = s3.sqlalchemy_url()
    assert resolved3.startswith("sqlite+aiosqlite:///")
    assert "\\" not in resolved3
    assert resolved3.endswith("/backend/.data/cp17_rel.db")

    # Database(url) direct constructor must also normalize backslashes
    raw_win_db = f"sqlite+aiosqlite:///{tmp_path}\\subdir\\win_direct.db"
    db = Database(raw_win_db)
    assert "\\" not in db.url
    assert db.url.endswith("/subdir/win_direct.db")


def test_cp17_02_verify_stack_windows_resource_and_python_resolution(tmp_path: Path, monkeypatch) -> None:
    """scripts/verify_stack.py must work when POSIX `resource` is None (Windows) and resolve .venv/Scripts/python.exe."""
    spec = importlib.util.spec_from_file_location("verify_stack", REPO_ROOT / "scripts" / "verify_stack.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Simulate Windows where `resource` is None
    with patch.object(mod, "resource", None):
        rss = mod._rss_mb()
        assert isinstance(rss, float)
        assert rss >= 0.0

    # Simulate DRILLAI_PYTHON override on Windows
    fake_win_python = tmp_path / "python.exe"
    fake_win_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("DRILLAI_PYTHON", str(fake_win_python))
    assert mod._resolve_venv_python() == str(fake_win_python)


def test_cp17_03_cross_platform_scripts_and_windows_ci_contract() -> None:
    """Verify scripts/bootstrap.ps1, Windows E2E scripts, docs/WINDOWS-SETUP.md, and CI windows-certification job."""
    bootstrap_ps1 = REPO_ROOT / "scripts" / "bootstrap.ps1"
    assert bootstrap_ps1.is_file()
    ps1_text = bootstrap_ps1.read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in ps1_text
    assert "prepare-chromium.mjs" in ps1_text

    start_api_text = (REPO_ROOT / "frontend" / "e2e" / "start-api.mjs").read_text(encoding="utf-8")
    assert "'Scripts', 'python.exe'" in start_api_text
    assert ".replace(/\\\\/g, '/')" in start_api_text

    prepare_chromium_text = (REPO_ROOT / "frontend" / "scripts" / "prepare-chromium.mjs").read_text(
        encoding="utf-8"
    )
    assert "process.platform !== 'linux'" in prepare_chromium_text
    assert "pwChromium.executablePath()" in prepare_chromium_text

    run_e2e_text = (REPO_ROOT / "frontend" / "scripts" / "run-e2e.mjs").read_text(encoding="utf-8")
    assert "'@playwright', 'test', 'cli.js'" in run_e2e_text
    assert "spawn(process.execPath" in run_e2e_text

    pw_config_text = (REPO_ROOT / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")
    assert "process.platform === 'linux'" in pw_config_text
    assert "node ./node_modules/vite/bin/vite.js" in pw_config_text

    # docs/FRONTEND.md must use ../scripts/seed_demo.py (not ../../scripts/seed_demo.py) when inside backend/
    frontend_doc_text = (REPO_ROOT / "docs" / "FRONTEND.md").read_text(encoding="utf-8")
    assert "../../scripts/seed_demo.py" not in frontend_doc_text
    assert "../scripts/seed_demo.py" in frontend_doc_text

    # docs/WINDOWS-SETUP.md and CP17 mission report must exist
    win_setup_doc = REPO_ROOT / "docs" / "WINDOWS-SETUP.md"
    assert win_setup_doc.is_file()
    assert len(win_setup_doc.read_text(encoding="utf-8")) > 1500

    ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "certification:" in ci_text
    assert "windows-certification:" in ci_text
    assert "runs-on: windows-latest" in ci_text
    assert "scripts/bootstrap.ps1" in ci_text
