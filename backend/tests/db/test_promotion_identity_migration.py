"""The DDR promotion identity index refuses legacy duplicates without deleting history."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.postgres
async def test_promotion_identity_unique_indexes_enforce_tenant_scoped_identity_on_postgres(
    pg_session,
) -> None:
    """PostgreSQL enforces the operation and event fingerprints the same way as SQLite."""
    import datetime as dt

    from sqlalchemy.exc import IntegrityError

    from drillai.db.models import Event, Operation, Organization

    org = Organization(slug="promotion-postgres", name="Promotion PostgreSQL")
    other_org = Organization(slug="promotion-postgres-other", name="Other promotion tenant")
    pg_session.add_all([org, other_org])
    await pg_session.flush()

    operation = Operation(
        org_id=org.id,
        well_id="wel_promotion_postgres",
        wellbore_id="wlb_promotion_postgres",
        operation_class="actual",
        name="Original operation",
        promotion_fingerprint="same-promotion-fingerprint",
    )
    event = Event(
        org_id=org.id,
        well_id="wel_promotion_postgres",
        kind="npt",
        title="Original event",
        occurred_at=dt.datetime(2026, 3, 15, tzinfo=dt.UTC),
        occurred_at_basis="source_record",
        promotion_fingerprint="same-promotion-fingerprint",
    )
    pg_session.add_all([operation, event])
    await pg_session.flush()

    with pytest.raises(IntegrityError):
        async with pg_session.begin_nested():
            pg_session.add(
                Operation(
                    org_id=org.id,
                    well_id="wel_promotion_postgres",
                    wellbore_id="wlb_promotion_postgres",
                    operation_class="actual",
                    name="Duplicate operation",
                    promotion_fingerprint=operation.promotion_fingerprint,
                )
            )
            await pg_session.flush()

    with pytest.raises(IntegrityError):
        async with pg_session.begin_nested():
            pg_session.add(
                Event(
                    org_id=org.id,
                    well_id="wel_promotion_postgres",
                    kind="npt",
                    title="Duplicate event",
                    occurred_at=dt.datetime(2026, 3, 16, tzinfo=dt.UTC),
                    occurred_at_basis="source_record",
                    promotion_fingerprint=event.promotion_fingerprint,
                )
            )
            await pg_session.flush()

    pg_session.add_all(
        [
            Operation(
                org_id=other_org.id,
                well_id="wel_other_promotion_postgres",
                wellbore_id="wlb_other_promotion_postgres",
                operation_class="actual",
                name="Other tenant operation",
                promotion_fingerprint=operation.promotion_fingerprint,
            ),
            Event(
                org_id=other_org.id,
                well_id="wel_other_promotion_postgres",
                kind="npt",
                title="Other tenant event",
                occurred_at=dt.datetime(2026, 3, 15, tzinfo=dt.UTC),
                occurred_at_basis="source_record",
                promotion_fingerprint=event.promotion_fingerprint,
            ),
        ]
    )
    await pg_session.flush()


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.mark.parametrize("model_name", ["operation", "event"])
def test_promotion_identity_migration_refuses_duplicates_then_enforces_uniqueness(
    tmp_path, model_name: str
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'promotion-identity.db'}"
    config = _config(database_url)
    command.upgrade(config, "c0f6d8a9b271")

    async def insert_duplicates() -> tuple[str, str]:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from drillai.db.models import Organization

        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                org = Organization(slug="promotion-migration", name="Promotion migration")
                session.add(org)
                await session.flush()
                row_ids = (
                    ("opr_legacy_duplicate_1", "opr_legacy_duplicate_2")
                    if model_name == "operation"
                    else ("evn_legacy_duplicate_1", "evn_legacy_duplicate_2")
                )
                if model_name == "operation":
                    await session.execute(
                        sa.text(
                            "INSERT INTO operations "
                            "(id, org_id, well_id, wellbore_id, operation_class, sequence, name, kind, phase, "
                            "status, is_productive, source, source_document_id, data_quality, attributes, "
                            "is_demo_fixture, source_record_id, promotion_fingerprint, source_kind) VALUES "
                            "(:id, :org_id, 'wel_legacy', 'wlb_legacy', 'actual', :sequence, :name, "
                            "'drilling', 'completed', 'completed', 1, 'report', 'doc_legacy_source', "
                            "'extracted', '{}', 0, 'rec_legacy_source', 'same-document-record-fingerprint', "
                            "'ddr_promotion')"
                        ),
                        [
                            {
                                "id": row_id,
                                "org_id": org.id,
                                "sequence": index,
                                "name": f"Legacy operation {index}",
                            }
                            for index, row_id in enumerate(row_ids, start=1)
                        ],
                    )
                else:
                    await session.execute(
                        sa.text(
                            "INSERT INTO events "
                            "(id, org_id, project_id, well_id, kind, is_npt, title, occurred_at, severity, "
                            "status, source, source_document_id, tags, attributes, is_demo_fixture, "
                            "source_record_id, promotion_fingerprint, source_kind, cause_basis, "
                            "classification_source) VALUES "
                            "(:id, :org_id, 'prj_legacy', 'wel_legacy', 'npt', 1, :title, :occurred_at, "
                            "'high', 'closed', 'report', 'doc_legacy_source', '[]', '{}', 0, "
                            "'rec_legacy_source', 'same-document-record-fingerprint', 'ddr_promotion', "
                            "'unknown', 'derived')"
                        ),
                        [
                            {
                                "id": row_id,
                                "org_id": org.id,
                                "title": f"Legacy event {index}",
                                "occurred_at": dt.datetime(2026, 3, index, tzinfo=dt.UTC),
                            }
                            for index, row_id in enumerate(row_ids, start=1)
                        ],
                    )
                await session.commit()
                return row_ids
        finally:
            await engine.dispose()

    _kept_id, duplicate_id = asyncio.run(insert_duplicates())
    with pytest.raises(RuntimeError, match="review duplicate rows first"):
        command.upgrade(config, "head")

    # The failed preflight must not have dropped the compatibility index or advanced the revision.
    sync_engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with sync_engine.connect() as connection:
            current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        sync_engine.dispose()
    assert current == "c0f6d8a9b271"

    async def clear_duplicate_fingerprint() -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                table_name = "operations" if model_name == "operation" else "events"
                await session.execute(
                    text(f"UPDATE {table_name} SET promotion_fingerprint = NULL WHERE id = :id"),
                    {"id": duplicate_id},
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(clear_duplicate_fingerprint())
    command.upgrade(config, "head")
    command.check(config)

    # The identity is scoped to an organization; two tenants can hold analogous rows independently.
    async def assert_other_tenant_can_reuse_fingerprint() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from drillai.db.models import Event, Operation, Organization

        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                other_org = Organization(slug="other-promotion-migration", name="Other tenant")
                session.add(other_org)
                await session.flush()
                common = {
                    "org_id": other_org.id,
                    "promotion_fingerprint": "same-document-record-fingerprint",
                    "source_document_id": "doc_legacy_source",
                    "source_record_id": "rec_legacy_source",
                }
                if model_name == "operation":
                    session.add(
                        Operation(
                            **common,
                            well_id="wel_other",
                            wellbore_id="wlb_other",
                            operation_class="actual",
                            sequence=1,
                            name="Other tenant operation",
                            source_kind="ddr_promotion",
                        )
                    )
                else:
                    session.add(
                        Event(
                            **common,
                            project_id="prj_other",
                            well_id="wel_other",
                            kind="npt",
                            title="Other tenant event",
                            occurred_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
                            occurred_at_basis="source_record",
                            source_kind="ddr_promotion",
                            classification_source="derived",
                        )
                    )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(assert_other_tenant_can_reuse_fingerprint())
