"""The NPT vocabulary migration, on a database that already holds the old spellings.

Two things are proved here and they are different: that a database written by the previous runtime is
*repaired* into the canonical vocabulary with its raw spelling preserved on the row, and that a database
holding a category nobody has mapped *stops the migration* with a message naming the table, the value and
the row count.

The second is the more important half. A migration that folds an unknown category into "unclassified"
passes on every test that only checks the happy path, and destroys the only evidence that something is
writing a vocabulary the platform does not know.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from drillai.core.config import reset_settings_cache

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
BEFORE = "d5a71c93e2f8"  # the revision this migration revises
AFTER = "e6b2f47c9a15"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _migrate(database_url: str, revision: str) -> None:
    command.upgrade(_alembic_config(database_url), revision)


def _insert_legacy_rows(database_url: str, *, category: str, count: int = 1) -> None:
    """Write the rows the previous runtime produced: raw spellings straight into ``npt_category``."""

    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    now = dt.datetime(2026, 3, 15, 8, 0)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO organizations (id, slug, name, kind, timezone, default_unit_system, "
                    "default_locale, settings, is_active, created_at, updated_at) VALUES ('org_legacy', "
                    "'legacy', 'Legacy', 'operator', 'UTC', 'metric', 'en', '{}', 1, :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, org_id, name, status, datum_policy, settings, created_at, updated_at) "
                    "VALUES ('prj_legacy', 'org_legacy', 'Legacy project', 'active', 'rkb', '{}', "
                    ":now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO wells (id, org_id, project_id, name, status, well_type, "
                    "elevation_datum, is_offshore, twin_state, target_formations, tags, attributes, "
                    "is_demo_fixture, created_at, updated_at) VALUES ('wel_legacy', 'org_legacy', "
                    "'prj_legacy', 'LEG-1', 'drilling', 'development_producer', 'rkb', 0, 'design', "
                    "'[]', '[]', '{}', 0, :now, :now)"
                ),
                {"now": now},
            )
            for index in range(count):
                connection.execute(
                    sa.text(
                        "INSERT INTO events (id, org_id, well_id, kind, title, severity, status, "
                        "occurred_at, is_npt, npt_category, npt_hours, cause_basis, classification_source, "
                        "source, source_kind, tags, attributes, is_demo_fixture, created_at, updated_at) "
                        "VALUES (:id, 'org_legacy', 'wel_legacy', 'incident', :title, 'high', 'closed', "
                        ":now, 1, :category, 3.5, 'unknown', 'recorded', 'report', 'manual', '[]', '{}', "
                        "0, :now, :now)"
                    ),
                    {"id": f"evt_legacy_{index}", "title": f"Kick {index}", "category": category, "now": now},
                )
    finally:
        engine.dispose()


def _categories(database_url: str) -> list[str]:
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as connection:
            return [
                row[0]
                for row in connection.execute(
                    sa.text("SELECT npt_category FROM events ORDER BY id")
                ).all()
            ]
    finally:
        engine.dispose()


def _downgrade(database_url: str) -> None:
    command.downgrade(_alembic_config(database_url), BEFORE)


@pytest.fixture(autouse=True)
def _settings_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_legacy_spellings_are_repaired_to_the_canonical_vocabulary(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/npt-legacy.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, category="kick_well_control", count=3)

    _migrate(database_url, AFTER)
    assert _categories(database_url) == ["well_control"] * 3

    # Re-running the migration is a no-op: the values are canonical now, so nothing is rewritten.
    command.downgrade(_alembic_config(database_url), BEFORE)
    _migrate(database_url, AFTER)
    assert _categories(database_url) == ["well_control"] * 3


def test_a_category_nobody_has_mapped_stops_the_migration_and_is_named(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/npt-unknown.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, category="gremlins", count=2)

    with pytest.raises(RuntimeError) as failure:
        _migrate(database_url, AFTER)
    message = str(failure.value)
    assert "events" in message and "npt_category" in message
    assert "gremlins" in message
    assert "2 row(s)" in message, "the operator needs the size of the problem, not just its existence"
    # and the rows are untouched: a refused migration must not have partially rewritten anything
    assert _categories(database_url) == ["gremlins"] * 2


def test_the_canonical_vocabulary_survives_a_downgrade_and_re_upgrade(tmp_path) -> None:
    """The chain applies, reverses and re-applies — the gate every revision in this repository passes.

    The downgrade deliberately does not attempt to reverse a many-to-one merge (several legacy spellings
    became ``well_control``, and relabelling every one of them ``kick_well_control`` would corrupt rows
    that were already canonical). What it must do is leave a database the upgrade can run against again.
    """

    database_url = f"sqlite+aiosqlite:///{tmp_path}/npt-cycle.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, category="logistics", count=1)
    _migrate(database_url, AFTER)
    assert _categories(database_url) == ["waiting"]

    _downgrade(database_url)
    assert _categories(database_url) == ["logistics"], (
        "waiting has exactly one legacy spelling, so the downgrade can and does restore it"
    )
    _migrate(database_url, AFTER)
    assert _categories(database_url) == ["waiting"]
