"""alert life cycle vocabulary, and the outbox sequence allocator

Revision ID: f7c3a8d21b46
Revises: e6b2f47c9a15

Two changes, both about statements the platform makes to a consumer.

**The alert life cycle.** ``alerts.status`` defaulted to ``open`` and nothing defined what else it could
be: the table was written by whichever code path raised an alert and read by nobody. CP9 gives the column
a closed vocabulary — ``raised``/``acknowledged``/``cleared``/``cancelled`` — and this revision repairs
the values already in deployed databases. The mapping is deliberately not "everything unknown becomes
raised": a status this migration does not recognise stops it, naming the value and the row count,
because a mislabelled alert is an alert whose life cycle nobody can reason about.

**The threshold unit.** A rule now records the unit its thresholds are written in. Without it a rule
written in psi was compared against values stored in pascals: the numbers were self-consistent and the
comparison was silently wrong, which is the failure mode this platform refuses to have. The column is
nullable, so existing rules keep their meaning (evaluated in the channel's canonical unit, as they
already were) instead of being reinterpreted by a migration.

**The outbox sequence.** ``outbox_events.sequence`` was declared with a unique constraint per
organization and no allocator. The new ``outbox_sequences`` table holds one counter row per organization;
writers advance it with ``UPDATE ... RETURNING`` so concurrent writers are serialized on the row lock
instead of colliding on the unique index. Existing rows get a counter seeded from the highest sequence
they already contain, so the first event written after this migration continues the stream rather than
restarting it at 1 (which every connected consumer would read as "no gap").
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7c3a8d21b46"
down_revision = "e6b2f47c9a15"
branch_labels = None
depends_on = None

#: Raw status → canonical life-cycle state. Frozen here rather than imported: a migration must keep
#: doing what it did when it ran, whatever the application's vocabulary becomes.
STATUS_ALIASES = {
    "open": "raised",
    "active": "raised",
    "new": "raised",
    "firing": "raised",
    "ack": "acknowledged",
    "acknowledged": "acknowledged",
    "in_progress": "acknowledged",
    "resolved": "cleared",
    "closed": "cleared",
    "auto_cleared": "cleared",
    "dismissed": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

CANONICAL_STATUSES = ("raised", "acknowledged", "cleared", "cancelled")

#: Severity spellings this platform and its integrations have used.
SEVERITY_ALIASES = {
    "info": "low",
    "notice": "low",
    "low": "low",
    "warning": "medium",
    "warn": "medium",
    "medium": "medium",
    "moderate": "medium",
    "error": "high",
    "high": "high",
    "severe": "high",
    "critical": "critical",
    "fatal": "critical",
    "emergency": "critical",
}

CANONICAL_SEVERITIES = ("low", "medium", "high", "critical")


def _values(connection: sa.Connection, table: str, column: str) -> list[tuple[str, int]]:
    rows = connection.execute(
        sa.text(f"SELECT {column} AS value, COUNT(*) AS total FROM {table} GROUP BY {column}")
    ).all()
    return [(row.value, int(row.total)) for row in rows if row.value is not None]


def _repair(
    connection: sa.Connection,
    *,
    column: str,
    aliases: dict[str, str],
    allowed: tuple[str, ...],
) -> None:
    for value, total in _values(connection, "alerts", column):
        if value in allowed:
            continue
        target = aliases.get(value.strip().lower())
        if target is None:
            raise RuntimeError(
                f"refusing to migrate: alerts.{column} holds {total} row(s) with the unknown value "
                f"{value!r}. It is not part of the CP9 life-cycle/severity vocabulary and has no "
                f"documented alias, so the platform cannot say what state the alert is in. Correct the "
                f"rows (or add the alias to this migration) and run the migration again."
            )
        connection.execute(
            sa.text(f"UPDATE alerts SET {column} = :target WHERE {column} = :source"),
            {"target": target, "source": value},
        )


def upgrade() -> None:
    connection = op.get_bind()

    # 1. the allocator
    op.create_table(
        "outbox_sequences",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("org_id", name="pk_outbox_sequences"),
    )
    # `CreatedAtMixin` indexes the column; the model and the migration must agree or `alembic check`
    # reports drift, which is how this index was noticed in the first place.
    op.create_index("ix_outbox_sequences_created_at", "outbox_sequences", ["created_at"])

    # 2. the rule's declared threshold unit. Nullable on purpose: a rule written before this column
    #    existed was compared in the channel's canonical unit, and that stays true rather than being
    #    silently reinterpreted. SQLite needs batch mode to add a column to an existing table.
    with op.batch_alter_table("alert_rules") as batch:
        batch.add_column(sa.Column("unit", sa.String(length=24), nullable=True))

    # 3. seed it from the stream that already exists, so the first new event continues the numbering
    #    rather than restarting it. A consumer that resumed with `after_seq = 40` must not be sent a
    #    sequence 1 it has already seen.
    connection.execute(
        sa.text(
            "INSERT INTO outbox_sequences (org_id, last_sequence, created_at, updated_at) "
            "SELECT org_id, MAX(sequence), MIN(created_at), MAX(updated_at) FROM outbox_events "
            "GROUP BY org_id"
        )
    )

    # 4. the vocabulary repairs
    _repair(connection, column="status", aliases=STATUS_ALIASES, allowed=CANONICAL_STATUSES)
    _repair(connection, column="severity", aliases=SEVERITY_ALIASES, allowed=CANONICAL_SEVERITIES)


def downgrade() -> None:
    """Reverse the vocabulary, then drop the allocator.

    ``raised`` is restored to ``open`` because that was the only spelling the column ever had a default
    for; the other three states did not exist in the old vocabulary and are left as they are rather than
    being mapped onto a state the old code would then misinterpret. The sequence rows are dropped with
    the table — the numbering they carry lives on in ``outbox_events.sequence``, which is untouched.
    """

    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE alerts SET status = 'open' WHERE status = 'raised'")
    )
    connection.execute(
        sa.text("UPDATE alerts SET severity = 'warning' WHERE severity = 'medium'")
    )
    with op.batch_alter_table("alert_rules") as batch:
        batch.drop_column("unit")
    op.drop_index("ix_outbox_sequences_created_at", table_name="outbox_sequences")
    op.drop_table("outbox_sequences")
