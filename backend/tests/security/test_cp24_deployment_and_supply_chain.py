"""CP24 Deployment Security, Reverse Proxy & Supply-Chain Hardening test suite.

Covers:
1. CP24-A: `docker-compose.yml` and `docker-compose.prod.yml` network/port isolation, `no-new-privileges:true`,
   and mandatory production environment variables.
2. CP24-B: FastAPI HTTP security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
   `Permissions-Policy`, `Content-Security-Policy`, `Strict-Transport-Security`) and `frontend/nginx.conf`
   rate limiting, security headers, and query-string credential log redaction.
3. CP24-C & CP24-D: GitHub Actions 40-char commit SHA pinning, zero `continue-on-error: true`,
   secret-leakage scanning (`scripts/verify_supply_chain_and_secrets.py`), and `SECURITY.md` disclosure policy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from drillai.api.app import create_app
from drillai.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]


async def test_cp24_01_fastapi_security_headers_and_hsts(tmp_path: Path) -> None:
    """Verifies HTTP security headers on both 2xx and 4xx responses and HSTS on HTTPS."""
    db_file = tmp_path / "cp24_headers.db"
    settings = Settings(
        environment="development",
        auth_enabled=True,
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        run_migrations_on_start=False,
    )
    app = create_app(settings)
    await app.state.database.create_all()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            ok_resp = await client.get("/api/v1/health")
            assert ok_resp.status_code == 200
            for header, expected_sub in (
                ("x-content-type-options", "nosniff"),
                ("x-frame-options", "DENY"),
                ("referrer-policy", "strict-origin-when-cross-origin"),
                ("permissions-policy", "camera=()"),
                ("content-security-policy", "default-src 'self'"),
            ):
                assert expected_sub in ok_resp.headers.get(header, "")
            assert "strict-transport-security" not in ok_resp.headers

            # HTTPS via X-Forwarded-Proto emits HSTS
            https_resp = await client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"})
            assert https_resp.status_code == 200
            assert "max-age=63072000" in https_resp.headers.get("strict-transport-security", "")

            # Error responses (401) also carry security headers
            err_resp = await client.get("/api/v1/wells", headers={"X-Forwarded-Proto": "https"})
            assert err_resp.status_code == 401
            assert err_resp.headers.get("x-content-type-options") == "nosniff"
            assert err_resp.headers.get("x-frame-options") == "DENY"
            assert "default-src 'self'" in err_resp.headers.get("content-security-policy", "")
            assert "max-age=63072000" in err_resp.headers.get("strict-transport-security", "")
    finally:
        await app.state.database.dispose()


def test_cp24_02_nginx_reverse_proxy_hardening_and_log_redaction() -> None:
    """Verifies frontend/nginx.conf configures security headers, rate limits, and log redaction."""
    nginx_conf = (REPO_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert "server_tokens off;" in nginx_conf
    assert "map $request_uri $redacted_request_uri" in nginx_conf
    assert "[REDACTED]" in nginx_conf
    assert "log_format drillai_redacted" in nginx_conf
    assert "limit_req_zone $binary_remote_addr zone=drillai_auth_limit:10m rate=10r/s;" in nginx_conf
    assert "limit_req_zone $binary_remote_addr zone=drillai_api_limit:10m rate=60r/s;" in nginx_conf
    assert 'add_header X-Content-Type-Options "nosniff" always;' in nginx_conf
    assert 'add_header X-Frame-Options "DENY" always;' in nginx_conf
    assert 'add_header Referrer-Policy "strict-origin-when-cross-origin" always;' in nginx_conf
    assert "add_header Content-Security-Policy" in nginx_conf
    assert "location /api/v1/auth/" in nginx_conf
    assert "proxy_set_header Upgrade $http_upgrade;" in nginx_conf


def test_cp24_03_docker_compose_production_topology_and_port_isolation() -> None:
    """Verifies docker-compose.yml and docker-compose.prod.yml enforce network/port isolation."""
    base_compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    prod_compose = (REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    # PostgreSQL is on drillai_internal (internal: true) and never bound to 0.0.0.0:5432
    assert "0.0.0.0:5432" not in base_compose
    assert "internal: true" in base_compose
    assert "DRILLAI_SESSION_CSRF_SECRET" in base_compose
    assert "DRILLAI_SECRET_MASTER_KEY" in base_compose

    # Production overlay removes host port mappings from postgres & backend and enforces no-new-privileges
    assert "no-new-privileges:true" in prod_compose
    assert "ports: !reset []" in prod_compose
    assert "DRILLAI_ENVIRONMENT: production" in prod_compose
    assert "DRILLAI_SESSION_COOKIE_SECURE: \"true\"" in prod_compose
    assert "DRILLAI_CORS_ORIGINS: ${DRILLAI_CORS_ORIGINS:?" in prod_compose
    assert "DRILLAI_ALLOWED_HOSTS: ${DRILLAI_ALLOWED_HOSTS:?" in prod_compose
    assert "DRILLAI_SESSION_CSRF_SECRET: ${DRILLAI_SESSION_CSRF_SECRET:?" in prod_compose
    assert "DRILLAI_SECRET_MASTER_KEY: ${DRILLAI_SECRET_MASTER_KEY:?" in prod_compose


def test_cp24_04_github_actions_sha_pinning_and_supply_chain_gate() -> None:
    """Verifies GitHub Actions SHA pinning, secret scan, and SECURITY.md via verify_supply_chain_and_secrets.py."""
    script_path = REPO_ROOT / "scripts" / "verify_supply_chain_and_secrets.py"
    spec = importlib.util.spec_from_file_location("verify_supply_chain_and_secrets", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    pinned = mod.verify_workflow_sha_pinning()
    assert len(pinned) >= 8
    scanned = mod.verify_no_leaked_secrets()
    assert scanned >= 300
    mod.verify_security_docs()

    security_md = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "Reporting a Vulnerability" in security_md
    assert "docs/SECURITY_MODEL.md" in security_md
    assert "Proprietary" in security_md
