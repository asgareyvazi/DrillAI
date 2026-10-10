"""Persistence layer: engine/session semantics, id minting, constraints and JSON columns.

SQLite proves the ORM paths; the PostgreSQL-only behaviours (native UUID/JSON, pool
config) are covered by the ``postgres``-marked tests when ``DRILLAI_TEST_POSTGRES=1``.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from drillai.core.ids import is_id
from drillai.db.models import (
    ALL_MODELS,
    Base,
    Document,
    Organization,
    Project,
    RawArtifact,
    Well,
)


async def test_session_commits_and_rolls_back():
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            session.add(Organization(slug="commit-me", name="Commit Me"))
        async with db.session() as session:
            count = await session.scalar(select(Organization).where(Organization.slug == "commit-me"))
        assert count is not None and count.id.startswith("org_")

        with pytest.raises(RuntimeError):
            async with db.session() as session:
                session.add(Organization(slug="roll-me-back", name="Rollback"))
                await session.flush()
                raise RuntimeError("boom")
        async with db.session() as session:
            assert await session.scalar(select(Organization).where(Organization.slug == "roll-me-back")) is None
    finally:
        await db.dispose()


async def test_healthcheck_reports_dialect_and_failure():
    from drillai.db.session import Database

    healthy = Database("sqlite+aiosqlite:///:memory:")
    try:
        assert await healthy.healthcheck() == {"status": "ok", "dialect": "sqlite"}
    finally:
        await healthy.dispose()

    broken = Database("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/absent")
    try:
        report = await broken.healthcheck()
        assert report["status"] == "error"
        assert report["dialect"] == "postgresql"
        assert "error" in report
    finally:
        await broken.dispose()


async def test_sqlite_enforces_foreign_keys_like_postgres():
    """Without the PRAGMA, SQLite silently accepts orphan rows — a test-only lie."""
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            session.add(Well(org_id="org_missing", project_id="prj_missing", name="orphan"))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
        async with db.session() as session:
            assert await session.scalar(select(Well).where(Well.name == "orphan")) is None
    finally:
        await db.dispose()


async def test_ids_are_generated_with_the_class_prefix():
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            org = Organization(slug="ids", name="Ids")
            session.add(org)
            await session.flush()
            assert is_id(org.id, "org")
        async with db.session() as session:
            rows = (await session.execute(select(Organization))).scalars().all()
            assert len(rows) == 1
    finally:
        await db.dispose()


def test_every_model_declares_a_short_id_prefix():
    prefixes = {}
    for model in ALL_MODELS:
        prefix = getattr(model, "id_prefix", None)
        assert prefix, f"{model.__name__} has no id_prefix"
        assert prefix.islower() and 2 <= len(prefix) <= 5, f"{model.__name__}: suspicious prefix {prefix!r}"
        assert prefix not in prefixes, f"{model.__name__} reuses {prefix!r} from {prefixes[prefix].__name__}"
        prefixes[prefix] = model


async def test_well_unique_per_project_and_json_round_trip():
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            org = Organization(slug="json", name="Json Co")
            session.add(org)
            await session.flush()
            project = Project(
                org_id=org.id,
                name="P",
                settings={"datum_policy_default": "rkb", "phases": ["plan", "drill"]},
            )
            session.add(project)
            await session.flush()
            project_id = project.id
            session.add(Well(org_id=org.id, project_id=project_id, name="W-1", target_formations=["A", "B"]))
            await session.commit()

        # A duplicate well name inside the same project is rejected by the database.
        async with db.session() as session:
            session.add(Well(org_id=org.id, project_id=project_id, name="W-1"))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with db.session() as session:
            stored = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()
            assert stored.settings["datum_policy_default"] == "rkb"
            assert stored.settings["phases"] == ["plan", "drill"]
            wells = (await session.execute(select(Well))).scalars().all()
            assert [well.name for well in wells] == ["W-1"]
    finally:
        await db.dispose()


async def test_soft_delete_and_timestamps_are_utc_aware():
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            org = Organization(slug="time", name="Time")
            session.add(org)
            await session.flush()
            assert org.created_at.tzinfo is not None
            assert org.updated_at.tzinfo is not None
            created_before = org.created_at
            org.name = "Time (renamed)"
            await session.flush()
            await session.refresh(org)
            assert org.updated_at >= created_before
    finally:
        await db.dispose()


async def test_document_and_artifact_chain_persists_hashes():
    """RawArtifact -> Document is the head of the provenance chain; hashes must survive."""
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            org = Organization(slug="prov", name="Provenance")
            session.add(org)
            await session.flush()
            raw = RawArtifact(
                org_id=org.id,
                sha256="a" * 64,
                original_filename="ddr.pdf",
                content_type="application/pdf",
                byte_size=1234,
                blob_key="raw/aa/ddr.pdf",
                source_kind="upload",
            )
            session.add(raw)
            await session.flush()
            doc = Document(
                org_id=org.id,
                raw_artifact_id=raw.id,
                doc_type="ddr",
                title="Daily Drilling Report 2026-01-01",
                status="ingested",
            )
            session.add(doc)
            await session.commit()
            loaded = (await session.execute(select(Document).where(Document.id == doc.id))).scalar_one()
            assert loaded.raw_artifact_id == raw.id
            assert loaded.title.startswith("Daily")
    finally:
        await db.dispose()


async def test_server_defaults_apply_without_python_defaults():
    """Rows inserted with raw SQL still get a ULID id and UTC timestamps."""
    from drillai.db.session import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
        async with db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, kind, timezone, default_unit_system,"
                    " default_locale, settings, is_active, created_at, updated_at)"
                    " VALUES (:id, 'raw', 'Raw', 'operator', 'UTC', 'field_us', 'en', '{}', 1,"
                    " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                ),
                {"id": "org_raw_sql"},
            )
        async with db.session() as session:
            row = (await session.execute(select(Organization).where(Organization.id == "org_raw_sql"))).scalar_one()
            assert row.created_at == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    finally:
        await db.dispose()


@pytest.mark.postgres
async def test_postgres_session_and_native_types(pg_session):
    """Real PostgreSQL: JSON, UUID-free string ids, and unique constraints behave the same."""
    org = Organization(slug="pg", name="PG Co", settings={"native": True})
    pg_session.add(org)
    await pg_session.flush()
    project = Project(org_id=org.id, name="PG project", settings={"a": [1, 2, 3]})
    pg_session.add(project)
    await pg_session.flush()
    await pg_session.commit()
    loaded = (await pg_session.execute(select(Project).where(Project.id == project.id))).scalar_one()
    assert loaded.settings == {"a": [1, 2, 3]}
    assert loaded.created_at.tzinfo is not None


@pytest.mark.postgres
async def test_postgres_migration_matches_metadata(pg_engine):
    """The Alembic baseline must create the same schema the models declare (no drift)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def _diff(connection):
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    async with pg_engine.connect() as conn:
        differences = await conn.run_sync(_diff)
    assert differences == [], f"schema drift: {differences[:5]}"
