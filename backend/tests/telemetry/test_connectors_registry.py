"""Tests for Checkpoint A: production connector registry, secret redaction, SSRF protection,
RBAC/tenant isolation, optimistic concurrency, idempotency, and audit ledger integrity."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.fixtures.fabric import Fabric

from drillai.api.app import create_app
from drillai.core.config import Settings, get_settings, reset_settings_cache
from drillai.core.errors import ConfigurationError, ValidationFailed
from drillai.db.models import AuditLog, Base, Organization, Project, SecretRef, Well, Wellbore
from drillai.telemetry.connectors import (
    redact_sensitive_text,
    validate_connector_config,
    validate_connector_endpoint,
    validate_no_plaintext_secrets,
    validate_secret_refs,
)


def _headers(roles: str = "engineer", idem: str | None = None) -> dict[str, str]:
    hdrs = {"X-Dev-Roles": roles}
    if idem:
        hdrs["Idempotency-Key"] = idem
    return hdrs


def _sample_mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "kPa",
        },
        {
            "source_mnemonic": "WOB",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "kN",
        },
    ]


def test_plaintext_secrets_and_tls_disable_are_refused():
    with pytest.raises(ValidationFailed) as exc_info:
        validate_no_plaintext_secrets({"password": "super-secret-value"})
    assert exc_info.value.details["reason"] == "plaintext_secret_forbidden"

    with pytest.raises(ValidationFailed) as exc_info:
        validate_no_plaintext_secrets({"nested": {"api_key": "abc-123"}})
    assert exc_info.value.details["reason"] == "plaintext_secret_forbidden"

    with pytest.raises(ValidationFailed) as exc_info:
        validate_connector_endpoint(
            "https://rig_user:rig_pass@witsml.example.com/store",
            "witsml.1.4.1.1.soap_http",
            allowed_hosts=["witsml.example.com"],
        )
    assert exc_info.value.details["reason"] == "plaintext_secret_forbidden"

    with pytest.raises(ValidationFailed) as exc_info:
        validate_connector_config(
            {"tls_verify": False, "channel_mappings": _sample_mappings()},
            protocol_profile="witsml.1.4.1.1.soap_http",
        )
    assert exc_info.value.details["reason"] == "tls_verify_required"

    with pytest.raises(ValidationFailed) as exc_info:
        validate_secret_refs({"password": "plain-password"})
    assert exc_info.value.details["reason"] == "invalid_secret_ref"

    valid_refs = validate_secret_refs(
        {
            "username": "env:DRILLAI_SECRET_WITSML_USER",
            "password": "secret_ref:rig1_witsml_pass",
        }
    )
    assert valid_refs["username"] == "env:DRILLAI_SECRET_WITSML_USER"
    assert valid_refs["password"] == "secret_ref:rig1_witsml_pass"


def test_ssrf_guard_blocks_metadata_loopback_and_private_networks():
    for blocked_url in (
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://127.0.0.1:8443/witsml",
        "https://localhost:8443/witsml",
        "https://10.12.34.56/witsml",
        "https://192.168.1.20/witsml",
        "https://172.16.5.4/witsml",
        "https://0.0.0.0/witsml",
    ):
        with pytest.raises(ValidationFailed) as exc_info:
            validate_connector_endpoint(
                blocked_url,
                "witsml.1.4.1.1.soap_http",
                allow_loopback=False,
                allowed_hosts=[],
            )
        assert exc_info.value.details["reason"] == "ssrf_blocked", blocked_url

    # Even when allow_loopback=True in non-production, cloud metadata endpoints remain blocked
    with pytest.raises(ValidationFailed) as exc_info:
        validate_connector_endpoint(
            "http://169.254.169.254/latest/meta-data/",
            "witsml.1.4.1.1.soap_http",
            allow_loopback=True,
            is_production=False,
        )
    assert exc_info.value.details["reason"] == "ssrf_blocked"

    # Loopback is permitted only when allow_loopback=True and not in production
    allowed = validate_connector_endpoint(
        "http://127.0.0.1:18080/witsml/store",
        "witsml.1.4.1.1.soap_http",
        allow_loopback=True,
        is_production=False,
    )
    assert allowed == "http://127.0.0.1:18080/witsml/store"

    # Production refuses loopback even if allow_loopback=True is passed
    with pytest.raises(ValidationFailed):
        validate_connector_endpoint(
            "https://127.0.0.1:18080/witsml/store",
            "witsml.1.4.1.1.soap_http",
            allow_loopback=True,
            is_production=True,
        )


def test_production_app_refuses_connector_allow_loopback():
    settings = Settings(
        environment="production",
        auth_enabled=True,
        cors_origins="https://cockpit.example.com",
        connector_allow_loopback=True,
    )
    with pytest.raises(ConfigurationError, match="DRILLAI_CONNECTOR_ALLOW_LOOPBACK"):
        create_app(settings=settings)


def test_sensitive_text_redaction_scrubs_secrets_and_headers():
    raw = (
        "Failed connecting to https://rig_admin:MyTopSecret99@rig.example.com/soap "
        "with header Authorization: Bearer tok_live_9876543210abcdef and pass MyTopSecret99"
    )
    redacted = redact_sensitive_text(raw, {"password": "MyTopSecret99"})
    assert redacted is not None
    assert "MyTopSecret99" not in redacted
    assert "tok_live_9876543210abcdef" not in redacted
    assert "********" in redacted


async def test_connector_crud_lifecycle_rbac_and_audit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DRILLAI_SECRET_TEST_PASS", "S3cr3tRigPass!")
    monkeypatch.setenv("DRILLAI_SECRET_TEST_USER", "witsml_operator")
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cnc_rbac.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_cnc_test_01"
    wellbore_id = "wbr_cnc_test_01"
    async with app.state.database.session() as session:
        session.add(
            Organization(
                id=org_id,
                slug=settings.dev_org_slug,
                name="Demo Operator",
                kind="operator",
            )
        )
        session.add(Project(id="prj_cnc_01", org_id=org_id, name="Connector Project"))
        session.add(
            Well(
                id=well_id,
                org_id=org_id,
                project_id="prj_cnc_01",
                name="CNC-WELL-01",
                well_type="development_producer",
                status="drilling",
            )
        )
        session.add(
            Wellbore(
                id=wellbore_id,
                org_id=org_id,
                well_id=well_id,
                name="CNC-WB-01",
                purpose="main",
                status="active",
                is_active=True,
            )
        )
        session.add(
            SecretRef(
                id="srf_test_user",
                org_id=org_id,
                key="rig_witsml_user",
                backend="env",
                locator="env:DRILLAI_SECRET_TEST_USER",
                is_active=True,
            )
        )
        await session.commit()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. Profiles catalogue is readable by viewer
            profiles_res = await client.get(
                "/api/v1/connectors/profiles", headers=_headers("viewer")
            )
            assert profiles_res.status_code == 200
            profiles_data = profiles_res.json()
            profile_keys = [p["profile"] for p in profiles_data["profiles"]]
            assert "synthetic.v1" in profile_keys
            assert "witsml.1.4.1.1.soap_http" in profile_keys
            assert "etp.1.2.json_ws" in profile_keys

            # 2. Viewer cannot create a connector (403)
            viewer_create = await client.post(
                "/api/v1/connectors",
                headers=_headers("viewer"),
                json={
                    "key": "rig-synth-01",
                    "name": "Rig Synthetic Collector",
                    "protocol_profile": "synthetic.v1",
                    "well_id": well_id,
                    "config": {"channel_mappings": _sample_mappings()},
                },
            )
            assert viewer_create.status_code == 403

            # 3. Engineer creates a synthetic connector with idempotency key
            create_payload = {
                "key": "rig-synth-01",
                "name": "Rig Synthetic Collector",
                "protocol_profile": "synthetic.v1",
                "well_id": well_id,
                "wellbore_id": wellbore_id,
                "description": "Commissioning synthetic telemetry source",
                "config": {
                    "poll_interval_seconds": 1.0,
                    "channel_mappings": _sample_mappings(),
                    "plan": {
                        "spp": [[0.0, 21500.0], [5.0, 22100.0]],
                        "wob": [[0.0, 95.0], [5.0, 102.0]],
                    },
                },
                "secret_refs": {
                    "username": "secret_ref:rig_witsml_user",
                    "password": "env:DRILLAI_SECRET_TEST_PASS",
                },
            }
            created_res = await client.post(
                "/api/v1/connectors",
                headers=_headers("engineer", idem="idem-create-cnc-1"),
                json=create_payload,
            )
            assert created_res.status_code == 201, created_res.text
            created = created_res.json()
            connector_id = created["id"]
            assert created["key"] == "rig-synth-01"
            assert created["is_synthetic"] is True
            assert created["source_classification"] == "synthetic_test_only"
            assert created["desired_state"] == "disabled"
            assert created["status"] == "configured"
            assert created["health"]["is_live"] is False
            assert created["health"]["health_state"] == "disabled"
            assert created["config_version"] == 1
            # Secret refs are masked; plaintext values never appear anywhere in the payload
            assert "S3cr3tRigPass!" not in created_res.text
            assert "witsml_operator" not in created_res.text
            assert created["secret_refs"]["password"]["masked_value"] == "********"
            assert created["secret_refs"]["username"]["masked_value"] == "********"

            # Replaying with the same Idempotency-Key returns the exact same connector
            replay_res = await client.post(
                "/api/v1/connectors",
                headers=_headers("engineer", idem="idem-create-cnc-1"),
                json=create_payload,
            )
            assert replay_res.status_code == 201
            assert replay_res.json()["id"] == connector_id

            # 4. Viewer can inspect and list connectors, but allowed_actions is empty
            viewer_get = await client.get(
                f"/api/v1/connectors/{connector_id}", headers=_headers("viewer")
            )
            assert viewer_get.status_code == 200
            assert viewer_get.json()["allowed_actions"] == []

            # 5. Test connection and preview on synthetic connector without persisting points
            test_res = await client.post(
                f"/api/v1/connectors/{connector_id}/test", headers=_headers("engineer")
            )
            assert test_res.status_code == 200
            test_body = test_res.json()
            assert test_body["ok"] is True
            assert len(test_body["discovered_channels"]) == 2

            preview_res = await client.post(
                f"/api/v1/connectors/{connector_id}/preview",
                headers=_headers("engineer"),
                json={"sample_limit": 10},
            )
            assert preview_res.status_code == 200
            preview_body = preview_res.json()
            assert preview_body["sample_count"] == 2
            assert {s["channel_key"] for s in preview_body["samples"]} == {"spp", "wob"}

            # 6. Update configuration with optimistic concurrency (409 on stale version)
            stale_patch = await client.patch(
                f"/api/v1/connectors/{connector_id}",
                headers=_headers("engineer"),
                json={
                    "expected_config_version": 99,
                    "reason": "stale version test",
                    "name": "Should Fail",
                },
            )
            assert stale_patch.status_code == 409

            # Updating without secret_refs preserves existing secret_refs
            valid_patch = await client.patch(
                f"/api/v1/connectors/{connector_id}",
                headers=_headers("engineer", idem="idem-patch-cnc-1"),
                json={
                    "expected_config_version": 1,
                    "reason": "tune polling interval to 1.5s",
                    "name": "Rig Synthetic Collector v2",
                    "config": {
                        "poll_interval_seconds": 1.5,
                        "channel_mappings": _sample_mappings(),
                    },
                },
            )
            assert valid_patch.status_code == 200
            patched = valid_patch.json()
            assert patched["config_version"] == 2
            assert patched["name"] == "Rig Synthetic Collector v2"
            assert "password" in patched["secret_refs"]
            assert "username" in patched["secret_refs"]

            # 7. Governed transitions: start -> stop (requires reason) -> restart -> disable
            start_res = await client.post(
                f"/api/v1/connectors/{connector_id}/start",
                headers=_headers("engineer", idem="idem-start-1"),
                json={"reason": "commission synthetic collector"},
            )
            assert start_res.status_code == 200
            started = start_res.json()
            assert started["desired_state"] == "enabled"
            assert started["is_enabled"] is True
            # Starting does NOT optimistically claim status='running' or is_live=True before a worker polls!
            assert started["status"] == "starting"
            assert started["health"]["is_live"] is False
            assert started["health"]["health_state"] == "starting"

            # Stop without reason is refused (422)
            stop_no_reason = await client.post(
                f"/api/v1/connectors/{connector_id}/stop",
                headers=_headers("engineer"),
                json={},
            )
            assert stop_no_reason.status_code == 422

            stop_res = await client.post(
                f"/api/v1/connectors/{connector_id}/stop",
                headers=_headers("engineer"),
                json={"reason": "pause collector for sensor calibration"},
            )
            assert stop_res.status_code == 200
            stopped = stop_res.json()
            assert stopped["desired_state"] == "stopped"
            assert stopped["status"] == "stopped"
            assert stopped["is_enabled"] is False
            assert stopped["health"]["is_live"] is False

            restart_res = await client.post(
                f"/api/v1/connectors/{connector_id}/restart",
                headers=_headers("engineer"),
                json={"reason": "resume after calibration"},
            )
            assert restart_res.status_code == 200
            restarted = restart_res.json()
            assert restarted["desired_state"] == "enabled"
            assert restarted["status"] == "starting"
            assert restarted["health"]["reconnect_count"] == 1

            disable_res = await client.post(
                f"/api/v1/connectors/{connector_id}/disable",
                headers=_headers("engineer"),
                json={"reason": "retire collector after section TD"},
            )
            assert disable_res.status_code == 200
            disabled = disable_res.json()
            assert disabled["desired_state"] == "disabled"
            assert disabled["status"] == "disabled"
            assert disabled["is_enabled"] is False

            # 8. Inspect run ledger and mappings
            runs_res = await client.get(
                f"/api/v1/connectors/{connector_id}/runs", headers=_headers("viewer")
            )
            assert runs_res.status_code == 200
            assert runs_res.json()["total"] >= 1

            mappings_res = await client.get(
                f"/api/v1/connectors/{connector_id}/mappings", headers=_headers("viewer")
            )
            assert mappings_res.status_code == 200
            assert len(mappings_res.json()["channel_mappings"]) == 2

            # 9. Foreign/unknown well or connector returns 404 without leaking existence
            foreign_get = await client.get(
                "/api/v1/connectors/cnc_foreign_does_not_exist", headers=_headers("engineer")
            )
            assert foreign_get.status_code == 404

        # 10. Verify AuditLog entries exist and contain zero plaintext secret material
        async with app.state.database.session() as session:
            audit_rows = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(
                            AuditLog.org_id == org_id,
                            AuditLog.resource_kind == "connector",
                            AuditLog.resource_id == connector_id,
                        )
                        .order_by(AuditLog.occurred_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            actions = [row.action for row in audit_rows]
            assert "connector.manage" in actions
            assert "connector.test" in actions
            assert "connector.control" in actions
            for row in audit_rows:
                serialized = f"{row.before} {row.after} {row.details}"
                assert "S3cr3tRigPass!" not in serialized
                assert "witsml_operator" not in serialized
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_connector_cross_tenant_isolation(fabric: Fabric):
    """Tenant B cannot read, mutate, test, or bind a connector to Tenant A's well or connector id."""
    create_res = await fabric.http.post(
        "/api/v1/connectors",
        headers=fabric.alpha.headers,
        json={
            "key": "alpha-synth-01",
            "name": "Alpha Collector",
            "protocol_profile": "synthetic.v1",
            "well_id": fabric.alpha.well_id,
            "config": {"channel_mappings": _sample_mappings()},
        },
    )
    assert create_res.status_code == 201, create_res.text
    alpha_connector_id = create_res.json()["id"]

    # Bravo cannot read Alpha's connector (404, not 403 — no existence leak)
    bravo_get = await fabric.http.get(
        f"/api/v1/connectors/{alpha_connector_id}", headers=fabric.bravo.headers
    )
    assert bravo_get.status_code == 404

    # Bravo cannot create a connector targeting Alpha's well (404)
    bravo_cross_well = await fabric.http.post(
        "/api/v1/connectors",
        headers=fabric.bravo.headers,
        json={
            "key": "bravo-steal-alpha",
            "name": "Bravo Cross Well Attempt",
            "protocol_profile": "synthetic.v1",
            "well_id": fabric.alpha.well_id,
            "config": {"channel_mappings": _sample_mappings()},
        },
    )
    assert bravo_cross_well.status_code == 404

    # Bravo's list does not include Alpha's connector
    bravo_list = await fabric.http.get("/api/v1/connectors", headers=fabric.bravo.headers)
    assert bravo_list.status_code == 200
    assert bravo_list.json()["total"] == 0
