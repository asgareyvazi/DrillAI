"""Add guarded immutable trajectory revision identity and reason.

Revision ID: f29a3c8d7e10
Revises: e2b4c6d8f013

Legacy trajectory rows are preserved as-is. Ambiguous duplicate version/current identities block the
migration; this revision does not decide which legacy trajectory should be current or renumber history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f29a3c8d7e10"
down_revision = "e2b4c6d8f013"
branch_labels = None
depends_on = None


def _preflight() -> tuple[int, int, int]:
    connection = op.get_bind()
    trajectories = sa.table(
        "trajectories",
        sa.column("id", sa.String(64)),
        sa.column("org_id", sa.String(64)),
        sa.column("wellbore_id", sa.String(64)),
        sa.column("kind", sa.String(40)),
        sa.column("name", sa.String(160)),
        sa.column("version", sa.Integer()),
        sa.column("is_current", sa.Boolean()),
    )
    duplicate_versions = connection.execute(
        sa.select(sa.func.count())
        .select_from(
            sa.select(trajectories.c.org_id)
            .where(trajectories.c.version > 0)
            .group_by(
                trajectories.c.org_id,
                trajectories.c.wellbore_id,
                trajectories.c.kind,
                trajectories.c.name,
                trajectories.c.version,
            )
            .having(sa.func.count() > 1)
            .subquery()
        )
    ).scalar_one()
    invalid_versions = connection.execute(
        sa.select(sa.func.count()).select_from(trajectories).where(trajectories.c.version <= 0)
    ).scalar_one()
    duplicate_current = connection.execute(
        sa.select(sa.func.count())
        .select_from(
            sa.select(trajectories.c.org_id)
            .where(trajectories.c.is_current.is_(True))
            .group_by(
                trajectories.c.org_id,
                trajectories.c.wellbore_id,
                trajectories.c.kind,
                trajectories.c.name,
            )
            .having(sa.func.count() > 1)
            .subquery()
        )
    ).scalar_one()
    return int(duplicate_versions), int(invalid_versions), int(duplicate_current)


def upgrade() -> None:
    duplicate_versions, invalid_versions, duplicate_current = _preflight()
    if duplicate_versions or invalid_versions or duplicate_current:
        raise RuntimeError(
            "trajectory revision preflight refused ambiguous legacy rows: "
            f"duplicate_version_groups={duplicate_versions}, invalid_version_rows={invalid_versions}, "
            f"duplicate_current_groups={duplicate_current}; no trajectory data was changed"
        )

    with op.batch_alter_table("trajectories") as batch:
        batch.add_column(
            sa.Column(
                "revision_reason",
                sa.Text(),
                nullable=True,
                comment="operator reason for this immutable trajectory content revision",
            )
        )
        batch.create_check_constraint("ck_trajectories_revision_version_positive", "version > 0")
        batch.create_unique_constraint(
            "uq_trajectories_revision_identity",
            ["org_id", "wellbore_id", "kind", "name", "version"],
        )
    op.create_index(
        "uq_trajectories_current_scope",
        "trajectories",
        ["org_id", "wellbore_id", "kind", "name"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current IS TRUE"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    trajectories = sa.table(
        "trajectories",
        sa.column("revision_reason", sa.Text()),
    )
    reasons = int(
        connection.execute(
            sa.select(sa.func.count()).select_from(trajectories).where(trajectories.c.revision_reason.is_not(None))
        ).scalar_one()
    )
    if reasons:
        raise RuntimeError(
            f"trajectory revision downgrade refused: {reasons} revision reason(s) would be lost"
        )

    op.drop_index("uq_trajectories_current_scope", table_name="trajectories")
    with op.batch_alter_table("trajectories") as batch:
        batch.drop_constraint("uq_trajectories_revision_identity", type_="unique")
        batch.drop_constraint("ck_trajectories_revision_version_positive", type_="check")
        batch.drop_column("revision_reason")
