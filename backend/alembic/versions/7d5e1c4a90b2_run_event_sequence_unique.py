"""make the run event sequence unique per run

`RunEvent.seq` is what a client resumes from (`after_seq` on the HTTP log and on the WebSocket), so
it has to be strictly increasing *per run*. It was not: the runtime numbered events from an instance
counter that started at zero, and resuming a run constructs a new runtime — the resumed stretch
therefore repeated the sequence numbers of the events already in the table.

Nothing enforced uniqueness, so the duplicates were written silently. The effect was exactly the one
this log exists to prevent: a client holding a cursor never saw the events after it, and a run that
had visibly succeeded looked like it had no further activity.

Two changes: `WorkflowRuntime._next_event_seq` continues from the durable maximum, and the database
now refuses a repeated `(run_id, seq)`. Existing rows are repaired first — renumbered per affected
run, in their recorded time order, without deleting anything — before the constraint is created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7d5e1c4a90b2"
down_revision = "9c1f4b7d5a20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    # Repair, then constrain — in that order, because a deployment that ran the old code holds rows
    # the new constraint would reject.
    #
    # Only runs that actually contain a repeated sequence number are touched: a deployment large
    # enough to care should not rewrite its whole event log to fix the runs the defect reached. For
    # an affected run the rows are renumbered densely from 1 in `(occurred_at, id)` order, which is
    # deterministic and preserves the order a reader would have reconstructed from timestamps.
    #
    # Nothing is deleted and nothing is merged: a duplicate row is a real event that was recorded
    # twice under one number, so it keeps its own row and receives the next number. `id` breaks ties,
    # so the same input always produces the same numbering.
    connection.execute(
        sa.text(
            """
            UPDATE run_events AS target
            SET seq = ranked.position
            FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY occurred_at, id) AS position
                FROM run_events
                WHERE run_id IN (
                    SELECT run_id FROM run_events GROUP BY run_id, seq HAVING COUNT(*) > 1
                )
            ) AS ranked
            WHERE ranked.id = target.id
            """
        )
    )

    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_run_events_run_seq", ["run_id", "seq"])


def downgrade() -> None:
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.drop_constraint("uq_run_events_run_seq", type_="unique")
