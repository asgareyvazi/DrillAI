"""The migration chain against a real PostgreSQL.

The other persistence tests prove the *schema*; this proves the *migrations*. The difference matters,
because a migration is code that runs once, on somebody's database, with data already in it — and the
dialects disagree about exactly the things this chain does: boolean flags (SQLite stores them as
integers and PostgreSQL refuses `boolean = integer` outright, which is how the asset-identity revision
failed the first time it ran on PostgreSQL) and string aggregation.

What is asserted, in the order a deployment would meet it:

1. the whole chain applies from empty to head;
2. `alembic check` finds no drift between what the migrations built and what the models declare;
3. the last revision reverses and re-applies cleanly (a downgrade that hands back a database the
   application cannot use is not a downgrade);
4. a database that already contains two wells sharing a regulator identifier is **refused**, with the
   offenders named, rather than silently repaired by guessing which well owns the identifier;
5. database composite keys reject wellbore/report rows whose organization or well tuple disagrees with
   the referenced parent asset.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: The revision the asset-identity change sits on top of — the database state a deployment upgrading
#: into it would already have.
PREVIOUS_REVISION = "7d5e1c4a90b2"


def _config(url: str) -> Config:
    """An Alembic configuration pointed at ``url``, independent of the process environment."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # The environment reads this before application settings, which is what lets the test drive the
    # chain against its own embedded server without exporting an environment variable that the rest of
    # the suite would then share.
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _insert_two_duplicate_uwi_wells(url: str) -> list[str]:
    """Two wells sharing one UWI, written the way the application writes them (through the models).

    Returns the ids, so the failure message can be checked against the rows that really exist rather
    than against a string the test made up.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from drillai.db.models import Organization, Project, Well

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            org = Organization(slug="migration-check", name="Migration Check", kind="operator")
            session.add(org)
            await session.flush()
            project = Project(org_id=org.id, name="Migration Project")
            session.add(project)
            await session.flush()
            ids: list[str] = []
            for index in (1, 2):
                well = Well(
                    org_id=org.id,
                    project_id=project.id,
                    name=f"Legacy {index}",
                    # The pre-repair value: the migration is expected to move this to the canonical one.
                    well_type="development",
                    status="planned",
                    uwi="MIGRATION-DUP",
                )
                session.add(well)
                ids.append(well.id)
            await session.commit()
            return ids
    finally:
        await engine.dispose()


async def _well_ids(url: str) -> list[str]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from drillai.db.models import Well

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(select(Well.id).order_by(Well.id))
            return list(result.scalars())
    finally:
        await engine.dispose()


async def _assert_report_wellbore_scope_constraints(url: str) -> None:
    """PostgreSQL rejects cross-organization wellbores and cross-well daily reports."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO organizations "
                    "(id, slug, name, kind, timezone, default_unit_system, default_locale, settings, is_active) "
                    "VALUES (:id, :slug, :name, 'operator', 'UTC', 'SI', 'en', '{}', TRUE)"
                ),
                [
                    {
                        "id": "org_scope_pg",
                        "slug": "scope-postgres",
                        "name": "Scope PostgreSQL",
                    },
                    {
                        "id": "org_scope_pg_other",
                        "slug": "scope-postgres-other",
                        "name": "Other scope PostgreSQL",
                    },
                ],
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO projects (id, org_id, name, status, settings, datum_policy) "
                    "VALUES ('prj_scope_pg', 'org_scope_pg', 'Scope PostgreSQL project', 'active', '{}', 'rkb')"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO wells "
                    "(id, org_id, project_id, name, well_type, status, elevation_datum, is_offshore, twin_state, "
                    "target_formations, tags, attributes, is_demo_fixture) "
                    "VALUES (:id, 'org_scope_pg', 'prj_scope_pg', :name, 'development_producer', 'planned', 'msl', "
                    "FALSE, 'planned', '[]', '[]', '{}', FALSE)"
                ),
                [
                    {"id": "wel_scope_pg_a", "name": "Scope PostgreSQL well A"},
                    {"id": "wel_scope_pg_b", "name": "Scope PostgreSQL well B"},
                ],
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO wellbores "
                    "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
                    "VALUES (:id, 'org_scope_pg', :well_id, :name, 'original', 1, 'planned', TRUE, 'rkb', '{}')"
                ),
                [
                    {
                        "id": "wlb_scope_pg_a",
                        "well_id": "wel_scope_pg_a",
                        "name": "Scope PostgreSQL main bore A",
                    },
                    {
                        "id": "wlb_scope_pg_b",
                        "well_id": "wel_scope_pg_b",
                        "name": "Scope PostgreSQL main bore B",
                    },
                ],
            )

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "INSERT INTO wellbores "
                        "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
                        "VALUES ('wlb_scope_pg_wrong_org', 'org_scope_pg_other', 'wel_scope_pg_a', "
                        "'Wrong organization bore', 'sidetrack', 2, 'planned', FALSE, 'rkb', '{}')"
                    )
                )

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "INSERT INTO daily_drilling_reports "
                        "(id, org_id, well_id, wellbore_id, report_date, report_timezone, shift_code, "
                        "identity_key, created_by) VALUES ('ddr_scope_pg_wrong_well', 'org_scope_pg', "
                        "'wel_scope_pg_a', 'wlb_scope_pg_b', '2026-11-02', 'UTC', 'day', "
                        "'shift:day:scope-postgres', 'usr_scope_pg')"
                    )
                )
    finally:
        await engine.dispose()


async def _assert_reconciliation_targets_are_revision_scoped(url: str) -> None:
    """A migrated PostgreSQL database rejects missing and cross-revision typed targets."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from drillai.db.models import (
        DailyDrillingReport,
        DailyDrillingReportRevision,
        DailyReportActivity,
        DailyReportMeasurement,
        DailyReportReconciliation,
        Document,
        ExtractedRecord,
    )

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            document = Document(
                id="doc_scope_pg_target_test",
                org_id="org_scope_pg",
                doc_type="ddr",
                title="Synthetic migration target evidence",
            )
            record = ExtractedRecord(
                id="rec_scope_pg_target_test",
                org_id="org_scope_pg",
                document_id=document.id,
                record_type="daily_summary",
                payload_schema_key="migration-test.v1",
                payload={"synthetic": True},
            )
            report_a = DailyDrillingReport(
                id="ddr_scope_pg_target_a",
                org_id="org_scope_pg",
                well_id="wel_scope_pg_a",
                wellbore_id="wlb_scope_pg_a",
                report_date=date(2026, 11, 4),
                report_timezone="UTC",
                shift_code="day",
                identity_key="shift:day:scope-pg-target-a",
                created_by="usr_scope_pg",
            )
            report_b = DailyDrillingReport(
                id="ddr_scope_pg_target_b",
                org_id="org_scope_pg",
                well_id="wel_scope_pg_a",
                wellbore_id="wlb_scope_pg_a",
                report_date=date(2026, 11, 5),
                report_timezone="UTC",
                shift_code="day",
                identity_key="shift:day:scope-pg-target-b",
                created_by="usr_scope_pg",
            )
            revision_a = DailyDrillingReportRevision(
                id="drr_scope_pg_target_a",
                org_id="org_scope_pg",
                report_id=report_a.id,
                revision_number=1,
                source_kind="import",
                source_document_id=document.id,
                source_record_id=record.id,
                created_by="usr_scope_pg",
            )
            revision_b = DailyDrillingReportRevision(
                id="drr_scope_pg_target_b",
                org_id="org_scope_pg",
                report_id=report_b.id,
                revision_number=1,
                source_kind="import",
                source_document_id=document.id,
                source_record_id=record.id,
                created_by="usr_scope_pg",
            )
            activity_a = DailyReportActivity(
                id="dra_scope_pg_target_a",
                org_id="org_scope_pg",
                revision_id=revision_a.id,
                sequence=0,
                activity_kind="operation",
                title="Synthetic activity A",
                start_time_basis="unknown",
                end_time_basis="unknown",
                duration_basis="unknown",
                depth_reference="unknown",
                depth_basis="unknown",
                cause_basis="unknown",
                resolution_state="proposed",
            )
            activity_b = DailyReportActivity(
                id="dra_scope_pg_target_b",
                org_id="org_scope_pg",
                revision_id=revision_b.id,
                sequence=0,
                activity_kind="operation",
                title="Synthetic activity B",
                start_time_basis="unknown",
                end_time_basis="unknown",
                duration_basis="unknown",
                depth_reference="unknown",
                depth_basis="unknown",
                cause_basis="unknown",
                resolution_state="proposed",
            )
            measurement_a = DailyReportMeasurement(
                id="drm_scope_pg_target_a",
                org_id="org_scope_pg",
                revision_id=revision_a.id,
                parameter_key="mud_weight",
                ordinal=0,
                dimension="mud_weight",
                canonical_unit="kg/m3",
                value_si=1101.26,
                source_value=9.2,
                source_unit="ppg",
                accepted_input_value=9.2,
                accepted_input_unit="ppg",
                value_basis="reported",
                time_basis="unknown",
                depth_reference="unknown",
                depth_basis="unknown",
                resolution_state="proposed",
            )
            measurement_b = DailyReportMeasurement(
                id="drm_scope_pg_target_b",
                org_id="org_scope_pg",
                revision_id=revision_b.id,
                parameter_key="mud_weight",
                ordinal=0,
                dimension="mud_weight",
                canonical_unit="kg/m3",
                value_si=1101.26,
                source_value=9.2,
                source_unit="ppg",
                accepted_input_value=9.2,
                accepted_input_unit="ppg",
                value_basis="reported",
                time_basis="unknown",
                depth_reference="unknown",
                depth_basis="unknown",
                resolution_state="proposed",
            )
            reconciliations = [
                DailyReportReconciliation(
                    id="drc_scope_pg_activity_target",
                    org_id="org_scope_pg",
                    revision_id=revision_a.id,
                    target_key="activity:synthetic",
                    target_kind="activity",
                    activity_target_id=activity_a.id,
                    field_key="activity",
                    source_document_id=document.id,
                    source_record_id=record.id,
                    source_path="payload.activities[0]",
                    source_review_version=1,
                    source_fingerprint="a" * 64,
                    source_value_hash="b" * 64,
                    proposed_value_hash="c" * 64,
                    created_by="usr_scope_pg",
                ),
                DailyReportReconciliation(
                    id="drc_scope_pg_measurement_target",
                    org_id="org_scope_pg",
                    revision_id=revision_a.id,
                    target_key="measurement:mud_weight",
                    target_kind="measurement",
                    measurement_target_id=measurement_a.id,
                    field_key="mud_weight",
                    source_document_id=document.id,
                    source_record_id=record.id,
                    source_path="payload.measurements[0]",
                    source_review_version=1,
                    source_fingerprint="a" * 64,
                    source_value_hash="b" * 64,
                    proposed_value_hash="c" * 64,
                    created_by="usr_scope_pg",
                ),
            ]
            session.add_all([document, record])
            await session.flush()
            session.add_all([report_a, report_b])
            await session.flush()
            session.add_all([revision_a, revision_b])
            await session.flush()
            session.add_all([activity_a, activity_b, measurement_a, measurement_b])
            await session.flush()
            session.add_all(reconciliations)
            await session.commit()

        invalid_updates = (
            (
                "UPDATE daily_report_reconciliations SET activity_target_id=:target_id WHERE id=:id",
                {"target_id": activity_b.id, "id": "drc_scope_pg_activity_target"},
            ),
            (
                "UPDATE daily_report_reconciliations SET measurement_target_id=:target_id WHERE id=:id",
                {"target_id": measurement_b.id, "id": "drc_scope_pg_measurement_target"},
            ),
            (
                "UPDATE daily_report_reconciliations SET measurement_target_id=:target_id WHERE id=:id",
                {"target_id": "drm_scope_pg_missing", "id": "drc_scope_pg_measurement_target"},
            ),
        )
        for statement, parameters in invalid_updates:
            with pytest.raises(sa.exc.IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement), parameters)

        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT activity_target_id FROM daily_report_reconciliations "
                        "WHERE id='drc_scope_pg_activity_target'"
                    )
                )
            ).scalar_one() == activity_a.id
            assert (
                await connection.execute(
                    text(
                        "SELECT measurement_target_id FROM daily_report_reconciliations "
                        "WHERE id='drc_scope_pg_measurement_target'"
                    )
                )
            ).scalar_one() == measurement_a.id
    finally:
        await engine.dispose()


@pytest.mark.postgres
def test_migration_chain_applies_reverses_and_refuses_duplicates_on_postgres(postgres_url):
    """Synchronous on purpose: Alembic's environment runs the migrations with ``asyncio.run``, so a
    migration driven from inside a running event loop cannot work — and neither can the real
    deployment command, which is the thing being tested."""
    config = _config(postgres_url)

    # 1. Fresh database, whole chain.
    command.upgrade(config, "head")

    # 2. No drift between the migrations and the models.
    command.check(config)

    # 3. Reverse the last revision and re-apply it.
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, "head")
    command.check(config)

    # 4. An existing database with duplicate identifiers must be refused, not guessed at. The rows are
    #    written after stepping back to the previous revision — the state such a database really is in.
    command.downgrade(config, PREVIOUS_REVISION)
    inserted = asyncio.run(_insert_two_duplicate_uwi_wells(postgres_url))
    assert len(inserted) == 2

    with pytest.raises(Exception) as raised:
        command.upgrade(config, "head")
    message = str(raised.value)
    assert "duplicate values already recorded" in message
    # The message names the wells that own the clash: a migration that stops without saying which rows
    # are involved leaves a person grepping a production database.
    for well_id in asyncio.run(_well_ids(postgres_url)):
        assert well_id in message

    # Leave the database usable: the two rows are removed and the chain completes. A test that left the
    # schema half-upgraded would make the *next* test's failure look like its own.
    async def _clear() -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(postgres_url, pool_size=2, max_overflow=0)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await session.execute(text("DELETE FROM wells"))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_clear())
    command.upgrade(config, "head")
    command.check(config)
    asyncio.run(_assert_report_wellbore_scope_constraints(postgres_url))
    asyncio.run(_assert_reconciliation_targets_are_revision_scoped(postgres_url))


def test_the_migration_environment_honours_an_explicit_url(tmp_path):
    """The environment reads the URL the caller configured, before application settings.

    Without this, the test above would drive the *configured* database rather than its own — and the
    failure mode of that would be a migration running against whatever the environment happened to
    point at, which is the one thing a migration runner must never do.
    """
    from drillai.core.config import get_settings

    config = _config("sqlite+aiosqlite:///" + str(tmp_path / "explicit.db"))
    assert config.get_main_option("sqlalchemy.url") != get_settings().sqlalchemy_url()

    command.upgrade(config, "head")
    # The application's own database was not touched: the file the URL names is the one that was built.
    assert (tmp_path / "explicit.db").exists()
