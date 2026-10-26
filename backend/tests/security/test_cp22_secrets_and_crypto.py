"""CP22 Security Verification Suite — Secrets Management, Password KDF Hardening & Config Guards.

Covers:
- CP22-A: Authenticated encryption (`AES-256-GCM`) for `SecretRef(backend="db_encrypted")`,
  HKDF-SHA256 key derivation, `(org_id, secret_key, kv)` AAD binding, key rotation (`v1 -> v2`),
  fail-closed behavior without master key, RBAC on `/api/v1/platform/secrets`, redaction in API
  and audit logs, and resolution in `connectors.py` and `notifications.py`.
- CP22-B: Hardened OWASP `scrypt` (`N=2^15=32768, r=8, p=1`, 32 MiB) and `Argon2id`
  (`m=19456 KiB, t=2, p=1`, 19 MiB), legacy `N=16384` verification + automatic rehash-on-login,
  pre-KDF DoS parameter rejection, bounded async KDF concurrency, and latency/memory benchmarks.
- CP22-C & CP22-D: Production startup guards (`allowed_hosts`, `session_cookie_secure`,
  `session_csrf_secret`, `secret_master_key`, placeholder `database_url`, `TrustedHostMiddleware`)
  and `docs/SECURITY_MODEL.md` completeness.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from drillai.api.app import create_app
from drillai.core.config import Settings, reset_settings_cache
from drillai.core.errors import ConfigurationError, ValidationFailed
from drillai.db.models import AuditLog, Membership, Organization, SecretRef, User
from drillai.security.passwords import (
    ARGON2ID_DEFAULT_MEMORY_KIB,
    SCRYPT_DEFAULT_N,
    benchmark_password_kdf,
    hash_password,
    needs_rehash,
    verify_password,
    verify_password_async,
)
from drillai.security.secrets import (
    _load_master_keyring,
    decrypt_secret_value,
    encrypt_secret_value,
    extract_envelope_key_version,
    needs_key_rotation,
    rotate_secret_envelope,
)
from drillai.telemetry.connectors import resolve_secret_refs
from drillai.workflow.notifications import resolve_notification_secret, resolve_notification_secret_async

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("active_version", "previous_keys", "duplicate_version"),
    [
        (2, "v2:previous-master-key-that-is-long-enough", 2),
        (3, "v1:previous-master-key-that-is-long-enough,v01:another-previous-master-key-long-enough", 1),
    ],
)
def test_master_keyring_rejects_duplicate_active_or_previous_versions(
    active_version: int, previous_keys: str, duplicate_version: int
) -> None:
    settings = Settings(
        environment="production",
        secret_master_key="active-master-key-that-is-long-enough-for-production",
        secret_active_key_version=active_version,
        secret_previous_master_keys=previous_keys,
    )
    with pytest.raises(ConfigurationError) as caught:
        _load_master_keyring(settings)
    assert caught.value.details == {"reason": "duplicate_key_version", "key_version": duplicate_version}


def test_notification_environment_secret_references_are_scoped_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRILLAI_DATABASE_URL", "must-not-be-exposed")
    monkeypatch.setenv("DATABASE_URL", "also-must-not-be-exposed")
    monkeypatch.setenv("DRILLAI_NOTIFY_SECRET_RIG_WEBHOOK_KEY", "notify-scoped-secret")
    monkeypatch.setenv("DRILLAI_SECRET_TEST_TOKEN", "indirect-secret")

    assert resolve_notification_secret("env:DRILLAI_DATABASE_URL") is None
    assert resolve_notification_secret("DRILLAI_DATABASE_URL") is None
    assert resolve_notification_secret("DATABASE_URL") is None
    assert resolve_notification_secret("rig-webhook-key") == "notify-scoped-secret"
    assert resolve_notification_secret("env:DRILLAI_SECRET_TEST_TOKEN") == "indirect-secret"


def test_cp22_01_encrypted_secret_ref_aead_key_rotation_aad_and_redaction() -> None:
    """Verify AES-256-GCM SecretRef encryption, AAD tenant/key binding, fail-closed, and v1->v2 rotation."""
    key_v1 = "master-key-v1-at-least-32-bytes-long-2026!!"
    key_v2 = "master-key-v2-rotated-32-bytes-long-2026!!"
    cfg_v1 = Settings(
        environment="production",
        secret_backend="database",
        secret_master_key=key_v1,
        secret_active_key_version=1,
    )

    plaintext = "SuperSecretRigWitsmlPass!98765"
    envelope_v1, kv = encrypt_secret_value(
        plaintext,
        org_id="org_alpha",
        secret_key="rig01_witsml_pass",
        settings=cfg_v1,
    )
    assert kv == 1
    assert envelope_v1.startswith("enc:v1:kv=1:")
    assert plaintext not in envelope_v1
    assert extract_envelope_key_version(envelope_v1) == 1
    assert needs_key_rotation(envelope_v1, settings=cfg_v1) is False

    # Round-trip decrypt with matching org_id and secret_key
    recovered = decrypt_secret_value(
        envelope_v1,
        org_id="org_alpha",
        secret_key="rig01_witsml_pass",
        settings=cfg_v1,
    )
    assert recovered == plaintext

    # Cross-tenant AAD transplantation (`org_beta` instead of `org_alpha`) fails closed
    with pytest.raises(ValidationFailed) as exc_org:
        decrypt_secret_value(
            envelope_v1,
            org_id="org_beta",
            secret_key="rig01_witsml_pass",
            settings=cfg_v1,
        )
    assert exc_org.value.details["reason"] == "secret_decryption_failed"

    # Cross-key AAD transplantation (`rig02_witsml_pass` instead of `rig01_witsml_pass`) fails closed
    with pytest.raises(ValidationFailed) as exc_key:
        decrypt_secret_value(
            envelope_v1,
            org_id="org_alpha",
            secret_key="rig02_witsml_pass",
            settings=cfg_v1,
        )
    assert exc_key.value.details["reason"] == "secret_decryption_failed"

    # Missing master key fails closed
    with pytest.raises(ValidationFailed) as exc_missing:
        decrypt_secret_value(
            envelope_v1,
            org_id="org_alpha",
            secret_key="rig01_witsml_pass",
            settings=Settings(secret_master_key=""),
        )
    assert exc_missing.value.details["reason"] == "master_key_unavailable"

    # Key rotation from v1 to v2 using keyring (`secret_previous_master_keys="v1:..."`)
    cfg_v2_transition = Settings(
        environment="production",
        secret_backend="database",
        secret_master_key=key_v2,
        secret_active_key_version=2,
        secret_previous_master_keys=f"v1:{key_v1}",
    )
    assert needs_key_rotation(envelope_v1, settings=cfg_v2_transition) is True
    envelope_v2, prev_kv, new_kv = rotate_secret_envelope(
        envelope_v1,
        org_id="org_alpha",
        secret_key="rig01_witsml_pass",
        settings=cfg_v2_transition,
    )
    assert prev_kv == 1
    assert new_kv == 2
    assert envelope_v2.startswith("enc:v1:kv=2:")
    assert needs_key_rotation(envelope_v2, settings=cfg_v2_transition) is False

    # After rotation completes, v1 key can be retired completely and v2 still decrypts
    cfg_v2_only = Settings(
        environment="production",
        secret_backend="database",
        secret_master_key=key_v2,
        secret_active_key_version=2,
    )
    assert (
        decrypt_secret_value(
            envelope_v2,
            org_id="org_alpha",
            secret_key="rig01_witsml_pass",
            settings=cfg_v2_only,
        )
        == plaintext
    )
    with pytest.raises(ValidationFailed) as exc_retired:
        decrypt_secret_value(
            envelope_v1,
            org_id="org_alpha",
            secret_key="rig01_witsml_pass",
            settings=cfg_v2_only,
        )
    assert exc_retired.value.details["reason"] == "unknown_key_version"


async def test_cp22_02_platform_secrets_api_rbac_connector_and_notification_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify /api/v1/platform/secrets RBAC, tenant isolation, redaction, and connector/notification resolution."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'cp22_secrets.db').as_posix()}"
    master_v1 = "cp22-test-master-key-v1-32-bytes-minimum!!"
    master_v2 = "cp22-test-master-key-v2-32-bytes-minimum!!"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_SECRET_BACKEND", "database")
    monkeypatch.setenv("DRILLAI_SECRET_MASTER_KEY", master_v1)
    monkeypatch.setenv("DRILLAI_SECRET_ACTIVE_KEY_VERSION", "1")
    reset_settings_cache()

    app = create_app(
        Settings(
            database_url=db_url,
            auth_enabled=True,
            secret_backend="database",
            secret_master_key=master_v1,
            secret_active_key_version=1,
        )
    )
    await app.state.database.create_all()
    try:
        async with app.state.database.session() as session:
            org_a = Organization(id="org_cp22_a", slug="cp22-alpha", name="CP22 Alpha")
            org_b = Organization(id="org_cp22_b", slug="cp22-beta", name="CP22 Beta")
            u_admin_a = User(
                id="usr_cp22_admin_a",
                email="admin@cp22-alpha.local",
                display_name="Alpha Admin",
                password_hash=hash_password("AlphaAdminPass!2026"),
                is_active=True,
            )
            u_viewer_a = User(
                id="usr_cp22_viewer_a",
                email="viewer@cp22-alpha.local",
                display_name="Alpha Viewer",
                password_hash=hash_password("AlphaViewerPass!2026"),
                is_active=True,
            )
            u_admin_b = User(
                id="usr_cp22_admin_b",
                email="admin@cp22-beta.local",
                display_name="Beta Admin",
                password_hash=hash_password("BetaAdminPass!2026"),
                is_active=True,
            )
            session.add_all([org_a, org_b, u_admin_a, u_viewer_a, u_admin_b])
            await session.flush()
            session.add_all(
                [
                    Membership(org_id=org_a.id, user_id=u_admin_a.id, role_key="admin"),
                    Membership(org_id=org_a.id, user_id=u_viewer_a.id, role_key="viewer"),
                    Membership(org_id=org_b.id, user_id=u_admin_b.id, role_key="admin"),
                ]
            )

        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_a_client,
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as viewer_a_client,
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_b_client,
        ):
            r_login_admin_a = await admin_a_client.post(
                "/api/v1/auth/login",
                json={"email": "admin@cp22-alpha.local", "password": "AlphaAdminPass!2026"},
            )
            assert r_login_admin_a.status_code == 200
            csrf_admin_a = r_login_admin_a.json()["csrf_token"]

            # The API must accept only the same dedicated secret-name grammar that the resolver uses.
            for invalid_env_name in ("DRILLAI_DATABASE_URL", "DRILLAI_SECRET_X"):
                r_invalid_env = await admin_a_client.post(
                    "/api/v1/platform/secrets",
                    headers={"X-CSRF-Token": csrf_admin_a},
                    json={
                        "key": "invalid_env_secret",
                        "backend": "env",
                        "env_var": invalid_env_name,
                    },
                )
                assert r_invalid_env.status_code == 422, r_invalid_env.text

            monkeypatch.setenv("DRILLAI_SECRET_CP22_ENV", "cp22-env-secret-value")
            r_env = await admin_a_client.post(
                "/api/v1/platform/secrets",
                headers={"X-CSRF-Token": csrf_admin_a},
                json={
                    "key": "test_env_secret",
                    "backend": "env",
                    "env_var": "DRILLAI_SECRET_CP22_ENV",
                },
            )
            assert r_env.status_code == 201, r_env.text
            assert r_env.json()["backend"] == "env"
            assert "cp22-env-secret-value" not in r_env.text

            r_login_viewer_a = await viewer_a_client.post(
                "/api/v1/auth/login",
                json={"email": "viewer@cp22-alpha.local", "password": "AlphaViewerPass!2026"},
            )
            assert r_login_viewer_a.status_code == 200
            csrf_viewer_a = r_login_viewer_a.json()["csrf_token"]

            r_login_admin_b = await admin_b_client.post(
                "/api/v1/auth/login",
                json={"email": "admin@cp22-beta.local", "password": "BetaAdminPass!2026"},
            )
            assert r_login_admin_b.status_code == 200
            csrf_admin_b = r_login_admin_b.json()["csrf_token"]

            # 1. Viewer is refused on GET and POST /api/v1/platform/secrets (403 PermissionDenied)
            r_viewer_get = await viewer_a_client.get("/api/v1/platform/secrets")
            assert r_viewer_get.status_code == 403
            r_viewer_post = await viewer_a_client.post(
                "/api/v1/platform/secrets",
                headers={"X-CSRF-Token": csrf_viewer_a},
                json={
                    "key": "rig_witsml_pass",
                    "backend": "db_encrypted",
                    "secret_value": "TopSecretRigCredential!2026",
                },
            )
            assert r_viewer_post.status_code == 403

            # 2. Tenant A Admin creates an encrypted secret
            secret_clear = "TopSecretRigCredential!2026"
            r_create = await admin_a_client.post(
                "/api/v1/platform/secrets",
                headers={"X-CSRF-Token": csrf_admin_a},
                json={
                    "key": "rig_witsml_pass",
                    "backend": "db_encrypted",
                    "secret_value": secret_clear,
                    "description": "Primary WITSML store password",
                    "scope": "connector",
                },
            )
            assert r_create.status_code == 201, r_create.text
            created_body = r_create.json()
            assert created_body["key"] == "rig_witsml_pass"
            assert created_body["backend"] == "db_encrypted"
            assert created_body["key_version"] == 1
            assert created_body["masked_preview"] == "********"
            assert secret_clear not in r_create.text
            assert "enc:v1:" not in r_create.text

            # 3. Tenant B Admin cannot see or rotate Tenant A's secret
            r_list_b = await admin_b_client.get("/api/v1/platform/secrets")
            assert r_list_b.status_code == 200
            assert r_list_b.json()["items"] == []
            r_rot_b = await admin_b_client.post(
                "/api/v1/platform/secrets/rig_witsml_pass/rotate",
                headers={"X-CSRF-Token": csrf_admin_b},
                json={},
            )
            assert r_rot_b.status_code == 404

            # 4. Connector and notification secret resolvers decrypt the SecretRef row
            async with app.state.database.session() as session:
                resolved_map = await resolve_secret_refs(
                    session,
                    "org_cp22_a",
                    {"password": "secret_ref:rig_witsml_pass"},
                )
                assert resolved_map["password"] == secret_clear

                resolved_env = await resolve_secret_refs(
                    session,
                    "org_cp22_a",
                    {"password": "env:DRILLAI_SECRET_CP22_ENV"},
                )
                assert resolved_env["password"] == "cp22-env-secret-value"

                # A legacy or manually inserted broad environment locator must not reach notification delivery.
                monkeypatch.setenv("DRILLAI_DATABASE_URL", "must-not-be-exposed")
                session.add(
                    SecretRef(
                        org_id="org_cp22_a",
                        key="legacy_broad_env_ref",
                        backend="env",
                        locator="env:DRILLAI_DATABASE_URL",
                        is_active=True,
                    )
                )
                await session.flush()
                assert (
                    await resolve_notification_secret_async(
                        session,
                        "org_cp22_a",
                        "secret_ref:legacy_broad_env_ref",
                    )
                    is None
                )
                with pytest.raises(ValidationFailed) as invalid_connector_env:
                    await resolve_secret_refs(
                        session,
                        "org_cp22_a",
                        {"password": "secret_ref:legacy_broad_env_ref"},
                    )
                assert invalid_connector_env.value.details["reason"] == "invalid_secret_ref"
                assert "must-not-be-exposed" not in str(invalid_connector_env.value)

                notif_val = await resolve_notification_secret_async(
                    session,
                    "org_cp22_a",
                    "secret_ref:rig_witsml_pass",
                )
                assert notif_val == secret_clear

                # Cross-tenant resolution from org_cp22_b fails closed
                with pytest.raises(ValidationFailed):
                    await resolve_secret_refs(
                        session,
                        "org_cp22_b",
                        {"password": "secret_ref:rig_witsml_pass"},
                    )

            # 5. Rotate master key to v2 and rotate the stored SecretRef via API
            monkeypatch.setenv("DRILLAI_SECRET_MASTER_KEY", master_v2)
            monkeypatch.setenv("DRILLAI_SECRET_ACTIVE_KEY_VERSION", "2")
            monkeypatch.setenv("DRILLAI_SECRET_PREVIOUS_MASTER_KEYS", f"v1:{master_v1}")
            reset_settings_cache()

            r_list_a = await admin_a_client.get("/api/v1/platform/secrets")
            assert r_list_a.status_code == 200
            encrypted_item = next(
                item for item in r_list_a.json()["items"] if item["key"] == "rig_witsml_pass"
            )
            assert encrypted_item["needs_key_rotation"] is True

            r_rotate = await admin_a_client.post(
                "/api/v1/platform/secrets/rig_witsml_pass/rotate",
                headers={"X-CSRF-Token": csrf_admin_a},
                json={},
            )
            assert r_rotate.status_code == 200
            assert r_rotate.json()["key_version"] == 2
            assert r_rotate.json()["needs_key_rotation"] is False

            # 6. Revoke secret and verify resolution fails closed and AuditLog has zero plaintext
            r_del = await admin_a_client.delete(
                "/api/v1/platform/secrets/rig_witsml_pass",
                headers={"X-CSRF-Token": csrf_admin_a},
            )
            assert r_del.status_code == 200
            assert r_del.json()["is_active"] is False

            async with app.state.database.session() as session:
                with pytest.raises(ValidationFailed):
                    await resolve_secret_refs(
                        session,
                        "org_cp22_a",
                        {"password": "secret_ref:rig_witsml_pass"},
                    )
                audits = (
                    await session.execute(
                        select(AuditLog).where(AuditLog.org_id == "org_cp22_a")
                    )
                ).scalars().all()
                actions = {a.action for a in audits}
                assert "security.secret_ref.upsert" in actions
                assert "security.secret_ref.rotate" in actions
                assert "security.secret_ref.revoke" in actions
                for a in audits:
                    assert secret_clear not in str(a.details)
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp22_03_password_kdf_hardening_argon2id_scrypt_dos_bounds_and_rehash_on_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify scrypt (N=2^15) + Argon2id, legacy N=16384 rehash-on-login, DoS parameter bounds, and benchmarks."""
    # 1. Default scrypt uses OWASP N=2^15 (32768), r=8, p=1 (~32 MiB)
    h_scrypt = hash_password("DrillAI-Hardened-Pass!2026", scheme="scrypt")
    assert h_scrypt.startswith(f"scrypt$n={SCRYPT_DEFAULT_N},r=8,p=1$")
    assert verify_password("DrillAI-Hardened-Pass!2026", h_scrypt) is True
    assert verify_password("Wrong-Pass!2026", h_scrypt) is False
    assert needs_rehash(h_scrypt) is False

    # 2. Argon2id uses OWASP m=19456 KiB (~19 MiB), t=2, p=1
    h_argon = hash_password("DrillAI-Hardened-Pass!2026", scheme="argon2id")
    assert h_argon.startswith(f"$argon2id$v=19$m={ARGON2ID_DEFAULT_MEMORY_KIB},t=2,p=1$")
    assert verify_password("DrillAI-Hardened-Pass!2026", h_argon) is True
    assert verify_password("Wrong-Pass!2026", h_argon) is False
    assert needs_rehash(h_argon) is False
    assert needs_rehash("$argon2id$v=19$m=8192,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2g") is True

    # 3. Malformed or dangerously extreme parameters are rejected BEFORE KDF execution (< 5 ms)
    crafted_hashes = [
        "scrypt$n=1073741824,r=64,p=16$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "scrypt$n=30000,r=8,p=1$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "scrypt$n=-16384,r=8,p=1$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "scrypt$n=65536,r=32,p=4$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "$argon2id$v=19$m=4194304,t=100,p=32$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "$argon2id$v=19$m=-1024,t=2,p=1$c2FsdHNhbHRzYWx0c2FsdA$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g",
    ]
    t0 = time.perf_counter()
    for bad_hash in crafted_hashes:
        assert verify_password("any-password", bad_hash) is False
    assert verify_password("x" * 2048, h_scrypt) is False
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 25.0, f"DoS parameter guard took too long: {elapsed_ms:.2f} ms"

    # 4. Bounded concurrent async verification + benchmark_password_kdf
    results = await asyncio.gather(
        *[verify_password_async("DrillAI-Hardened-Pass!2026", h_scrypt) for _ in range(6)],
        *[verify_password_async("DrillAI-Hardened-Pass!2026", h_argon) for _ in range(6)],
    )
    assert all(r is True for r in results)

    bench = benchmark_password_kdf(samples=3)
    assert bench["scrypt"]["n"] == 32768
    assert bench["scrypt"]["memory_mib"] == 32.0
    assert bench["scrypt"]["p50_ms"] > 0.0
    assert bench["argon2id"]["memory_kib"] == 19456
    assert bench["argon2id"]["p50_ms"] > 0.0

    # 5. Legacy N=16384 hash verifies and automatically upgrades to N=32768 on login
    legacy_hash = hash_password("LegacySupervisorPass!2026", scrypt_n=2**14)
    assert legacy_hash.startswith("scrypt$n=16384,r=8,p=1$")
    assert verify_password("LegacySupervisorPass!2026", legacy_hash) is True
    assert needs_rehash(legacy_hash) is True

    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'cp22_rehash.db').as_posix()}"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    reset_settings_cache()

    app = create_app(Settings(database_url=db_url, auth_enabled=True))
    await app.state.database.create_all()
    try:
        async with app.state.database.session() as session:
            org = Organization(id="org_rehash", slug="rehash-org", name="Rehash Org")
            user = User(
                id="usr_legacy_hash",
                email="legacy@rehash-org.local",
                display_name="Legacy Hash User",
                password_hash=legacy_hash,
                is_active=True,
            )
            session.add_all([org, user])
            await session.flush()
            session.add(Membership(org_id=org.id, user_id=user.id, role_key="engineer"))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_login = await client.post(
                "/api/v1/auth/login",
                json={"email": "legacy@rehash-org.local", "password": "LegacySupervisorPass!2026"},
            )
            assert r_login.status_code == 200

        async with app.state.database.session() as session:
            upgraded_user = (
                await session.execute(select(User).where(User.id == "usr_legacy_hash"))
            ).scalar_one()
            assert upgraded_user.password_hash is not None
            assert upgraded_user.password_hash.startswith(f"scrypt$n={SCRYPT_DEFAULT_N},r=8,p=1$")
            assert needs_rehash(upgraded_user.password_hash) is False
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp22_04_production_configuration_guards_and_security_model_doc() -> None:
    """Verify production fail-closed startup guards, TrustedHostMiddleware, and docs/SECURITY_MODEL.md."""
    base_prod_kwargs = {
        "environment": "production",
        "auth_enabled": True,
        "debug": False,
        "cors_origins": "https://cockpit.drillai.example.com",
        "allowed_hosts": "cockpit.drillai.example.com,api.drillai.example.com",
        "e2e_faults": False,
        "connector_allow_loopback": False,
        "session_cookie_secure": True,
        "session_csrf_secret": "prod-csrf-secret-at-least-32-characters-long-2026!!",
        "database_url": "postgresql+asyncpg://drillai_prod:StrongVaultPass99!@db.internal:5432/drillai",
    }

    # 1. Wildcard allowed_hosts refused in production
    with pytest.raises(ConfigurationError, match="DRILLAI_ALLOWED_HOSTS"):
        create_app(Settings(**{**base_prod_kwargs, "allowed_hosts": "*"}))

    # 2. Insecure session cookie refused in production
    with pytest.raises(ConfigurationError, match="DRILLAI_SESSION_COOKIE_SECURE"):
        create_app(Settings(**{**base_prod_kwargs, "session_cookie_secure": False}))

    # 3. Default or short CSRF secret refused in production
    with pytest.raises(ConfigurationError, match="DRILLAI_SESSION_CSRF_SECRET"):
        create_app(
            Settings(
                **{
                    **base_prod_kwargs,
                    "session_csrf_secret": "drillai-dev-csrf-synchronizer-key",
                }
            )
        )

    # 4. Short master key when secret_backend="database" refused in production
    with pytest.raises(ConfigurationError, match="DRILLAI_SECRET_MASTER_KEY"):
        create_app(
            Settings(
                **{
                    **base_prod_kwargs,
                    "secret_backend": "database",
                    "secret_master_key": "too-short",
                }
            )
        )

    # 5. Placeholder database password refused in production
    with pytest.raises(ConfigurationError, match="DRILLAI_DATABASE_URL"):
        create_app(
            Settings(
                **{
                    **base_prod_kwargs,
                    "database_url": "postgresql+asyncpg://drillai:change_me_in_production@postgres:5432/drillai",
                }
            )
        )

    # 6. TrustedHostMiddleware rejects untrusted Host headers when allowed_hosts is configured
    app = create_app(
        Settings(
            environment="staging",
            allowed_hosts="api.drillai.example.com",
            cors_origins="https://cockpit.drillai.example.com",
        )
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://evil.attacker.example") as bad_client:
            r_bad_host = await bad_client.get("/api/v1/health")
            assert r_bad_host.status_code == 400
        async with httpx.AsyncClient(transport=transport, base_url="http://api.drillai.example.com") as ok_client:
            r_ok_host = await ok_client.get("/api/v1/health")
            assert r_ok_host.status_code == 200
    finally:
        await app.state.database.dispose()

    # 7. docs/SECURITY_MODEL.md exists and covers required architecture sections
    sec_doc = REPO_ROOT / "docs" / "SECURITY_MODEL.md"
    assert sec_doc.is_file()
    sec_text = sec_doc.read_text(encoding="utf-8")
    for required_topic in (
        "Authentication Architecture",
        "Password Hashing",
        "Argon2id",
        "scrypt",
        "Session Security, CSRF & WebSocket Authentication",
        "Multi-Worker Brute-Force Rate Limiting",
        "Scoped API Tokens",
        "AES-256-GCM",
        "Outbound SSRF & Egress Policy Defense",
        "Production Startup Guards",
    ):
        assert required_topic in sec_text, f"Missing {required_topic!r} in docs/SECURITY_MODEL.md"
