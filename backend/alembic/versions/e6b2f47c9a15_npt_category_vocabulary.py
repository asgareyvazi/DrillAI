"""the NPT category vocabulary: one spelling per meaning, with the raw ones mapped

Revision ID: e6b2f47c9a15
Revises: d5a71c93e2f8

The platform had three NPT category vocabularies and they disagreed. ``db/models/operations.py``
validated event writes against ``kick_well_control``, ``hole_problem``, ``rig_equipment``,
``tool_failure`` and ``unknown``; ``drilling/npt.py`` presented ``well_control``, ``hole_problems``,
``surface_equipment``, ``downhole_tools`` and ``unclassified`` as its chart buckets; and the content
classifier emitted codes whose categories were in neither. The consequences were both silent: a kick
recorded through the classifier produced hours in a bucket the chart legend did not contain, and an
event written with the report's spelling was *refused* by the validator even though the report is what
the operator was reading.

This revision makes the canonical vocabulary the one the chart and the classifier already use, and
translates the raw spellings that exist in deployed databases into it:

    kick_well_control  -> well_control
    hole_problem       -> hole_problems
    rig_equipment      -> equipment_failure
    tool_failure       -> downhole_tools
    unknown            -> unclassified
    logistics          -> waiting

A category that is neither canonical nor a known alias stops the migration with the table, the value and
the number of rows involved. Folding it into ``unclassified`` would have been easier and would have
destroyed the only evidence that something is writing a dialect nobody has mapped yet.

Columns are only *values* here — the types do not change — so the downgrade restores the legacy spelling
of the merged values where the merge was one-to-one, and leaves the many-to-one merges (a canonical
value that had several raw sources) alone rather than inventing which spelling a row arrived in. That is
recorded in the downgrade body instead of being papered over.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6b2f47c9a15"
down_revision = "d5a71c93e2f8"
branch_labels = None
depends_on = None

#: Raw spelling → canonical. Kept as a literal copy of ``db.models.operations.NPT_CATEGORY_ALIASES`` at
#: this revision, deliberately frozen: a migration that imported the application's map would change its
#: own meaning the next time somebody edits it, and this file has to keep doing what it did.
ALIASES = {
    "kick_well_control": "well_control",
    "kick": "well_control",
    "well_control_loss": "well_control",
    "hole_problem": "hole_problems",
    "wellbore_problem": "hole_problems",
    "wellbore_problems": "hole_problems",
    "loss_circulation": "lost_circulation",
    "lost_returns": "lost_circulation",
    "tool_failure": "downhole_tools",
    "downhole_tool_failure": "downhole_tools",
    "rig_equipment": "equipment_failure",
    "equipment_failures": "equipment_failure",
    "surface_equipment_failure": "surface_equipment",
    "logistics": "waiting",
    "waiting_on_weather": "weather",
    "third_party_time": "third_party",
    "unknown": "unclassified",
    "other": "unclassified",
}

CANONICAL = (
    "equipment_failure",
    "surface_equipment",
    "downhole_tools",
    "hole_problems",
    "lost_circulation",
    "stuck_pipe",
    "well_control",
    "third_party",
    "weather",
    "waiting",
    "unclassified",
    "not_npt",
)

#: ``(table, column)`` pairs that carry the vocabulary. Both are nullable; NULL means "not classified",
#: which is a statement of its own and is left exactly as it is.
TARGETS = (("events", "npt_category"), ("npt_codes", "category"))


def _distinct(connection: sa.Connection, table: str, column: str) -> list[tuple[str, int]]:
    rows = connection.execute(
        sa.text(f"SELECT {column} AS value, COUNT(*) AS total FROM {table} GROUP BY {column}")
    ).all()
    return [(row.value, int(row.total)) for row in rows if row.value is not None]


def _apply(connection: sa.Connection, *, mapping: dict[str, str], allowed: tuple[str, ...]) -> None:
    for table, column in TARGETS:
        for value, total in _distinct(connection, table, column):
            if value in allowed:
                continue
            target = mapping.get(value)
            if target is None:
                raise RuntimeError(
                    f"refusing to migrate: {table}.{column} holds {total} row(s) with the unknown NPT "
                    f"category {value!r}. It is neither canonical nor a documented alias, so the "
                    f"platform cannot say what it means. Map it (add it to NPT_CATEGORY_ALIASES and to "
                    f"this migration's ALIASES, or correct the rows) and run the migration again."
                )
            connection.execute(
                sa.text(f"UPDATE {table} SET {column} = :target WHERE {column} = :source"),
                {"target": target, "source": value},
            )


def upgrade() -> None:
    connection = op.get_bind()
    _apply(connection, mapping=ALIASES, allowed=CANONICAL)


def downgrade() -> None:
    """Restore the legacy spelling only where the old vocabulary had exactly one spelling to restore.

    ``well_control``, ``hole_problems``, ``downhole_tools``, ``equipment_failure`` and ``unclassified``
    each had more than one raw source, so a row that now reads ``well_control`` may have arrived as
    ``kick_well_control`` or as ``well_control`` — and both were accepted before this revision. Reversing
    the merge would relabel rows that were already canonical, which is a corruption dressed as a
    downgrade. The types do not change, so nothing about the schema needs reversing; what is reversed is
    the spelling of the values that had a single raw source.
    """

    connection = op.get_bind()
    single_source = {"waiting": "logistics", "weather": "waiting_on_weather", "third_party": "third_party_time"}
    for table, column in TARGETS:
        for canonical, legacy in single_source.items():
            # `logistics`/`waiting_on_weather`/`third_party_time` were never produced by the classifier,
            # so a row carrying them can only be one this migration rewrote.
            connection.execute(
                sa.text(f"UPDATE {table} SET {column} = :legacy WHERE {column} = :canonical"),
                {"legacy": legacy, "canonical": canonical},
            )
