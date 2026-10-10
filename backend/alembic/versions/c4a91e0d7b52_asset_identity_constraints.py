"""asset identity: one vocabulary, one active hole, unique identifiers

Three defects met in the asset tables, and this migration repairs the data before it constrains the
shape — in that order, because a deployment that ran the old code holds rows the new constraints
would reject.

**The vocabulary drift.** ``well_type`` and ``purpose`` were free text. The create endpoints defaulted
them to ``development`` and ``production`` — values that appear in neither vocabulary — so every well
created without an explicit type was written with a type the domain does not recognise, and every
wellbore with a purpose it does not recognise. This was not hypothetical: the repository's own seeder
uses both, so the demo well itself carried the drift.

Rows holding those two *default* values are maps to their canonical equivalents. That is a mechanical
correction and not a judgement about the well: the values could only have arrived by omission, because
no client that named a type deliberately could have chosen a string the vocabulary never contained.
Any *other* unrecognised value is left exactly as it is — silently rewriting a value the platform does
not understand would be inventing data, and an operator who entered it is the only one who can say
what it meant. Validation now stops new ones at the boundary.

**The missing uniqueness.** ``wellbores`` allowed a well to hold two rows at the same ``sequence``, and
``wells`` allowed one UWI or one API number to belong to two wells. Where duplicates exist the repair
is deterministic rather than semantic: wellbore positions are renumbered densely in ``(created_at, id)``
order, the same rule the run-event migration used, so the same input always produces the same result
and nothing is deleted. Duplicate UWI and API values are *not* repaired automatically — deciding which
of two wells owns a regulator identifier is an operator's call, so the migration reports the offending
ids and stops rather than guessing.

**The active wellbore.** ``is_active`` was computed as ``sequence == 1`` at creation and never
maintained, so a well could report two active holes (both created at position one) or none (the
position-one row removed). The existing rule is kept and made explicit — the lowest sequence wins —
and a partial unique index makes a second active row impossible from now on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4a91e0d7b52"
down_revision = "7d5e1c4a90b2"
branch_labels = None
depends_on = None

# The two values the old create path wrote when the caller said nothing.
_LEGACY_WELL_TYPE = "development"
_LEGACY_WELLBORE_PURPOSE = "production"
_CANONICAL_WELL_TYPE = "development_producer"
_CANONICAL_WELLBORE_PURPOSE = "original"


def _repair_duplicate_identifiers(connection: sa.Connection) -> None:
    """Stop rather than guess: two wells cannot both own one UWI."""
    for column in ("uwi", "api_number"):
        duplicates = connection.execute(
            sa.text(
                f"""
                SELECT org_id, {column} AS value
                FROM wells
                WHERE {column} IS NOT NULL
                GROUP BY org_id, {column}
                HAVING COUNT(*) > 1
                """
            )
        ).all()
        if not duplicates:
            continue
        # Names of the offenders are fetched per group rather than aggregated in SQL: string
        # aggregation is dialect-specific (GROUP_CONCAT vs STRING_AGG) and this file must run
        # unchanged on both SQLite and PostgreSQL.
        offenders = []
        for row in duplicates:
            well_ids = connection.execute(
                sa.text(f"SELECT id FROM wells WHERE org_id = :org_id AND {column} = :value ORDER BY id"),
                {"org_id": row.org_id, "value": row.value},
            ).scalars()
            offenders.append(f"org {row.org_id} {column} {row.value!r}: {', '.join(well_ids)}")
        raise RuntimeError(
            f"cannot enforce unique {column}: duplicate values already recorded — "
            + "; ".join(offenders)
            + ". Resolve these by hand: which well owns the identifier is a decision the "
            "migration will not make for you."
        )


def upgrade() -> None:
    connection = op.get_bind()

    # 1. vocabulary repair (defaults only, nothing else is touched)
    connection.execute(
        sa.text("UPDATE wells SET well_type = :canonical WHERE well_type = :legacy"),
        {"canonical": _CANONICAL_WELL_TYPE, "legacy": _LEGACY_WELL_TYPE},
    )
    connection.execute(
        sa.text("UPDATE wellbores SET purpose = :canonical WHERE purpose = :legacy"),
        {"canonical": _CANONICAL_WELLBORE_PURPOSE, "legacy": _LEGACY_WELLBORE_PURPOSE},
    )

    # 2. wellbore positions: renumber only the wells that actually contain a collision
    connection.execute(
        sa.text(
            """
            UPDATE wellbores AS target
            SET sequence = ranked.position
            FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY well_id ORDER BY created_at, id) AS position
                FROM wellbores
                WHERE well_id IN (
                    SELECT well_id FROM wellbores GROUP BY well_id, sequence HAVING COUNT(*) > 1
                )
            ) AS ranked
            WHERE ranked.id = target.id
            """
        )
    )

    # 3. one active hole per well: keep the lowest sequence's claim, clear the rest
    #
    # The flags are compared as booleans rather than as `1`, and the statement is built from a
    # lightweight table rather than written as literal SQL. SQLite stores booleans as integers and is
    # happy either way; PostgreSQL types `is_active` as a real boolean and refuses `boolean = integer`
    # outright — which is how this migration failed on PostgreSQL the first time it was run there.
    wellbores = sa.table(
        "wellbores",
        sa.column("id", sa.String),
        sa.column("well_id", sa.String),
        sa.column("sequence", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )
    other = wellbores.alias("other")
    connection.execute(
        sa.update(wellbores)
        .where(
            wellbores.c.is_active.is_(True),
            sa.exists(
                sa.select(sa.literal(1))
                .select_from(other)
                .where(
                    other.c.well_id == wellbores.c.well_id,
                    other.c.is_active.is_(True),
                    sa.tuple_(other.c.sequence, other.c.id) < sa.tuple_(wellbores.c.sequence, wellbores.c.id),
                )
            ),
        )
        .values(is_active=False)
    )

    # 4. identity uniqueness, then the constraints
    _repair_duplicate_identifiers(connection)

    # `wells(project_id, name)` was already unique from the baseline, so only the two missing
    # constraints are created here. Their names are the ones the SQLAlchemy naming convention derives,
    # which is why they are spelled out rather than left to the dialect: the migration has to create
    # the exact name the models expect, or `alembic check` reports drift forever.
    with op.batch_alter_table("fields", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_fields_project_id_name", ["project_id", "name"])

    with op.batch_alter_table("wellbores", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_wellbores_well_id_sequence", ["well_id", "sequence"])

    op.create_index(
        "uq_wells_org_uwi",
        "wells",
        ["org_id", "uwi"],
        unique=True,
        sqlite_where=sa.text("uwi IS NOT NULL"),
        postgresql_where=sa.text("uwi IS NOT NULL"),
    )
    op.create_index(
        "uq_wells_org_api_number",
        "wells",
        ["org_id", "api_number"],
        unique=True,
        sqlite_where=sa.text("api_number IS NOT NULL"),
        postgresql_where=sa.text("api_number IS NOT NULL"),
    )
    op.create_index(
        "uq_wellbores_active_per_well",
        "wellbores",
        ["well_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    """Drop the constraints. The vocabulary repair is deliberately **not** reversed.

    Putting ``development`` back would mean restoring a value the platform has just been taught to
    reject: the downgrade would produce a database the application refuses to write to. A downgrade
    that corrupts the data it hands back is not a downgrade, so the repaired vocabulary stays and only
    the structural guarantees are removed.
    """
    op.drop_index("uq_wellbores_active_per_well", table_name="wellbores")
    op.drop_index("uq_wells_org_api_number", table_name="wells")
    op.drop_index("uq_wells_org_uwi", table_name="wells")
    with op.batch_alter_table("wellbores", schema=None) as batch_op:
        batch_op.drop_constraint("uq_wellbores_well_id_sequence", type_="unique")
    with op.batch_alter_table("fields", schema=None) as batch_op:
        batch_op.drop_constraint("uq_fields_project_id_name", type_="unique")
    # `uq_wells_project_id_name` is *not* dropped: it belongs to the baseline schema, not to this
    # revision. Dropping it here would hand back a database missing a guarantee the previous revision
    # had, and a later `upgrade` would not restore it (this revision never creates it).
