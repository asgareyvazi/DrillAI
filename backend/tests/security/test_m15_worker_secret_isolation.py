"""Regression tests for application-scoped connector and notification secret resolution."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from drillai.api.app import create_app
from drillai.core.config import Settings, reset_settings_cache
from drillai.core.errors import ConfigurationError, ValidationFailed
from drillai.db.models import Organization, SecretRef
from drillai.security.secrets import encrypt_secret_value
from drillai.telemetry.connectors import resolve_secret_refs
from drillai.telemetry.worker import ConnectorWorker
from drillai.workflow.notifications import resolve_notification_secret_async


@pytest.mark.asyncio
async def test_two_applications_resolve_only_their_own_keys_concurrently(monkeypatch):
    key_a = "tenant-a-master-key-is-long-enough-123"
    key_b = "tenant-b-master-key-is-long-enough-456"
    monkeypatch.setenv("DRILLAI_SECRET_MASTER_KEY", key_a)
    reset_settings_cache()
    configs = [
        Settings(environment="test", database_url="sqlite+aiosqlite:///:memory:", secret_master_key=key)
        for key in (key_a, key_b)
    ]
    apps = [create_app(config) for config in configs]
    envelopes = [
        encrypt_secret_value(value, org_id="org_shared", secret_key="shared", settings=config)[0]
        for value, config in zip(("credential-a", "credential-b"), configs, strict=True)
    ]
    try:
        for app, envelope in zip(apps, envelopes, strict=True):
            await app.state.database.create_all()
            async with app.state.database.session() as session:
                session.add(Organization(id="org_shared", slug="shared", name="Shared"))
                await session.flush()
                session.add(SecretRef(org_id="org_shared", key="shared", backend="db_encrypted", locator=envelope))

        async def resolve(app, expected):
            for _ in range(5):
                async with app.state.database.session() as session:
                    assert (await resolve_secret_refs(session, "org_shared", {"password": "secret_ref:shared"}))["password"] == expected
                    assert await resolve_notification_secret_async(session, "org_shared", "secret_ref:shared") == expected
                await asyncio.sleep(0)

        await asyncio.gather(resolve(apps[0], "credential-a"), resolve(apps[1], "credential-b"))
        # Even identical tenant and key labels must not make the other app's ciphertext usable.
        for index in (0, 1):
            async with apps[index].state.database.session() as session:
                row = (await session.execute(select(SecretRef))).scalar_one()
                row.locator = envelopes[1 - index]
                await session.flush()
                with pytest.raises(ValidationFailed):
                    await resolve_secret_refs(session, "org_shared", {"password": "secret_ref:shared"})
                assert await resolve_notification_secret_async(session, "org_shared", "secret_ref:shared") is None
    finally:
        for app in apps:
            await app.state.database.dispose()
        reset_settings_cache()


@pytest.mark.asyncio
async def test_unscoped_session_must_not_fall_back_to_global_master_key(monkeypatch):
    key = "process-global-master-key-is-long-enough"
    monkeypatch.setenv("DRILLAI_SECRET_MASTER_KEY", key)
    reset_settings_cache()
    app = create_app(Settings(environment="test", database_url="sqlite+aiosqlite:///:memory:", secret_master_key=key))
    try:
        await app.state.database.create_all()
        envelope, _ = encrypt_secret_value("credential", org_id="org_a", secret_key="shared", settings=app.state.settings)
        async with app.state.database.session() as session:
            session.add(Organization(id="org_a", slug="a", name="A"))
            await session.flush()
            session.add(SecretRef(org_id="org_a", key="shared", backend="db_encrypted", locator=envelope))
        factory = async_sessionmaker(app.state.database.engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(ConfigurationError):
                await resolve_secret_refs(session, "org_a", {"password": "secret_ref:shared"})
            with pytest.raises(ConfigurationError):
                await resolve_notification_secret_async(session, "org_a", "secret_ref:shared")
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


def test_connector_worker_uses_own_database_configuration(monkeypatch):
    monkeypatch.setenv("DRILLAI_CONNECTOR_WORKER_MAX_CONCURRENCY", "1")
    reset_settings_cache()
    cfg = Settings(environment="test", connector_worker_max_concurrency=3)
    app = create_app(cfg)
    assert ConnectorWorker(app.state.database).max_concurrency == 3
    reset_settings_cache()


@pytest.mark.asyncio
async def test_key_rotation_missing_key_and_revocation_fail_closed():
    cfg = Settings(environment="test", database_url="sqlite+aiosqlite:///:memory:", secret_master_key="old-master-key-long-enough-123456789")
    app = create_app(cfg)
    try:
        await app.state.database.create_all()
        envelope, _ = encrypt_secret_value("old-credential", org_id="org_a", secret_key="shared", settings=cfg)
        async with app.state.database.session() as session:
            session.add(Organization(id="org_a", slug="a", name="A"))
            await session.flush()
            session.add(SecretRef(org_id="org_a", key="shared", backend="db_encrypted", locator=envelope))
        new_cfg = cfg.model_copy(update={"secret_master_key": "new-master-key-long-enough-123456789", "secret_active_key_version": 2})
        app.state.database.settings = new_cfg
        async with app.state.database.session() as session:
            with pytest.raises(ValidationFailed):
                await resolve_secret_refs(session, "org_a", {"password": "secret_ref:shared"})
            assert await resolve_notification_secret_async(session, "org_a", "secret_ref:shared") is None
            row = (await session.execute(select(SecretRef))).scalar_one()
            row.locator, _ = encrypt_secret_value("new-credential", org_id="org_a", secret_key="shared", settings=new_cfg)
            await session.flush()
            assert (await resolve_secret_refs(session, "org_a", {"password": "secret_ref:shared"}))["password"] == "new-credential"
            row.is_active = False
            await session.flush()
            with pytest.raises(ValidationFailed):
                await resolve_secret_refs(session, "org_a", {"password": "secret_ref:shared"})
            assert await resolve_notification_secret_async(session, "org_a", "secret_ref:shared") is None
        app.state.database.settings = new_cfg.model_copy(update={"secret_master_key": ""})
        # The resolver's existing contract reports invalid key configuration as unresolved.
        async with app.state.database.session() as session:
            row = (await session.execute(select(SecretRef))).scalar_one()
            row.is_active = True
            await session.flush()
            with pytest.raises(ValidationFailed):
                await resolve_secret_refs(session, "org_a", {"password": "secret_ref:shared"})
            assert await resolve_notification_secret_async(session, "org_a", "secret_ref:shared") is None
    finally:
        await app.state.database.dispose()
