"""Typed DDR persistence contracts: scope, identity, time/depth basis, and unit provenance."""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.db.models import (
    DAILY_REPORT_PARAMETER_SPECS,
    Base,
    DailyDrillingReport,
    DailyDrillingReportRevision,
    DailyReportActivity,
    DailyReportMeasurement,
    Organization,
    Project,
    Well,
    Wellbore,
)


@pytest_asyncio.fixture
async def constrained_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_assets(session: AsyncSession, org_id: str = "org_ddr_alpha") -> tuple[Well, Wellbore]:
    organization = Organization(
        id=org_id,
        slug=org_id,
        name="DDR Test Organization",
        kind="operator",
        timezone="America/Denver",
        default_unit_system="field_us",
        default_locale="en",
    )
    project = Project(id=f"prj_{org_id}", org_id=org_id, name="DDR test project")
    well = Well(
        id=f"wel_{org_id}",
        org_id=org_id,
        project_id=project.id,
        name="DDR test well",
    )
    wellbore = Wellbore(
        id=f"wlb_{org_id}",
        org_id=org_id,
        well_id=well.id,
        name="Original hole",
        purpose="original",
        sequence=1,
        is_active=True,
    )
    session.add_all([organization, project, well, wellbore])
    await session.flush()
    return well, wellbore


async def _seed_report(session: AsyncSession) -> tuple[DailyDrillingReport, DailyDrillingReportRevision]:
    well, wellbore = await _seed_assets(session)
    report = DailyDrillingReport(
        id="ddr_alpha_2026_11_01_day",
        org_id=well.org_id,
        well_id=well.id,
        wellbore_id=wellbore.id,
        report_date=dt.date(2026, 11, 1),
        report_timezone="America/Denver",
        shift_code="day",
        identity_key="shift:day",
        latest_revision_number=1,
        created_by="usr_operator_alpha",
    )
    revision = DailyDrillingReportRevision(
        id="drr_alpha_1",
        org_id=well.org_id,
        report_id=report.id,
        revision_number=1,
        edit_version=1,
        status="draft",
        source_kind="manual",
        period_start_at=dt.datetime(2026, 11, 1, 13, tzinfo=dt.UTC),
        period_end_at=dt.datetime(2026, 11, 2, 1, tzinfo=dt.UTC),
        summary="Operator-entered report summary.",
        created_by="usr_operator_alpha",
    )
    session.add_all([report, revision])
    await session.flush()
    return report, revision


def _measurement(revision: DailyDrillingReportRevision, **changes) -> DailyReportMeasurement:
    values = {
        "id": "drm_alpha_spp_0",
        "org_id": revision.org_id,
        "revision_id": revision.id,
        "parameter_key": "standpipe_pressure",
        "ordinal": 0,
        "dimension": "pressure",
        "canonical_unit": "Pa",
        "value_si": 6_894_757.293168,
        "source_value": 1000.0,
        "source_unit": "psi",
        "accepted_input_value": 1000.0,
        "accepted_input_unit": "psi",
        "value_basis": "reported",
        "observed_at": None,
        "time_basis": "unknown",
        "depth_si": None,
        "depth_reference": "unknown",
        "depth_basis": "unknown",
        "resolution_state": "manual",
    }
    values.update(changes)
    return DailyReportMeasurement(**values)


@pytest.mark.asyncio
async def test_typed_report_revision_activity_and_measurement_persist_with_unknown_time_explicit(
    constrained_session: AsyncSession,
):
    session = constrained_session
    _report, revision = await _seed_report(session)
    activity = DailyReportActivity(
        id="dra_alpha_1",
        org_id=revision.org_id,
        revision_id=revision.id,
        sequence=0,
        activity_kind="operation",
        operation_kind="drilling",
        title="Drilling interval",
        start_at=None,
        start_time_basis="unknown",
        end_at=None,
        end_time_basis="unknown",
        depth_from_si=None,
        depth_to_si=None,
        depth_reference="unknown",
        depth_basis="unknown",
        duration_basis="unknown",
        cause_basis="unknown",
        resolution_state="manual",
    )
    measurement = _measurement(revision)
    session.add_all([activity, measurement])
    await session.flush()

    assert revision.period_end_at == dt.datetime(2026, 11, 2, 1, tzinfo=dt.UTC)
    assert activity.start_at is None and activity.start_time_basis == "unknown"
    assert measurement.observed_at is None and measurement.time_basis == "unknown"
    assert measurement.source_value == 1000.0 and measurement.source_unit == "psi"
    assert measurement.value_si == pytest.approx(6_894_757.293168)


@pytest.mark.asyncio
async def test_database_refuses_duplicate_report_identity_and_invalid_parameter_contract(
    constrained_session: AsyncSession,
):
    session = constrained_session
    report, revision = await _seed_report(session)
    duplicate = DailyDrillingReport(
        id="ddr_alpha_duplicate",
        org_id=report.org_id,
        well_id=report.well_id,
        wellbore_id=report.wellbore_id,
        report_date=report.report_date,
        report_timezone=report.report_timezone,
        shift_code=report.shift_code,
        identity_key=report.identity_key,
        created_by=report.created_by,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    report, revision = await _seed_report(session)
    session.add(
        _measurement(
            revision,
            id="drm_alpha_invalid_dimension",
            ordinal=1,
            parameter_key="wob",
            dimension="pressure",
            canonical_unit="Pa",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_database_refuses_tenant_mismatched_revision_child_and_fabricated_time_basis(
    constrained_session: AsyncSession,
):
    session = constrained_session
    _report, revision = await _seed_report(session)
    await _seed_assets(session, "org_ddr_bravo")
    session.add(
        DailyReportActivity(
            id="dra_bravo_cross_tenant",
            org_id="org_ddr_bravo",
            revision_id=revision.id,
            sequence=0,
            activity_kind="event",
            title="Cross-tenant child must not attach",
            start_at=None,
            start_time_basis="unknown",
            end_at=None,
            end_time_basis="unknown",
            depth_reference="unknown",
            depth_basis="unknown",
            duration_basis="unknown",
            cause_basis="unknown",
            resolution_state="manual",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    _report, revision = await _seed_report(session)
    session.add(
        DailyReportActivity(
            id="dra_alpha_fabricated_time",
            org_id=revision.org_id,
            revision_id=revision.id,
            sequence=0,
            activity_kind="event",
            title="A period end is not an event time",
            start_at=None,
            start_time_basis="reported",
            end_at=None,
            end_time_basis="unknown",
            depth_reference="unknown",
            depth_basis="unknown",
            duration_basis="unknown",
            cause_basis="unknown",
            resolution_state="manual",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_enforces_daily_report_composite_tenant_and_unit_contracts(pg_session: AsyncSession):
    session = pg_session
    _report, revision = await _seed_report(session)
    await _seed_assets(session, "org_ddr_bravo")
    session.add(
        DailyReportActivity(
            id="dra_bravo_postgres_cross_tenant",
            org_id="org_ddr_bravo",
            revision_id=revision.id,
            sequence=0,
            activity_kind="event",
            title="Cross-tenant child must not attach",
            start_at=None,
            start_time_basis="unknown",
            end_at=None,
            end_time_basis="unknown",
            depth_reference="unknown",
            depth_basis="unknown",
            duration_basis="unknown",
            cause_basis="unknown",
            resolution_state="manual",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    _report, revision = await _seed_report(session)
    session.add(
        _measurement(
            revision,
            id="drm_alpha_postgres_bad_contract",
            parameter_key="wob",
            dimension="length",
            canonical_unit="m",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


def test_daily_report_parameter_contract_matches_domain_and_converter_registries():
    from drillai.telemetry.units import CANONICAL_UNIT, unit_dimensions
    from drillai.units.registry import get_unit

    for parameter_key, (dimension, canonical_unit, _depth_reference) in DAILY_REPORT_PARAMETER_SPECS.items():
        assert CANONICAL_UNIT[dimension] == canonical_unit, parameter_key
        assert dimension in unit_dimensions(canonical_unit), parameter_key
        # The DDR service uses the shared physical conversion registry; its canonical unit must exist.
        assert get_unit(canonical_unit).symbol == canonical_unit, parameter_key
