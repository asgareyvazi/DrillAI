"""Persist explicit survey azimuth frame identities and raw correction provenance.

Revision ID: b83e4a6d1c29
Revises: e13acb7d4f20

The previous schema stored a free-form magnetic-reference label on individual stations but did not
identify the actual frame or the common frame of a trajectory. Add nullable fields so historical
rows remain honestly unclassified; no migration guesses a frame from the legacy column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b83e4a6d1c29"
down_revision = "e13acb7d4f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trajectories", sa.Column("azimuth_reference", sa.String(length=16), nullable=True))
    op.add_column("trajectories", sa.Column("azimuth_reference_id", sa.String(length=120), nullable=True))
    op.add_column("trajectory_stations", sa.Column("input_azimuth_deg", sa.Float(), nullable=True))
    op.add_column("trajectory_stations", sa.Column("input_azimuth_reference", sa.String(length=16), nullable=True))
    op.add_column("trajectory_stations", sa.Column("input_azimuth_reference_id", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("trajectory_stations", "input_azimuth_reference_id")
    op.drop_column("trajectory_stations", "input_azimuth_reference")
    op.drop_column("trajectory_stations", "input_azimuth_deg")
    op.drop_column("trajectories", "azimuth_reference_id")
    op.drop_column("trajectories", "azimuth_reference")
