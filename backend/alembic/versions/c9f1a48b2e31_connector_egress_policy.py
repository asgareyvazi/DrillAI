"""durable system-wide connector egress policy table

Revision ID: c9f1a48b2e31
Revises: b4e8f19c3d72

Replaces process-local in-memory connector egress policy overrides with a durable,
versioned database table (`connector_egress_policies`) enforced consistently across
API instances and `connector-worker` processes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9f1a48b2e31"
down_revision = "b4e8f19c3d72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_egress_policies",
        sa.Column("policy_key", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("allowed_hosts", sa.JSON(), nullable=False),
        sa.Column("allowed_private_cidrs", sa.JSON(), nullable=False),
        sa.Column("updated_by_id", sa.String(length=64), nullable=True),
        sa.Column("updated_by_display", sa.String(length=200), nullable=True),
        sa.Column("updated_by_org_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_egress_policies")),
        sa.UniqueConstraint("policy_key", name=op.f("uq_connector_egress_policies_policy_key")),
    )
    with op.batch_alter_table("connector_egress_policies", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_connector_egress_policies_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connector_egress_policies_policy_key"), ["policy_key"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connector_egress_policies_scope"), ["scope"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connector_egress_policies_status"), ["status"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("connector_egress_policies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_connector_egress_policies_status"))
        batch_op.drop_index(batch_op.f("ix_connector_egress_policies_scope"))
        batch_op.drop_index(batch_op.f("ix_connector_egress_policies_policy_key"))
        batch_op.drop_index(batch_op.f("ix_connector_egress_policies_created_at"))

    op.drop_table("connector_egress_policies")
