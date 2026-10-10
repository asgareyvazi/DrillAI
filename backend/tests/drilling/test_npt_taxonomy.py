"""One NPT vocabulary: the assertions that keep it one.

The platform carried three: the tuple event writes were validated against, the tuple the report charted
by, and the categories the content classifier's codes were written in. Five of the eleven chart buckets
were spelled differently from the validator's values, which means a kick recorded by the classifier
produced hours in a bucket the legend did not contain while the *event* was refused if it arrived with
the report's spelling. Nothing failed loudly; the numbers were simply split.

These tests are deliberately written over the *whole* vocabulary rather than the values that were wrong,
because the next category somebody adds is the one that would drift.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.db.models import (
    NPT_CATEGORIES,
    NPT_CATEGORY_ALIASES,
    Base,
    Event,
    NptCode,
    Organization,
    Project,
    Well,
    canonical_npt_category,
)
from drillai.db.models.operations import is_npt_category
from drillai.documents.scope import DocumentScope
from drillai.drilling.classifiers import STANDARD_NPT_CODES, classify_npt
from drillai.drilling.npt import NOT_NPT_CATEGORY, STANDARD_CATEGORIES, canonical_category_sql
from drillai.operations.events import NOT_NPT, EventService
from drillai.security.actions import Principal

# No `pytestmark`: the suite runs in `asyncio_mode = "auto"`, so the async tests below are collected
# without a mark and the pure vocabulary assertions in this file are not told they are coroutines.


def test_the_report_charts_by_the_vocabulary_the_validator_enforces() -> None:
    assert set(STANDARD_CATEGORIES) <= set(NPT_CATEGORIES)
    assert set(STANDARD_CATEGORIES) == set(NPT_CATEGORIES) - {NOT_NPT_CATEGORY}


def test_the_classifiers_codes_are_written_in_that_same_vocabulary() -> None:
    for row in STANDARD_NPT_CODES:
        assert row["category"] in NPT_CATEGORIES, row


def test_every_documented_alias_lands_on_a_canonical_category() -> None:
    for raw, canonical in NPT_CATEGORY_ALIASES.items():
        assert canonical in NPT_CATEGORIES, (raw, canonical)
        assert raw != canonical, raw


def test_the_alias_map_translates_and_never_guesses() -> None:
    assert canonical_npt_category("kick_well_control") == "well_control"
    assert canonical_npt_category("KICK_WELL_CONTROL") == "well_control"
    assert canonical_npt_category("  unknown  ") == "unclassified"
    assert canonical_npt_category("well_control") == "well_control"
    # An unknown spelling comes back unchanged, so the caller can refuse it *with the value*: folding it
    # into "unclassified" would hide that a connector is writing a dialect nobody has mapped.
    assert canonical_npt_category("gremlins") == "gremlins"
    assert canonical_npt_category(None) is None
    assert is_npt_category("well_control") is True
    assert is_npt_category("kick_well_control") is False


def test_no_category_has_two_canonical_spellings() -> None:
    """The reduction that made this one vocabulary: the aliases are no longer canonical values."""

    for raw in NPT_CATEGORY_ALIASES:
        assert raw not in NPT_CATEGORIES, f"{raw} is both an alias and a canonical category"


def test_the_sql_translation_is_generated_from_the_map() -> None:
    """The aggregation translates raw rows in SQL; the expression must come from the alias map so that
    adding an alias once reaches the report instead of leaving a second table of translations."""

    from sqlalchemy import func

    expression = canonical_category_sql(Event.npt_category)
    rendered = str(expression.compile(compile_kwargs={"literal_binds": True}))
    assert rendered.upper().startswith("CASE")
    for raw, canonical in NPT_CATEGORY_ALIASES.items():
        assert f"'{raw}'" in rendered, raw
        assert f"'{canonical}'" in rendered, canonical
    assert "lower" in str(expression).lower() or "lower" in rendered.lower()
    assert func.count() is not None  # the import is used; the assertion above is about SQL, not Python


# --------------------------------------------------------------------------- the write path


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
async def well(session: AsyncSession) -> Well:
    org = Organization(slug="taxonomy", name="Taxonomy")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Taxonomy project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="TAX-1")
    session.add(well)
    await session.flush()
    return well


async def test_an_event_filed_with_the_old_spelling_is_stored_canonically(
    session: AsyncSession, well: Well
) -> None:
    """A connector that still says ``kick_well_control`` is understood — once — and the spelling it used
    is kept on the row, because a translation nobody can audit is indistinguishable from a guess."""

    service = EventService(session, well.org_id, principal=Principal(id="usr_1", org_id=well.org_id))
    event = await service.create(
        scope=DocumentScope(well_id=well.id),
        kind="incident",
        title="Kick taken at 2410 m",
        npt_category="kick_well_control",
        npt_hours=6.0,
        is_npt=True,
    )
    assert event.npt_category == "well_control"
    assert (event.attributes or {}).get("npt_category_raw") == "kick_well_control"
    assert event.is_npt is True


async def test_an_unknown_category_is_refused_with_the_value_in_the_message(
    session: AsyncSession, well: Well
) -> None:
    from drillai.core.errors import ValidationFailed

    service = EventService(session, well.org_id, principal=Principal(id="usr_1", org_id=well.org_id))
    with pytest.raises(ValidationFailed) as failure:
        await service.create(
            scope=DocumentScope(well_id=well.id),
            kind="incident",
            title="Something odd",
            npt_category="gremlins",
        )
    assert failure.value.details["value"] == "gremlins", (
        "the refusal must name the spelling it did not understand, or the caller cannot fix it"
    )


async def test_a_classified_non_loss_cannot_be_booked_as_a_loss(session: AsyncSession, well: Well) -> None:
    """``not_npt`` is the vocabulary's way of saying "classified, and the answer is no". Saying both at
    once is a contradiction and is refused rather than resolved in whichever direction is convenient."""

    from drillai.core.errors import ValidationFailed

    service = EventService(session, well.org_id, principal=Principal(id="usr_1", org_id=well.org_id))
    with pytest.raises(ValidationFailed):
        await service.create(
            scope=DocumentScope(well_id=well.id),
            kind="performance",
            title="Fast connection",
            npt_category="not_npt",
            is_npt=True,
        )
    quiet = await service.create(
        scope=DocumentScope(well_id=well.id),
        kind="performance",
        title="Fast connection",
        npt_category="not_npt",
        npt_hours=0.5,
    )
    assert quiet.is_npt is False, "a classified non-loss is not a loss of time"
    assert NOT_NPT == "not_npt"


async def test_the_classifier_agrees_with_the_validator(session: AsyncSession, well: Well) -> None:
    """The end-to-end version of the same claim: every category the classifier can emit is one the event
    service accepts, so a promoted DDR event cannot be rejected by the validator that guards it."""

    service = EventService(session, well.org_id, principal=Principal(id="usr_1", org_id=well.org_id))
    samples = (
        "Stuck pipe at 2410 m",
        "Differentially stuck, free point at 2400 m",
        "Lost circulation, partial losses",
        "Kick taken, BOP closed",
        "Twist-off in the BHA",
        "Fishing job for the dropped BHA",
        "Motor failure in the BHA",
        "Damaged bit, pulled out of hole",
        "Pump repair on the active",
        "Rig repair: mechanical failure",
        "Hole caving and pack-off",
        "Tight hole through the shale",
        "Waiting on weather",
        "Third party service company on location",
        "Waiting on instructions from the supervisor",
    )
    for index, text in enumerate(samples):
        classification, _code, _controllable = classify_npt(text)
        assert classification.label is not None, f"{text!r} (sample {index}) matched no NPT signal"
        assert classification.label in NPT_CATEGORIES, (text, classification.label)
        event = await service.create(
            scope=DocumentScope(well_id=well.id),
            kind="incident",
            title=text,
            npt_category=classification.label,
            npt_hours=1.0,
            is_npt=True,
        )
        assert event.npt_category == classification.label


async def test_a_legacy_row_is_counted_in_the_canonical_bucket_at_query_time(
    session: AsyncSession, well: Well
) -> None:
    """Rows written before this revision (and rows a connector writes tomorrow under an old spelling)
    are translated by the aggregation itself, so the chart shows one bucket and not two."""

    session.add(
        Event(
            id="evt_legacy",
            org_id=well.org_id,
            well_id=well.id,
            kind="incident",
            title="Kick taken (written under the legacy spelling)",
            severity="high",
            status="closed",
            occurred_at=_moment(),
            is_npt=True,
            npt_category="kick_well_control",
            npt_hours=4.0,
        )
    )
    session.add(
        Event(
            id="evt_modern",
            org_id=well.org_id,
            well_id=well.id,
            kind="incident",
            title="Kick taken (canonical)",
            severity="high",
            status="closed",
            occurred_at=_moment(),
            is_npt=True,
            npt_category="well_control",
            npt_hours=2.0,
        )
    )
    await session.flush()

    grouped = (
        await session.execute(
            select(
                canonical_category_sql(Event.npt_category).label("category"),
                Event.npt_category.label("stored"),
            )
            .where(Event.npt_category.is_not(None))
            .group_by("category", "stored")
        )
    ).all()
    categories = {row.category for row in grouped}
    stored = {row.stored for row in grouped}
    assert categories == {"well_control"}, "the raw and the canonical spelling are one bucket"
    assert stored == {"kick_well_control", "well_control"}, "the rows themselves were not rewritten"

    # and the seed catalogue's categories are canonical, so the report's join lands on controllability
    for row in STANDARD_NPT_CODES:
        session.add(
            NptCode(
                id=f"npt_{row['code']}",
                org_id=well.org_id,
                code=str(row["code"]),
                name=str(row["name"]),
                category=str(row["category"]),
                subcategory=row.get("subcategory"),
                is_operator_controllable=bool(row["controllable"]),
                is_system=True,
            )
        )
    await session.flush()


def _moment():
    import datetime as dt

    return dt.datetime(2026, 3, 15, 8, 0, tzinfo=dt.UTC)
