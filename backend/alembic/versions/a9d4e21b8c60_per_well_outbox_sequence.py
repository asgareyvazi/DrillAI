"""per-well outbox sequence allocator and stream index

Revision ID: a9d4e21b8c60
Revises: f7c3a8d21b46

An organisation-wide counter (`outbox_sequences.last_sequence`) gives a tenant a total order across
every well it operates, which an organisation-wide consumer needs. A well live stream
(`GET /wells/{well_id}/live/stream`) needs a contiguous sequence of its own: when Well B emits 1,500
events between two events on Well A, the difference between Well A's organisation-level sequence
numbers is 1,500 even though Well A only produced one event. Using the organisation sequence to measure
a well stream's backlog therefore mistakes activity on another well for a 1,500-event gap on this one.

This revision adds:

1. ``outbox_well_sequences`` — one counter row per ``(org_id, well_id)``, advanced with
   ``UPDATE ... RETURNING`` so concurrent writers on the same well are serialized without blocking
   writers on other wells;
2. ``outbox_events.well_sequence`` — the gap-free per-well sequence for every well-scoped event,
   backed by ``uq_outbox_events_well_sequence (org_id, well_id, well_sequence)`` and
   ``ix_outbox_events_well_stream (org_id, well_id, well_sequence)``;
3. backfill of existing well-scoped ``outbox_events`` rows in ``sequence`` order per ``(org_id, well_id)``,
   and seeding of ``outbox_well_sequences`` from the highest assigned per-well sequence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9d4e21b8c60"
down_revision = "f7c3a8d21b46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    op.create_table(
        "outbox_well_sequences",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("well_id", sa.String(length=64), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("org_id", "well_id", name="pk_outbox_well_sequences"),
    )
    op.create_index(
        "ix_outbox_well_sequences_created_at", "outbox_well_sequences", ["created_at"]
    )

    with op.batch_alter_table("outbox_events") as batch:
        batch.add_column(sa.Column("well_sequence", sa.Integer(), nullable=True))
        batch.create_unique_constraint(
            "uq_outbox_events_well_sequence", ["org_id", "well_id", "well_sequence"]
        )
        batch.create_index(
            "ix_outbox_events_well_stream", ["org_id", "well_id", "well_sequence"]
        )

    rows = connection.execute(
        sa.text(
            "SELECT id, org_id, well_id FROM outbox_events "
            "WHERE well_id IS NOT NULL ORDER BY org_id ASC, well_id ASC, sequence ASC"
        )
    ).all()
    counters: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row.org_id), str(row.well_id))
        next_seq = counters.get(key, 0) + 1
        counters[key] = next_seq
        connection.execute(
            sa.text("UPDATE outbox_events SET well_sequence = :seq WHERE id = :id"),
            {"seq": next_seq, "id": row.id},
        )

    connection.execute(
        sa.text(
            "INSERT INTO outbox_well_sequences (org_id, well_id, last_sequence, created_at, updated_at) "
            "SELECT org_id, well_id, MAX(well_sequence), MIN(created_at), MAX(updated_at) "
            "FROM outbox_events WHERE well_id IS NOT NULL AND well_sequence IS NOT NULL "
            "GROUP BY org_id, well_id"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch:
        batch.drop_index("ix_outbox_events_well_stream")
        batch.drop_constraint("uq_outbox_events_well_sequence", type_="unique")
        batch.drop_column("well_sequence")
    op.drop_index("ix_outbox_well_sequences_created_at", table_name="outbox_well_sequences")
    op.drop_table("outbox_well_sequences")
