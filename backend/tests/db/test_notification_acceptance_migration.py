"""Notification acceptance schema preserves legacy status without inventing delivery receipts."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "d2a4f6c8b109"
HEAD_REVISION = "e90f1c8a2b64"


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_outbound(connection: sa.Connection, *, message_id: str, status: str, **fields) -> None:
    values = {
        "id": message_id,
        "org_id": "org_notifications",
        "channel": "webhook",
        "status": status,
        "payload": "{}",
        "attachments": "[]",
        "evidence_refs": "[]",
        "attributes": "{}",
        "attempts": 1,
        "max_attempts": 3,
        "created_at": "2026-11-03 12:00:00",
        "updated_at": "2026-11-03 12:00:00",
        **fields,
    }
    columns = ", ".join(values)
    parameters = ", ".join(f":{name}" for name in values)
    connection.execute(sa.text(f"INSERT INTO outbound_messages ({columns}) VALUES ({parameters})"), values)


def test_acceptance_migration_refuses_duplicate_pending_idempotency_without_deleting_history(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'notification-idempotency.db'}"
    config = _config(database_url)
    engine = sa.create_engine(database_url)
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            _insert_outbound(
                connection,
                message_id="obm_duplicate_one",
                status="queued",
                idempotency_key="run-1:notify",
            )
            _insert_outbound(
                connection,
                message_id="obm_duplicate_two",
                status="queued",
                idempotency_key="run-1:notify",
            )

        with pytest.raises(RuntimeError, match="duplicate pending keys"):
            command.upgrade(config, HEAD_REVISION)

        with engine.connect() as connection:
            remaining = connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM outbound_messages "
                    "WHERE idempotency_key='run-1:notify' AND status='queued'"
                )
            ).scalar_one()
            assert remaining == 2
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == PARENT_REVISION
    finally:
        engine.dispose()


def test_acceptance_migration_keeps_legacy_sent_unknown_and_guards_new_evidence(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'notification-acceptance.db'}"
    config = _config(database_url)
    engine = sa.create_engine(database_url)
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            _insert_outbound(connection, message_id="obm_legacy_sent", status="sent", sent_at="2026-11-03 12:01:00")

        command.upgrade(config, HEAD_REVISION)
        command.check(config)
        with engine.begin() as connection:
            legacy = connection.execute(
                sa.text(
                    "SELECT status, sent_at, accepted_at, acceptance_evidence "
                    "FROM outbound_messages WHERE id='obm_legacy_sent'"
                )
            ).one()
            assert legacy.status == "sent"
            assert legacy.sent_at is not None
            assert legacy.accepted_at is None
            assert legacy.acceptance_evidence is None

            _insert_outbound(
                connection,
                message_id="obm_accepted",
                status="accepted",
                accepted_at="2026-11-03 12:02:00",
                acceptance_evidence="http_2xx",
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    sa.text(
                        "INSERT INTO outbound_messages "
                        "(id, org_id, channel, status, payload, attachments, evidence_refs, attributes, "
                        "attempts, max_attempts, created_at, updated_at, acceptance_evidence) "
                        "VALUES ('obm_bad_evidence', 'org_notifications', 'webhook', 'accepted', '{}', '[]', "
                        "'[]', '{}', 1, 3, '2026-11-03 12:00:00', '2026-11-03 12:00:00', 'recipient_delivered')"
                    )
                )

        with pytest.raises(RuntimeError, match="cannot drop notification acceptance evidence"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM outbound_messages WHERE id='obm_accepted'"))
        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            legacy_after = connection.execute(
                sa.text("SELECT status, sent_at FROM outbound_messages WHERE id='obm_legacy_sent'")
            ).one()
            assert legacy_after.status == "sent"
            assert legacy_after.sent_at is not None
    finally:
        engine.dispose()
