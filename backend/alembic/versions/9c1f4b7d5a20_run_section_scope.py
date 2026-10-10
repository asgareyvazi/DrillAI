"""record the hole section a workflow run was scoped to

A run could be started with a `section_id` and the API accepted it, but nothing stored it as a
column: it survived only inside the `context` blob. Two consequences, both found while proving the
run monitor reads the real scope: a client could not read back the section it had scoped a run to,
and "every run in this hole section" was not queryable.

The column is added nullable — existing runs keep their id and their scope is copied out of
`context.scope.section_id` where it was recorded.

Revision ID: 9c1f4b7d5a20
Revises: 6867078e1e32
Create Date: 2026-09-28 09:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c1f4b7d5a20"
down_revision = "6867078e1e32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("section_id", sa.String(length=64), nullable=True))
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_workflow_runs_section_id"), ["section_id"], unique=False)

    # Backfill: rows started before the column existed carry the scope inside `context`.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, context FROM workflow_runs "
            "WHERE context IS NOT NULL AND section_id IS NULL"
        )
    ).fetchall()
    for run_id, context in rows:
        if not isinstance(context, dict):
            continue
        scope = context.get("scope")
        if not isinstance(scope, dict):
            continue
        section_id = scope.get("section_id")
        if not section_id:
            continue
        connection.execute(
            sa.text("UPDATE workflow_runs SET section_id = :section_id WHERE id = :run_id"),
            {"section_id": section_id, "run_id": run_id},
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflow_runs_section_id"))
    with op.batch_alter_table("workflow_runs", schema=None) as batch_op:
        batch_op.drop_column("section_id")
