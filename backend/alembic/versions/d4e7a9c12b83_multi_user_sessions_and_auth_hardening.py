"""multi-user browser sessions, password reset tokens, and auth rate limits

Revision ID: d4e7a9c12b83
Revises: c9f1a48b2e31

Adds durable tables for:
- `user_sessions` (HttpOnly cookie browser sessions with CSRF synchronizer token hashes,
  expiry, revocation, and IP/User-Agent attribution),
- `password_reset_tokens` (single-use, time-bounded password reset / invitation digests),
- `auth_rate_limits` (multi-worker-safe rate limiting and brute-force lockout buckets).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e7a9c12b83"
down_revision = "c9f1a48b2e31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limits",
        sa.Column("bucket_key", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_rate_limits")),
    )
    with op.batch_alter_table("auth_rate_limits", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_auth_rate_limits_blocked_until"), ["blocked_until"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_auth_rate_limits_bucket_key"), ["bucket_key"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_auth_rate_limits_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_auth_rate_limits_scope"), ["scope"], unique=False)

    op.create_table(
        "password_reset_tokens",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("issued_by_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_reset_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_reset_tokens_token_hash")),
    )
    with op.batch_alter_table("password_reset_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_org_id"), ["org_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_token_prefix"), ["token_prefix"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "user_sessions",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_token_prefix", sa.String(length=16), nullable=False),
        sa.Column("session_token_hash", sa.String(length=128), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=128), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=120), nullable=True),
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
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_sessions_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_sessions_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_sessions_org_id"), ["org_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_sessions_revoked_at"), ["revoked_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_sessions_session_token_hash"),
            ["session_token_hash"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_user_sessions_session_token_prefix"),
            ["session_token_prefix"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_user_sessions_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_sessions_user_id"))
        batch_op.drop_index(batch_op.f("ix_user_sessions_session_token_prefix"))
        batch_op.drop_index(batch_op.f("ix_user_sessions_session_token_hash"))
        batch_op.drop_index(batch_op.f("ix_user_sessions_revoked_at"))
        batch_op.drop_index(batch_op.f("ix_user_sessions_org_id"))
        batch_op.drop_index(batch_op.f("ix_user_sessions_expires_at"))
        batch_op.drop_index(batch_op.f("ix_user_sessions_created_at"))
    op.drop_table("user_sessions")

    with op.batch_alter_table("password_reset_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_password_reset_tokens_user_id"))
        batch_op.drop_index(batch_op.f("ix_password_reset_tokens_token_prefix"))
        batch_op.drop_index(batch_op.f("ix_password_reset_tokens_org_id"))
        batch_op.drop_index(batch_op.f("ix_password_reset_tokens_expires_at"))
        batch_op.drop_index(batch_op.f("ix_password_reset_tokens_created_at"))
    op.drop_table("password_reset_tokens")

    with op.batch_alter_table("auth_rate_limits", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auth_rate_limits_scope"))
        batch_op.drop_index(batch_op.f("ix_auth_rate_limits_created_at"))
        batch_op.drop_index(batch_op.f("ix_auth_rate_limits_bucket_key"))
        batch_op.drop_index(batch_op.f("ix_auth_rate_limits_blocked_until"))
    op.drop_table("auth_rate_limits")
