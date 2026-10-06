"""telemetry spine: channel identity, point identity, alert rules, outbox

Four repairs and two new tables, all of them consequences of taking real-time data seriously.

**A channel's identity was its key, and only one organization could hold it.** ``time_series``
declared ``UniqueConstraint("org_id", "channel_key")`` — one ``wob`` for the whole tenant. The second
well's weight on bit could not be recorded at all, so the constraint is replaced by the identity the
domain actually has: scope (well / wellbore / operation) + key + dimension, with the scope stored as a
canonical token so the database enforces exactly what the service looks up. Existing rows are
backfilled from the columns they already carry; where that backfill would produce two rows with the
same identity — which the old constraint made impossible within a tenant, and which therefore can only
happen if a row was written with no well at all — the migration refuses rather than guessing which row
to keep.

**A point had no identity.** ``time_series_points`` accepted the same measurement twice: a re-sent
frame became a second row, two rows for one instant were indistinguishable from two real readings, and
nothing recorded when a value actually arrived. This revision adds ``source_point_id``,
``fingerprint``, ``dedup_key``, ``received_at``, ``is_late``, ``is_out_of_order`` and the source's own
value/unit, and enforces ``UNIQUE (series_id, dedup_key)``. Existing rows are given the fingerprint they
would have received at ingestion — the same documented function the application uses, copied here so a
migration cannot change meaning when the application's helper is refactored — and duplicates that the
new constraint would reject are collapsed to their earliest row, which is the reading the platform
received first.

**Alerts had nowhere to say why they fired.** The ``alerts`` table gains the series, the source point,
the section, the rule, the sustained duration and the clearing observation, so "why was this raised?"
is answered from the row instead of reconstructed from a log.

**A notification could outlive its transaction, or die with it.** ``outbox_events`` records the domain
event inside the writer's transaction, with a per-organization sequence a consumer can use to notice a
gap, and ``alert_rules`` stores deterministic conditions as data — never as an expression to evaluate.

The uniqueness rules below are written for both dialects: ``sa.UniqueConstraint`` and ``sa.Index`` are
created through ``op.create_table``/``op.create_index`` rather than by raw DDL, because SQLite and
PostgreSQL disagree about ``NULL`` in a unique key — two rows with ``NULL`` in a constrained column are
"distinct" to PostgreSQL and "equal" to SQLite — and the identity token is therefore NOT NULL for every
row the application writes from this revision onward.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "d5a71c93e2f8"
down_revision = "b8f2a6d31c04"
branch_labels = None
depends_on = None

#: Quality states the model declares (``drillai.telemetry.vocabulary.QUALITY_STATES``). The repair
#: below maps anything stored before this revision onto one of them — a quality the application no
#: longer recognises is worse than a coarser one it does.
QUALITY_ALIASES = {
    "ok": "good",
    "valid": "good",
    "uncertain": "suspect",
    "questionable": "suspect",
    "error": "bad",
    "invalid": "bad",
    "null": "missing",
    "unknown": "missing",
    "calculated": "estimated",
    "computed": "estimated",
}


def _scope_token(
    *, well_id: str | None, wellbore_id: str | None, operation_id: str | None
) -> str | None:
    """The identity token, computed exactly as ``drillai.telemetry.identity.scope_token`` does.

    Copied rather than imported: a migration must keep meaning the same thing after the application's
    helper is refactored, and this function is four lines long precisely so that it can be.
    """

    if not well_id:
        return None
    parts = [well_id]
    if wellbore_id:
        parts.append(wellbore_id)
    if operation_id:
        parts.append(operation_id)
    return "/".join(parts)


def _fingerprint(
    *,
    series_id: str,
    ts: dt.datetime | None,
    value: float | None,
    source_ref: str | None,
    sequence: int | None = None,
) -> str:
    """The point fingerprint, computed exactly as the application computes it.

    Same reasoning as :func:`_scope_token`: the value written here and the value computed later must be
    the same string for the same measurement, or a re-sent historical frame would create a duplicate
    the constraint was supposed to prevent.
    """

    if ts is None:
        stamp = ""
    else:
        aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
        stamp = aware.astimezone(dt.UTC).isoformat()
    payload = json.dumps(
        [series_id, stamp, repr(value), source_ref or "", sequence if sequence is not None else ""],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------------------------------------------------------- channels
    with op.batch_alter_table("time_series", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope_token", sa.String(200), nullable=True))
        batch_op.drop_constraint("uq_time_series_org_id", type_="unique") if False else None

    # The old constraint was unnamed in the model, so its name is whatever the dialect generated.
    # Dropping it by inspection keeps this migration honest on a database created from the model and on
    # one created by the initial revision's raw DDL.
    inspector = sa.inspect(bind)
    for constraint in inspector.get_unique_constraints("time_series"):
        columns = list(constraint.get("column_names") or [])
        if columns == ["org_id", "channel_key"]:
            name = constraint.get("name")
            if name:
                with op.batch_alter_table("time_series", schema=None) as batch_op:
                    batch_op.drop_constraint(name, type_="unique")

    # Backfill the token, and repair the two vocabularies that drifted from the model's own tuple.
    rows = bind.execute(
        sa.text(
            "SELECT id, well_id, wellbore_id, operation_id, channel_key, dimension, source FROM time_series"
        )
    ).fetchall()
    identities: dict[tuple[str, str, str], str] = {}
    for row in rows:
        series_id, well_id, wellbore_id, operation_id, channel_key, dimension, source = row
        token = _scope_token(
            well_id=well_id, wellbore_id=wellbore_id, operation_id=operation_id
        )
        if token is None:
            # A channel with no well cannot be placed on a rig, and every well-scoped read would miss
            # it. Refusing here is the only honest option: choosing a well for it would be inventing a
            # fact about where a measurement came from.
            raise RuntimeError(
                f"time_series row {series_id} has no well_id; its channel identity cannot be derived. "
                "Delete or attach the row before upgrading — the platform will not guess."
            )
        key = (token, (channel_key or "").strip().lower(), (dimension or "").strip())
        if key in identities:
            raise RuntimeError(
                f"time_series rows {identities[key]} and {series_id} are the same channel "
                f"({key[0]}|{key[1]}|{key[2]}); the platform will not choose which to keep"
            )
        identities[key] = series_id
        bind.execute(
            sa.text("UPDATE time_series SET scope_token = :token WHERE id = :id"),
            {"token": token, "id": series_id},
        )
        if source not in ("manual", "synthetic", "witsml", "etp", "opcua", "modbus", "csv_import",
                          "ddr_extraction", "engine", "integration", "system"):
            # The old model defaulted this column to "manual" and nothing validated it. An unknown
            # source is mapped to the closest honest reading: how the row was actually written.
            bind.execute(
                sa.text("UPDATE time_series SET source = 'integration' WHERE id = :id"),
                {"id": series_id},
            )

    with op.batch_alter_table("time_series", schema=None) as batch_op:
        batch_op.alter_column("scope_token", existing_type=sa.String(200), nullable=False)
        batch_op.create_unique_constraint(
            "uq_time_series_identity", ["org_id", "scope_token", "channel_key", "dimension"]
        )
        batch_op.create_index("ix_time_series_scope_token", ["scope_token"])
        batch_op.create_index("ix_time_series_well_channel", ["org_id", "well_id", "channel_key"])
        batch_op.create_index("ix_time_series_bore_channel", ["org_id", "wellbore_id", "channel_key"])

    # ---------------------------------------------------------------- points
    with op.batch_alter_table("time_series_points", schema=None) as batch_op:
        batch_op.add_column(sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("source_point_id", sa.String(160), nullable=True))
        batch_op.add_column(sa.Column("source_ref", sa.String(200), nullable=True))
        batch_op.add_column(sa.Column("fingerprint", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("dedup_key", sa.String(160), nullable=True))
        batch_op.add_column(
            sa.Column("is_late", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("is_out_of_order", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("src_value", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("src_unit", sa.String(40), nullable=True))

    point_rows = bind.execute(
        sa.text(
            "SELECT p.id, p.series_id, p.ts, p.value, p.quality, p.sequence, p.created_at, "
            "s.source_ref AS channel_source_ref, s.last_ts AS channel_last_ts "
            "FROM time_series_points p LEFT JOIN time_series s ON s.id = p.series_id "
            "ORDER BY p.series_id, p.ts, p.created_at"
        )
    ).fetchall()

    seen: dict[tuple[str, str], str] = {}
    for row in point_rows:
        (
            point_id,
            series_id,
            ts,
            value,
            quality,
            sequence,
            created_at,
            channel_source_ref,
            _channel_last_ts,
        ) = row
        canonical_quality = (quality or "good").strip().lower()
        canonical_quality = QUALITY_ALIASES.get(canonical_quality, canonical_quality)
        if canonical_quality not in (
            "good", "suspect", "bad", "missing", "estimated", "late", "duplicate", "out_of_order",
        ):
            canonical_quality = "suspect"
        digest = _fingerprint(
            series_id=series_id, ts=ts, value=value, source_ref=channel_source_ref, sequence=sequence
        )
        identity = (series_id, digest)
        if identity in seen:
            # A duplicate the old schema permitted. Keep the row received first — it is the reading the
            # platform acted on — and drop the later copy, which carried no information the first did
            # not: same series, same instant, same value.
            bind.execute(
                sa.text("DELETE FROM time_series_points WHERE id = :id"), {"id": point_id}
            )
            continue
        seen[identity] = point_id
        # `received_at` was never recorded; `created_at` is when the row was written, which is the
        # closest true statement available, and it is recorded as such rather than as the source time.
        bind.execute(
            sa.text(
                "UPDATE time_series_points SET received_at = :received, fingerprint = :fingerprint, "
                "dedup_key = :dedup_key, src_value = value, quality = :quality WHERE id = :id"
            ),
            {
                "received": created_at,
                "fingerprint": digest,
                "dedup_key": digest,
                "quality": canonical_quality,
                "id": point_id,
            },
        )

    with op.batch_alter_table("time_series_points", schema=None) as batch_op:
        batch_op.alter_column("dedup_key", existing_type=sa.String(160), nullable=False)
        batch_op.create_unique_constraint(
            "uq_time_series_point_identity", ["series_id", "dedup_key"]
        )
        batch_op.create_index("ix_time_series_points_series_ts", ["series_id", "ts"])
        batch_op.create_index("ix_time_series_points_quality", ["quality"])
        batch_op.create_index("ix_time_series_points_received_at", ["received_at"])
        batch_op.create_index(
            "ix_time_series_points_source_identity", ["source_ref", "source_point_id"]
        )

    # ---------------------------------------------------------------- alerts
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("series_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("source_point_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("section_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("rule_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("sustained_seconds", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("clear_observed_value", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("cancelled_reason", sa.Text(), nullable=True))
        batch_op.create_index("ix_alerts_series_id", ["series_id"])
        batch_op.create_index("ix_alerts_section_id", ["section_id"])
        batch_op.create_index("ix_alerts_rule_id", ["rule_id"])

    # The alert life cycle is the vocabulary the service enforces. Rows written before it existed
    # carried "open"/"ack"/"resolved"; each maps onto exactly one canonical state, and the mapping is
    # stated here so the value in the database and the value the API returns cannot disagree.
    for stored, canonical in (
        ("open", "raised"),
        ("ack", "acknowledged"),
        ("acknowledged", "acknowledged"),
        ("resolved", "cleared"),
        ("closed", "cleared"),
        ("cleared", "cleared"),
        ("cancelled", "cancelled"),
        ("raised", "raised"),
    ):
        bind.execute(
            sa.text("UPDATE alerts SET status = :canonical WHERE status = :stored"),
            {"canonical": canonical, "stored": stored},
        )
    for severity in ("info", "warning", "minor"):
        bind.execute(
            sa.text("UPDATE alerts SET severity = 'low' WHERE severity = :stored"), {"stored": severity}
        )
    for severity in ("major", "severe"):
        bind.execute(
            sa.text("UPDATE alerts SET severity = 'high' WHERE severity = :stored"),
            {"stored": severity},
        )
    for severity in ("fatal", "emergency"):
        bind.execute(
            sa.text("UPDATE alerts SET severity = 'critical' WHERE severity = :stored"),
            {"stored": severity},
        )

    # ---------------------------------------------------------------- new tables
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False, index=True),
        sa.Column("rule_key", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("channel_key", sa.String(160), nullable=False),
        sa.Column("scope_token", sa.String(200), nullable=True),
        sa.Column("well_id", sa.String(64), nullable=True),
        sa.Column("wellbore_id", sa.String(64), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=True),
        sa.Column("operator", sa.String(8), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("clear_operator", sa.String(8), nullable=True),
        sa.Column("clear_threshold", sa.Float(), nullable=True),
        sa.Column("sustain_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("clear_sustain_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cooldown_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "rule_key", name="uq_alert_rules_key"),
    )
    op.create_index("ix_alert_rules_channel", "alert_rules", ["org_id", "channel_key"])
    op.create_index("ix_alert_rules_rule_key", "alert_rules", ["rule_key"])
    op.create_index("ix_alert_rules_scope_token", "alert_rules", ["scope_token"])
    op.create_index("ix_alert_rules_well_id", "alert_rules", ["well_id"])
    op.create_index("ix_alert_rules_wellbore_id", "alert_rules", ["wellbore_id"])
    op.create_index("ix_alert_rules_operation_id", "alert_rules", ["operation_id"])
    op.create_index("ix_alert_rules_is_enabled", "alert_rules", ["is_enabled"])
    op.create_index("ix_alert_rules_created_at", "alert_rules", ["created_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False, index=True),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("subject_kind", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("well_id", sa.String(64), nullable=True),
        sa.Column("wellbore_id", sa.String(64), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "sequence", name="uq_outbox_events_sequence"),
    )
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"])
    op.create_index("ix_outbox_events_well_sequence", "outbox_events", ["org_id", "well_id", "sequence"])
    op.create_index("ix_outbox_events_unpublished", "outbox_events", ["published_at", "sequence"])
    op.create_index("ix_outbox_events_well_id", "outbox_events", ["well_id"])
    op.create_index("ix_outbox_events_wellbore_id", "outbox_events", ["wellbore_id"])
    op.create_index("ix_outbox_events_operation_id", "outbox_events", ["operation_id"])
    op.create_index("ix_outbox_events_occurred_at", "outbox_events", ["occurred_at"])
    op.create_index("ix_outbox_events_created_at", "outbox_events", ["created_at"])


def downgrade() -> None:
    """Remove the two new tables and the new columns. The repairs are not reversed.

    Restoring the old quality spellings, the old alert statuses or the duplicate channel identity would
    hand back a database whose values the application no longer recognises — and, for the identity, one
    that cannot be written to at all. The vocabulary repairs and the collapsed duplicate points
    therefore stay; the columns and tables this revision added go.
    """

    op.drop_index("ix_outbox_events_created_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_occurred_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_operation_id", table_name="outbox_events")
    op.drop_index("ix_outbox_events_wellbore_id", table_name="outbox_events")
    op.drop_index("ix_outbox_events_well_id", table_name="outbox_events")
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_index("ix_outbox_events_well_sequence", table_name="outbox_events")
    op.drop_index("ix_outbox_events_event_type", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_alert_rules_created_at", table_name="alert_rules")
    op.drop_index("ix_alert_rules_is_enabled", table_name="alert_rules")
    op.drop_index("ix_alert_rules_operation_id", table_name="alert_rules")
    op.drop_index("ix_alert_rules_wellbore_id", table_name="alert_rules")
    op.drop_index("ix_alert_rules_well_id", table_name="alert_rules")
    op.drop_index("ix_alert_rules_scope_token", table_name="alert_rules")
    op.drop_index("ix_alert_rules_rule_key", table_name="alert_rules")
    op.drop_index("ix_alert_rules_channel", table_name="alert_rules")
    op.drop_table("alert_rules")

    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_index("ix_alerts_rule_id")
        batch_op.drop_index("ix_alerts_section_id")
        batch_op.drop_index("ix_alerts_series_id")
        batch_op.drop_column("cancelled_reason")
        batch_op.drop_column("observed_at")
        batch_op.drop_column("clear_observed_value")
        batch_op.drop_column("sustained_seconds")
        batch_op.drop_column("rule_id")
        batch_op.drop_column("section_id")
        batch_op.drop_column("source_point_id")
        batch_op.drop_column("series_id")

    with op.batch_alter_table("time_series_points", schema=None) as batch_op:
        batch_op.drop_index("ix_time_series_points_received_at")
        batch_op.drop_index("ix_time_series_points_quality")
        batch_op.drop_index("ix_time_series_points_source_identity")
        batch_op.drop_index("ix_time_series_points_series_ts")
        batch_op.drop_constraint("uq_time_series_point_identity", type_="unique")
        batch_op.drop_column("src_unit")
        batch_op.drop_column("src_value")
        batch_op.drop_column("is_out_of_order")
        batch_op.drop_column("is_late")
        batch_op.drop_column("dedup_key")
        batch_op.drop_column("fingerprint")
        batch_op.drop_column("source_ref")
        batch_op.drop_column("source_point_id")
        batch_op.drop_column("received_at")

    with op.batch_alter_table("time_series", schema=None) as batch_op:
        batch_op.drop_index("ix_time_series_scope_token")
        batch_op.drop_index("ix_time_series_bore_channel")
        batch_op.drop_index("ix_time_series_well_channel")
        batch_op.drop_constraint("uq_time_series_identity", type_="unique")
        batch_op.drop_column("scope_token")
        batch_op.create_unique_constraint("uq_time_series_org_id", ["org_id", "channel_key"])
