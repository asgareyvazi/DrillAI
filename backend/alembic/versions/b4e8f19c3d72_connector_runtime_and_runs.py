"""connector runtime leases, fencing, telemetry timestamps and run ledger

Revision ID: b4e8f19c3d72
Revises: a9d4e21b8c60

Production telemetry connectors require:

1. explicit separation of operator intent (``desired_state``, ``is_enabled``, ``config_version``)
   from observed runtime health (``status``, ``last_connected_at``, ``last_poll_at``,
   ``last_successful_poll_at``, ``last_frame_at``, ``last_ingest_at``, ``reconnect_count``,
   ``backoff_seconds``, ``next_poll_at``, ``last_error_category``, ``last_error_at``,
   ``last_trace_id``);
2. distributed worker claim and stale-worker fencing fields (``worker_id``, ``lease_expires_at``,
   ``last_heartbeat_at``, ``fencing_token``) on ``connectors``;
3. target scope and protocol profile fields (``protocol_profile``, ``wellbore_id``,
   ``operation_id``, ``endpoint_url``);
4. ``connector_runs`` — a bounded operational ledger recording each poll, connection test, or
   preview with its fencing token, configuration version, point counts, cursor movement, safe
   error classification, and trace correlation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4e8f19c3d72"
down_revision = "a9d4e21b8c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connectors", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "protocol_profile",
                sa.String(length=64),
                nullable=False,
                server_default="synthetic.v1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "desired_state",
                sa.String(length=24),
                nullable=False,
                server_default="disabled",
            )
        )
        batch_op.add_column(
            sa.Column("config_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("wellbore_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("operation_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("endpoint_url", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("worker_id", sa.String(length=120), nullable=True))
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("last_successful_poll_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_frame_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("last_ingest_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_error_category", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_trace_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("reconnect_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("backoff_seconds", sa.Float(), nullable=False, server_default="0.0")
        )
        batch_op.add_column(sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True))

        batch_op.create_index(
            batch_op.f("ix_connectors_protocol_profile"), ["protocol_profile"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connectors_desired_state"), ["desired_state"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connectors_wellbore_id"), ["wellbore_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connectors_operation_id"), ["operation_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_connectors_worker_id"), ["worker_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_connectors_lease_expires_at"), ["lease_expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connectors_next_poll_at"), ["next_poll_at"], unique=False
        )

    op.create_table(
        "connector_runs",
        sa.Column("connector_id", sa.String(length=64), nullable=False),
        sa.Column("well_id", sa.String(length=64), nullable=True),
        sa.Column("worker_id", sa.String(length=120), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("run_kind", sa.String(length=32), nullable=False, server_default="poll"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("frames_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_duplicates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_revised", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alerts_raised", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alerts_cleared", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor_before", sa.JSON(), nullable=False),
        sa.Column("cursor_after", sa.JSON(), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["connector_id"],
            ["connectors.id"],
            name=op.f("fk_connector_runs_connector_id_connectors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_runs")),
    )
    with op.batch_alter_table("connector_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_connector_runs_connector_id"), ["connector_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_connector_runs_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_connector_runs_org_id"), ["org_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_connector_runs_run_kind"), ["run_kind"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_connector_runs_started_at"), ["started_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_connector_runs_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_connector_runs_trace_id"), ["trace_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_connector_runs_well_id"), ["well_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("connector_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_connector_runs_well_id"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_trace_id"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_status"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_started_at"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_run_kind"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_org_id"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_created_at"))
        batch_op.drop_index(batch_op.f("ix_connector_runs_connector_id"))

    op.drop_table("connector_runs")

    with op.batch_alter_table("connectors", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_connectors_next_poll_at"))
        batch_op.drop_index(batch_op.f("ix_connectors_lease_expires_at"))
        batch_op.drop_index(batch_op.f("ix_connectors_worker_id"))
        batch_op.drop_index(batch_op.f("ix_connectors_operation_id"))
        batch_op.drop_index(batch_op.f("ix_connectors_wellbore_id"))
        batch_op.drop_index(batch_op.f("ix_connectors_desired_state"))
        batch_op.drop_index(batch_op.f("ix_connectors_protocol_profile"))

        batch_op.drop_column("next_poll_at")
        batch_op.drop_column("backoff_seconds")
        batch_op.drop_column("reconnect_count")
        batch_op.drop_column("last_trace_id")
        batch_op.drop_column("last_error_at")
        batch_op.drop_column("last_error_category")
        batch_op.drop_column("last_ingest_at")
        batch_op.drop_column("last_frame_at")
        batch_op.drop_column("last_successful_poll_at")
        batch_op.drop_column("last_poll_at")
        batch_op.drop_column("last_connected_at")
        batch_op.drop_column("last_transition_at")
        batch_op.drop_column("fencing_token")
        batch_op.drop_column("last_heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("worker_id")
        batch_op.drop_column("endpoint_url")
        batch_op.drop_column("operation_id")
        batch_op.drop_column("wellbore_id")
        batch_op.drop_column("config_version")
        batch_op.drop_column("desired_state")
        batch_op.drop_column("protocol_profile")
