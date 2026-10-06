"""The telemetry service: channels, batch ingestion, windows and latest values.

Three properties this module is built around, because each one was absent and each one hurt:

**One bounded query, whatever the well has.** ``latest`` resolves the newest reading per channel with a
single window function over a bounded set of channels — not one query per channel. The number of
statements a request issues is asserted in the suite; a per-channel loop is an N+1 that looks correct
until a rig streams forty channels.

**Ingestion reconciles.** A batch reports what it received, accepted, recognised as a replay, refused
and classified as late or out of order — and every one of those numbers is a count of rows that were
actually read or written, not an estimate. A caller that is told ``accepted=982`` can find 982 rows.

**Nothing is silently coerced.** Values are converted once, through the one unit engine; a value whose
unit the platform cannot convert is refused with the reason, not stored as though it were canonical; a
quality outside the vocabulary is refused rather than written and discovered by a reader.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.clock import utc_now
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import AlertRule, TimeSeries, TimeSeriesPoint
from drillai.telemetry.identity import (
    channel_identity,
    point_identity,
    resolve_channel_scope,
    scope_of,
    scope_token,
)
from drillai.telemetry.units import CANONICAL_UNIT, convert
from drillai.telemetry.vocabulary import (
    KPI_CHANNEL_LABELS,
    MAX_BATCH_POINTS,
    MAX_LATEST_CHANNELS,
    QUALITY_STATES,
    REPLAY_POLICIES,
    SOURCE_KINDS,
)

#: A page of points never exceeds this, whatever the caller asks for. A range query with no bound is a
#: whole-series load wearing a query, and the memory it needs grows with the well.
MAX_WINDOW_POINTS = 2000

#: How many identities one statement may carry. SQLite historically caps a statement's parameters at
#: 999 and Postgres at 65 535; four hundred keeps both dialects comfortable and the batch at a handful
#: of statements rather than one per point.
_IDENTITY_CHUNK = 400


def _chunks(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def conflict_aware_insert(dialect: str):
    """The INSERT the platform uses to store a measurement that may already exist.

    ``ON CONFLICT DO NOTHING`` is the database-level half of the replay guarantee: even if two workers
    race, only one row is written and the loser learns it from the statement's return rather than from
    an exception. Postgres and SQLite both support it with different import paths; a dialect the
    platform has never been run against is refused *by name*, because the alternative — compiling
    something that silently overwrites a stored measurement — is exactly the failure this guards.
    """

    if dialect == "postgresql":
        return postgresql_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise ValidationFailed(
        f"telemetry ingestion has no tested conflict clause for the {dialect} dialect",
        details={"dialect": dialect, "supported": ["postgresql", "sqlite"]},
    )


@dataclass(frozen=True)
class PointIn:
    """One measurement as it arrives, before normalisation.

    ``unit`` is the unit *the source sent*. When it is absent the channel's own source unit is used, and
    when there is neither the point is refused — the platform will not assume a unit for a number it is
    about to store as a measurement.
    """

    ts: dt.datetime
    value: float | None
    unit: str | None = None
    quality: str = "good"
    source_point_id: str | None = None
    source_ref: str | None = None
    sequence: int | None = None
    depth_md_si: float | None = None
    received_at: dt.datetime | None = None


@dataclass
class Rejection:
    index: int
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestReport:
    """What an ingestion actually did, with the numbers reconciling against rows."""

    channel_id: str
    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    late: int = 0
    out_of_order: int = 0
    revised: int = 0
    quality_counts: dict[str, int] = field(default_factory=dict)
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None
    rejections: list[Rejection] = field(default_factory=list)
    #: Points that carry a `source_point_id` the platform has already stored with a *different* value.
    #: A conflict is not a duplicate: the source is asserting something new about a moment it already
    #: described, and the caller decides (see ``on_conflict``).
    conflicts: list[Rejection] = field(default_factory=list)

    @property
    def written(self) -> int:
        """Rows this call created or updated. ``accepted`` counts insertions; a revision is not one."""

        return self.accepted + self.revised

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "received": self.received,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "late": self.late,
            "out_of_order": self.out_of_order,
            "revised": self.revised,
            "written": self.written,
            "quality_counts": dict(sorted(self.quality_counts.items())),
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "conflicts": [item.reason for item in self.conflicts],
            "rejections": [
                {"index": item.index, "reason": item.reason, **item.details}
                for item in self.rejections[:50]
            ],
            "reconciled": self.received == self.accepted + self.duplicates + self.rejected + self.revised,
        }


@dataclass(frozen=True)
class LatestReading:
    """The newest measurement on a channel, with everything a screen must show beside the number."""

    channel_id: str
    channel_key: str
    label: str
    dimension: str
    unit: str
    value: float | None
    quality: str
    quality_flags: tuple[str, ...]
    observed_at: dt.datetime | None
    received_at: dt.datetime | None
    age_seconds: float | None
    freshness: str
    source: str
    source_ref: str | None
    is_late: bool


@dataclass(frozen=True)
class WindowPage:
    """A page of points plus what the client needs to know about the whole series."""

    channel_id: str
    points: list[TimeSeriesPoint]
    count: int
    returned: int
    next_cursor: str | None
    limit: int
    unit: str
    truncated: bool


class TelemetryService:
    """Every telemetry read and write in one place, with the tenant boundary applied first."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        fresh_seconds: float = 30.0,
        stale_seconds: float = 300.0,
        late_seconds: float = 120.0,
        max_latest_channels: int = MAX_LATEST_CHANNELS,
        max_window_points: int = MAX_WINDOW_POINTS,
        max_batch_points: int = MAX_BATCH_POINTS,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.fresh_seconds = fresh_seconds
        self.stale_seconds = stale_seconds
        self.late_seconds = late_seconds
        self.max_latest_channels = min(max_latest_channels, MAX_LATEST_CHANNELS)
        self.max_window_points = min(max_window_points, MAX_WINDOW_POINTS)
        self.max_batch_points = min(max_batch_points, MAX_BATCH_POINTS)

    # ------------------------------------------------------------------ freshness

    def freshness(self, observed_at: dt.datetime | None, *, now: dt.datetime | None = None) -> tuple[str, float | None]:
        """``(state, age_seconds)`` for a value.

        Four outcomes, and they are four different statements:

        * ``missing``  — there is no value at all;
        * ``unknown``  — there is a value whose age the platform cannot compute;
        * ``fresh``    — measured within ``fresh_seconds``;
        * ``stale``    — measured longer ago than that.

        The thresholds come from configuration so the browser and the API never disagree about whether
        a number is current.
        """

        if observed_at is None:
            return "missing", None
        if observed_at.tzinfo is None:
            # A naive instant is not a measurement with an unknown age; it is a row written by something
            # that ignored the platform's UTC convention. Saying "unknown" is honest; guessing a zone
            # would silently move the measurement in time.
            return "unknown", None
        moment = now or utc_now()
        age = (moment - observed_at).total_seconds()
        if age < 0:
            # A source clock ahead of ours. The value is not stale — it is from the future, which is a
            # clock problem, so its freshness is unknown rather than fresh.
            return "unknown", age
        return ("fresh" if age <= self.fresh_seconds else "stale"), age

    # ------------------------------------------------------------------ channels

    async def create_channel(
        self,
        *,
        channel_key: str,
        name: str,
        dimension: str,
        well_id: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        unit: str | None = None,
        is_realtime: bool = False,
        source: str = "manual",
        source_ref: str | None = None,
        description: str | None = None,
        sampling_hint_seconds: float | None = None,
        principal: Any = None,
    ) -> tuple[TimeSeries, bool]:
        """Register a channel, or return the one that identity already names.

        Returns ``(channel, created)``. The second value is not decoration: a client that retries a
        creation after a timeout must be able to tell "I created it" from "it already existed", and the
        database is what decides — the unique constraint, not a check-then-insert that two concurrent
        callers can both pass.
        """

        scope = await resolve_channel_scope(
            self.session,
            org_id=self.org_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            operation_id=operation_id,
        )
        if source not in SOURCE_KINDS:
            raise ValidationFailed(
                "the source is not one the platform recognises",
                details={"field": "source", "value": source, "allowed": list(SOURCE_KINDS)},
            )
        canonical_unit = CANONICAL_UNIT.get(dimension)
        if canonical_unit is None:
            raise ValidationFailed(
                "the dimension is not one the platform can store",
                details={"field": "dimension", "value": dimension, "allowed": sorted(CANONICAL_UNIT)},
            )
        source_unit = unit or canonical_unit
        # The source unit is validated *now*, not on the first point: a channel that declares a unit its
        # dimension cannot convert would accept one measurement and refuse the next.
        conversion = convert(0.0, source_unit, dimension)
        token = scope_token(**scope)  # type: ignore[arg-type]

        existing = await self.find_channel(
            well_id=well_id, wellbore_id=wellbore_id, operation_id=operation_id,
            channel_key=channel_key, dimension=dimension,
        )
        if existing is not None:
            return existing, False

        channel = TimeSeries(
            id=new_id("tms"),
            org_id=self.org_id,
            well_id=scope["well_id"],
            wellbore_id=scope["wellbore_id"],
            operation_id=scope["operation_id"],
            scope_token=token,
            channel_key=channel_key.strip().lower(),
            name=name,
            dimension=dimension,
            unit=conversion.unit,
            src_unit=source_unit,
            description=description,
            is_realtime=is_realtime,
            source=source,
            source_ref=source_ref,
            sampling_hint_seconds=sampling_hint_seconds,
            attributes={},
        )
        # The unique index on (org, scope, channel_key, dimension) is what makes two concurrent creators
        # safe. Inserting *through* it with a conflict clause rather than hoping a flush does not raise —
        # a unique violation inside a savepoint leaves the transaction unusable on SQLite (verified), and
        # a caller that lost a race must still be able to read the row the winner wrote.
        insert = conflict_aware_insert(self.session.get_bind().dialect.name)
        statement = (
            insert(TimeSeries)
            .values(
                id=channel.id,
                org_id=channel.org_id,
                well_id=channel.well_id,
                wellbore_id=channel.wellbore_id,
                operation_id=channel.operation_id,
                scope_token=channel.scope_token,
                channel_key=channel.channel_key,
                name=channel.name,
                dimension=channel.dimension,
                unit=channel.unit,
                src_unit=channel.src_unit,
                description=channel.description,
                is_realtime=channel.is_realtime,
                source=channel.source,
                source_ref=channel.source_ref,
                sampling_hint_seconds=channel.sampling_hint_seconds,
                attributes=channel.attributes,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "scope_token", "channel_key", "dimension"]
            )
            .returning(TimeSeries.id)
        )
        created_id = (await self.session.execute(statement)).scalar_one_or_none()
        if created_id is None:
            winner = await self.find_channel(
                well_id=well_id, wellbore_id=wellbore_id, operation_id=operation_id,
                channel_key=channel_key, dimension=dimension,
            )
            if winner is None:  # pragma: no cover - the constraint failed for another reason
                raise Conflict(
                    "a channel with this identity exists but could not be read back",
                    details={"channel_key": channel_key, "dimension": dimension},
                )
            return winner, False
        stored = (
            await self.session.execute(select(TimeSeries).where(TimeSeries.id == created_id))
        ).scalar_one()

        await record_audit(
            self.session,
            org_id=self.org_id,
            action="timeseries.create",
            resource_kind="time_series",
            resource_id=stored.id,
            principal=principal,
            after={
                "channel_key": stored.channel_key,
                "dimension": channel.dimension,
                "unit": channel.unit,
                "source_unit": source_unit,
                "scope_token": token,
            },
            details={"scope": scope_of(token)},
            well_id=scope["well_id"],
        )
        return stored, True

    async def find_channel(
        self,
        *,
        well_id: str,
        channel_key: str,
        dimension: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
    ) -> TimeSeries | None:
        token = channel_identity(
            well_id=well_id,
            wellbore_id=wellbore_id,
            operation_id=operation_id,
            channel_key=channel_key,
            dimension=dimension,
        )
        identity = token.split("|")
        stmt = select(TimeSeries).where(
            TimeSeries.org_id == self.org_id,
            TimeSeries.scope_token == identity[0],
            TimeSeries.channel_key == identity[1],
            TimeSeries.dimension == identity[2],
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def channel_by_id(self, series_id: str) -> TimeSeries:
        channel = (
            await self.session.execute(
                select(TimeSeries).where(
                    TimeSeries.id == series_id, TimeSeries.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if channel is None:
            # A channel in another organization is not "forbidden" — the caller is not entitled to learn
            # that it exists.
            raise NotFound("channel not found", details={"series_id": series_id})
        return channel

    def channel_statement(
        self,
        *,
        well_id: str | None = None,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        channel_key: str | None = None,
        dimension: str | None = None,
        is_realtime: bool | None = None,
        source: str | None = None,
    ):
        """The filter every channel read shares, so a list and a count can never disagree."""

        clauses = [TimeSeries.org_id == self.org_id]
        if well_id is not None:
            clauses.append(TimeSeries.well_id == well_id)
        if wellbore_id is not None:
            clauses.append(TimeSeries.wellbore_id == wellbore_id)
        if operation_id is not None:
            clauses.append(TimeSeries.operation_id == operation_id)
        if channel_key is not None:
            clauses.append(TimeSeries.channel_key == channel_key.strip().lower())
        if dimension is not None:
            clauses.append(TimeSeries.dimension == dimension)
        if is_realtime is not None:
            clauses.append(TimeSeries.is_realtime.is_(is_realtime))
        if source is not None:
            clauses.append(TimeSeries.source == source)
        return select(TimeSeries).where(*clauses).order_by(TimeSeries.channel_key, TimeSeries.id)

    async def list_channels(self, *, limit: int = 100, offset: int = 0, **filters) -> tuple[list[TimeSeries], int]:
        stmt = self.channel_statement(**filters)
        total = int(
            (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )
        rows = list((await self.session.execute(stmt.limit(limit).offset(offset))).scalars().all())
        return rows, total

    # ------------------------------------------------------------------ ingestion

    async def append_points(
        self,
        series_id: str,
        points: Sequence[PointIn],
        *,
        source_ref: str | None = None,
        default_unit: str | None = None,
        on_conflict: str = "reject",
        principal: Any = None,
        received_at: dt.datetime | None = None,
    ) -> IngestReport:
        """Append a batch of measurements.

        Semantics, stated because each is a decision:

        * **Bounded.** A batch larger than ``max_batch_points`` is refused rather than truncated: a
          caller that sent a million rows and was told 5 000 landed would not know which 5 000.
        * **Atomic as a batch, tolerant of bad rows.** Every valid point commits together; a point that
          fails validation is refused with its reason and does not prevent the rest. A database error
          rolls the whole batch back — there is no half-written batch.
        * **Idempotent.** Identity is ``source_point_id`` when the source has one, else the documented
          fingerprint. A replay is counted as a duplicate and writes nothing.
        * **Explicit about conflicts.** The same source identity arriving with a *different* value is a
          conflict: ``reject`` (default) refuses it and says so; ``revise`` updates the stored point in
          place and appends the previous value to its revision trail. Neither silently overwrites.
        * **Honest about arrival.** ``is_late`` is set when the point arrived more than
          ``late_seconds`` after it was taken, and ``is_out_of_order`` when it is older than a point the
          channel already holds. Both are recorded, and neither moves the point in time.
        """

        if on_conflict not in REPLAY_POLICIES:
            raise ValidationFailed(
                "unknown replay policy",
                details={"field": "on_conflict", "value": on_conflict, "allowed": list(REPLAY_POLICIES)},
            )
        if len(points) > self.max_batch_points:
            raise ValidationFailed(
                f"a batch may not exceed {self.max_batch_points} points",
                details={"field": "points", "value": len(points), "max": self.max_batch_points},
            )
        channel = await self.channel_by_id(series_id)
        now = received_at or utc_now()
        report = IngestReport(channel_id=channel.id, received=len(points))
        if not points:
            return report

        # ---- pass 1: validate and normalise, refusing row by row and never raising for one bad row.
        prepared: list[_Prepared] = []
        for index, point in enumerate(points):
            outcome = self._prepare_point(
                channel=channel,
                point=point,
                default_unit=default_unit or channel.src_unit or channel.unit,
                now=now,
                index=index,
            )
            if isinstance(outcome, Rejection):
                report.rejected += 1
                report.rejections.append(outcome)
            else:
                prepared.append(outcome)

        # ---- pass 2: one statement (per four hundred identities) asking what the channel already
        # holds. This is what lets the batch classify replays without a flush per point, and it is what
        # a per-point existence check would turn into the N+1 this module exists to avoid.
        known = await self._existing_points(channel.id, [item.identity.dedup_key for item in prepared])

        # ---- pass 3: everything not already stored goes in as a conflict-aware insert. The rows the
        # statement actually created come back, so a duplicate that raced *this* batch is classified
        # from what the database did, and a unique violation is never an exception to be swallowed.
        candidates = [item for item in prepared if item.identity.dedup_key not in known]
        # Out of order is a fact about the stored timeline, so it is decided before the write that
        # carries the flag: the horizon starts at what the channel already holds and advances through
        # the batch in the order the caller sent. An identity that is already stored does not advance it
        # — whatever it contributed to the horizon did so when it was first written, in the same
        # transaction that updated ``time_series.last_ts``.
        horizon = _aware(channel.last_ts)
        for item in candidates:
            item.row.is_out_of_order = horizon is not None and item.row.ts < horizon
            if horizon is None or item.row.ts > horizon:
                horizon = item.row.ts
        landed = await self._insert_points([item.row for item in candidates])

        for item in prepared:
            key = item.identity.dedup_key
            row = item.row
            # The identities the insert actually created, *consumed* as the batch is walked: one landing
            # is one row, so a batch that names the same identity twice can count it as accepted once.
            # The second occurrence falls through to the replay classification and is compared against
            # the row the first one wrote — which is how an external replay is treated too.
            if key in landed:
                landed.discard(key)
                report.accepted += 1
            else:
                stored = known.get(key) or await self._read_point(channel.id, key)
                if stored is None:  # pragma: no cover - the row vanished between the two statements
                    raise Conflict(
                        "a measurement was neither stored nor found where it should have been",
                        details={"channel_id": channel.id, "dedup_key": key},
                    )
                outcome = await self._classify_replay(
                    channel_id=channel.id, row=row, stored=stored, on_conflict=on_conflict, index=item.index,
                )
                if outcome == "duplicate":
                    report.duplicates += 1
                    continue
                if outcome == "conflict":
                    report.rejected += 1
                    report.conflicts.append(
                        Rejection(
                            index=item.index,
                            reason="a measurement with this source identity already exists with a different value",
                            details={
                                "source_point_id": row.source_point_id,
                                "incoming_value": row.value,
                                "stored_value": stored["value"],
                                "policy": "reject",
                            },
                        )
                    )
                    continue
                report.revised += 1

            report.quality_counts[row.quality] = report.quality_counts.get(row.quality, 0) + 1
            if row.is_late:
                report.late += 1
            if row.is_out_of_order:
                report.out_of_order += 1
            if report.first_ts is None or row.ts < report.first_ts:
                report.first_ts = row.ts
            if report.last_ts is None or row.ts > report.last_ts:
                report.last_ts = row.ts

        # The channel's own bookkeeping, in the same transaction as the points it describes. It is a
        # cache of what the points say, and it is derived from them: a reader can always recompute it
        # from ``time_series_points``.
        written = [row for row in (report.first_ts, report.last_ts) if row is not None]
        if written and report.written:
            if channel.first_ts is None or (report.first_ts is not None and report.first_ts < _aware(channel.first_ts)):
                channel.first_ts = report.first_ts
            if channel.last_ts is None or (report.last_ts is not None and report.last_ts > _aware(channel.last_ts)):
                channel.last_ts = report.last_ts
            channel.point_count = int(channel.point_count or 0) + report.accepted
            if source_ref:
                channel.source_ref = source_ref

        await self.session.flush()
        # One audit row per batch, with the metadata — never the payload. A million measurements are not
        # an audit record; "9 998 accepted, 2 refused at 09:31:20" is.
        await record_audit(
            self.session,
            org_id=self.org_id,
            action="timeseries.append",
            resource_kind="time_series",
            resource_id=channel.id,
            principal=principal,
            after={
                "received": report.received,
                "accepted": report.accepted,
                "duplicates": report.duplicates,
                "rejected": report.rejected,
                "late": report.late,
                "out_of_order": report.out_of_order,
                "revised": report.revised,
            },
            details={
                "channel_key": channel.channel_key,
                "source_ref": source_ref or channel.source_ref,
                "first_ts": report.first_ts.isoformat() if report.first_ts else None,
                "last_ts": report.last_ts.isoformat() if report.last_ts else None,
            },
            well_id=channel.well_id,
        )
        return report

    def _prepare_point(
        self,
        *,
        channel: TimeSeries,
        point: PointIn,
        default_unit: str,
        now: dt.datetime,
        index: int,
    ) -> _Prepared | Rejection:
        """Validate and normalise one arriving measurement. Refusals are returned, never raised: one bad
        row in a batch of a thousand must not cost the other 999.

        Nothing here decides whether the point is a replay or whether it is out of order — those are
        facts about what the channel already holds, and they are resolved against the database rather
        than guessed from the batch. What is decided here is everything that is true of the point alone:
        its instant, its quality, its canonical value, and the identity a later replay will match on.
        """

        try:
            if point.ts is None:
                raise ValidationFailed("a measurement must have a source timestamp", details={"field": "ts"})
            ts = _aware(point.ts)
            quality = (point.quality or "good").strip().lower()
            if quality not in QUALITY_STATES:
                raise ValidationFailed(
                    "the quality is not one the platform records",
                    details={"field": "quality", "value": point.quality, "allowed": list(QUALITY_STATES)},
                )
            conversion = convert(point.value, point.unit or default_unit, channel.dimension)
            received = _aware(point.received_at or now)
            is_late = (received - ts).total_seconds() > self.late_seconds
            identity = point_identity(
                series_id=channel.id,
                source_point_id=point.source_point_id,
                ts=ts,
                value=conversion.value,
                source_ref=point.source_ref or channel.source_ref,
                sequence=point.sequence,
            )
        except ValidationFailed as failure:
            return Rejection(index=index, reason=failure.message, details=dict(failure.details))

        row = TimeSeriesPoint(
            id=new_id("tsp"),
            series_id=channel.id,
            ts=ts,
            received_at=received,
            value=conversion.value,
            quality=quality,
            sequence=point.sequence,
            depth_md_si=point.depth_md_si,
            source_point_id=identity.source_point_id,
            source_ref=point.source_ref or channel.source_ref,
            fingerprint=identity.fingerprint,
            dedup_key=identity.dedup_key,
            is_late=is_late,
            is_out_of_order=False,
            src_value=conversion.source_value,
            src_unit=conversion.source_unit,
            attributes={"identified_by": identity.identified_by},
        )
        return _Prepared(row=row, identity=identity, index=index)

    # ------------------------------------------------------------------ writes, statement by statement

    async def _existing_points(self, series_id: str, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """What the channel already holds for a batch's identities — the identities only, in chunks.

        Deliberately not ``SELECT *``: the point's measured value is needed to tell a replay from a
        conflict, and its revision trail to revise it. Reading the thirty other columns of a thousand
        rows to answer that question is how a fast ingestion path becomes a slow one.
        """

        found: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(list(dict.fromkeys(keys)), _IDENTITY_CHUNK):
            rows = (
                await self.session.execute(
                    select(
                        TimeSeriesPoint.dedup_key,
                        TimeSeriesPoint.id,
                        TimeSeriesPoint.value,
                        TimeSeriesPoint.quality,
                        TimeSeriesPoint.src_value,
                        TimeSeriesPoint.src_unit,
                        TimeSeriesPoint.attributes,
                        TimeSeriesPoint.is_late,
                        TimeSeriesPoint.is_out_of_order,
                    ).where(TimeSeriesPoint.series_id == series_id, TimeSeriesPoint.dedup_key.in_(chunk))
                )
            ).all()
            for row in rows:
                found[row.dedup_key] = {
                    "id": row.id,
                    "value": row.value,
                    "quality": row.quality,
                    "src_value": row.src_value,
                    "src_unit": row.src_unit,
                    "attributes": row.attributes,
                    "is_late": row.is_late,
                    "is_out_of_order": row.is_out_of_order,
                }
        return found

    async def _insert_points(self, rows: Sequence[TimeSeriesPoint]) -> set[str]:
        """Store new measurements with a conflict-aware insert; return the identities that landed.

        The unique constraint on ``(series_id, dedup_key)`` means a replayed identity cannot become a
        second row. ``DO NOTHING`` means the loser of a race is told so by the statement rather than by
        an exception, which is what keeps the rest of a thousand-point batch committable.
        """

        if not rows:
            return set()
        dialect = self.session.get_bind().dialect.name
        insert = conflict_aware_insert(dialect)
        statement = insert(TimeSeriesPoint).on_conflict_do_nothing(
            index_elements=["series_id", "dedup_key"]
        ).returning(TimeSeriesPoint.dedup_key)
        landed: set[str] = set()
        for chunk in _chunks(rows, _IDENTITY_CHUNK):
            parameters = [self._point_params(row) for row in chunk]
            result = await self.session.execute(statement, parameters)
            landed.update(key for (key,) in result.fetchall())
        return landed

    @staticmethod
    def _point_params(row: TimeSeriesPoint) -> dict[str, Any]:
        return {
            "id": row.id,
            "series_id": row.series_id,
            "ts": row.ts,
            "received_at": row.received_at,
            "value": row.value,
            "quality": row.quality,
            "sequence": row.sequence,
            "depth_md_si": row.depth_md_si,
            "source_point_id": row.source_point_id,
            "source_ref": row.source_ref,
            "fingerprint": row.fingerprint,
            "dedup_key": row.dedup_key,
            "is_late": row.is_late,
            "is_out_of_order": row.is_out_of_order,
            "src_value": row.src_value,
            "src_unit": row.src_unit,
            "attributes": row.attributes,
        }

    async def _read_point(self, series_id: str, dedup_key: str) -> dict[str, Any] | None:
        """Read back one identity — used when a batch lost a race it could not have seen coming."""

        return (await self._existing_points(series_id, [dedup_key])).get(dedup_key)

    async def _classify_replay(
        self,
        *,
        channel_id: str,
        row: TimeSeriesPoint,
        stored: dict[str, Any],
        on_conflict: str,
        index: int,
    ) -> str:
        """Decide what a point that did not land actually is: a replay, a conflict, or a correction.

        The same measured value and quality is a duplicate — the source resent what the platform already
        has, and nothing changes. A different value is the source contradicting an earlier statement, and
        the policy the caller declared decides: refuse it and say so, or revise the stored point and keep
        the value it replaced in its own revision trail.
        """

        same = stored["value"] == row.value and stored["quality"] == row.quality
        if same:
            return "duplicate"
        if on_conflict == "reject":
            return "conflict"

        trail = list((stored["attributes"] or {}).get("revisions", []))
        trail.append(
            {
                "value": stored["value"],
                "quality": stored["quality"],
                "src_value": stored["src_value"],
                "src_unit": stored["src_unit"],
                "revised_at": utc_now().isoformat(),
            }
        )
        await self.session.execute(
            update(TimeSeriesPoint)
            .where(TimeSeriesPoint.series_id == channel_id, TimeSeriesPoint.dedup_key == row.dedup_key)
            .values(
                value=row.value,
                quality=row.quality,
                src_value=row.src_value,
                src_unit=row.src_unit,
                received_at=row.received_at,
                is_late=row.is_late,
                is_out_of_order=row.is_out_of_order,
                attributes={**(stored["attributes"] or {}), "revisions": trail[-20:]},
            )
        )
        return "revised"

    # ------------------------------------------------------------------ reads

    async def latest(
        self,
        *,
        well_id: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        channel_keys: Iterable[str] | None = None,
        limit: int | None = None,
        now: dt.datetime | None = None,
    ) -> list[LatestReading]:
        """The newest reading per channel, in two statements whatever the number of channels.

        Statement one picks the channels (bounded by ``max_latest_channels``); statement two is a single
        window function that ranks each channel's points and returns rank 1. There is no loop over
        channels anywhere in this path, which is what keeps a forty-channel rig from costing forty
        round trips.
        """

        bound = min(limit or self.max_latest_channels, self.max_latest_channels)
        channels, _total = await self.list_channels(
            limit=bound,
            well_id=well_id,
            wellbore_id=wellbore_id,
            operation_id=operation_id,
        )
        if channel_keys is not None:
            wanted = {key.strip().lower() for key in channel_keys}
            channels = [channel for channel in channels if channel.channel_key in wanted]
        if not channels:
            return []

        ranked = (
            select(
                TimeSeriesPoint.series_id.label("series_id"),
                func.row_number()
                .over(
                    partition_by=TimeSeriesPoint.series_id,
                    order_by=(TimeSeriesPoint.ts.desc(), TimeSeriesPoint.id.desc()),
                )
                .label("rank"),
            )
            .where(TimeSeriesPoint.series_id.in_([channel.id for channel in channels]))
            .subquery()
        )
        newest = (
            await self.session.execute(
                select(TimeSeriesPoint).join(
                    ranked,
                    (ranked.c.series_id == TimeSeriesPoint.series_id) & (ranked.c.rank == 1),
                )
            )
        ).scalars().all()
        by_series = {point.series_id: point for point in newest}

        readings: list[LatestReading] = []
        for channel in channels:
            point = by_series.get(channel.id)
            observed = _aware(point.ts) if point is not None else None
            state, age = self.freshness(observed, now=now)
            flags: list[str] = []
            if point is not None and point.is_late:
                flags.append("late")
            if point is not None and point.is_out_of_order:
                flags.append("out_of_order")
            readings.append(
                LatestReading(
                    channel_id=channel.id,
                    channel_key=channel.channel_key,
                    label=KPI_CHANNEL_LABELS.get(channel.channel_key, channel.name),
                    dimension=channel.dimension,
                    unit=channel.unit,
                    value=point.value if point is not None else None,
                    quality=(point.quality if point is not None else "missing"),
                    quality_flags=tuple(flags),
                    observed_at=observed,
                    received_at=_aware(point.received_at) if point is not None and point.received_at else None,
                    age_seconds=age,
                    freshness=state,
                    source=channel.source,
                    source_ref=(point.source_ref if point is not None and point.source_ref else channel.source_ref),
                    is_late=bool(point is not None and point.is_late),
                )
            )
        return readings

    async def window(
        self,
        series_id: str,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 500,
        cursor: str | None = None,
        order: str = "asc",
    ) -> WindowPage:
        """A bounded page of measurements, ordered deterministically and paged by keyset.

        Ordering is ``(ts, id)`` — the instant, then the identifier for the ties that same-instant points
        create. That total order is what makes a cursor meaningful: a page boundary can never fall
        between two rows that "compare equal" and therefore can never skip or repeat one.
        """

        channel = await self.channel_by_id(series_id)
        bound = min(max(limit, 1), self.max_window_points)
        ascending = order != "desc"

        clauses = [TimeSeriesPoint.series_id == channel.id]
        if since is not None:
            clauses.append(TimeSeriesPoint.ts >= _aware(since))
        if until is not None:
            clauses.append(TimeSeriesPoint.ts <= _aware(until))

        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(
                        select(TimeSeriesPoint.id).where(*clauses).subquery()
                    )
                )
            ).scalar_one()
        )

        if cursor:
            from drillai.telemetry.cursor import decode_cursor

            anchor_ts, anchor_id = decode_cursor(cursor)
            if ascending:
                clauses.append(
                    (TimeSeriesPoint.ts > anchor_ts)
                    | ((TimeSeriesPoint.ts == anchor_ts) & (TimeSeriesPoint.id > anchor_id))
                )
            else:
                clauses.append(
                    (TimeSeriesPoint.ts < anchor_ts)
                    | ((TimeSeriesPoint.ts == anchor_ts) & (TimeSeriesPoint.id < anchor_id))
                )

        ordering = (
            (TimeSeriesPoint.ts.asc(), TimeSeriesPoint.id.asc())
            if ascending
            else (TimeSeriesPoint.ts.desc(), TimeSeriesPoint.id.desc())
        )
        rows = list(
            (
                await self.session.execute(
                    select(TimeSeriesPoint)
                    .where(*clauses)
                    .order_by(*ordering)
                    .limit(bound + 1)  # one extra row: "is there more" is read, not guessed
                )
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > bound
        page = rows[:bound]

        next_cursor = None
        if has_more and page:
            from drillai.telemetry.cursor import encode_cursor

            next_cursor = encode_cursor(_aware(page[-1].ts), page[-1].id)

        return WindowPage(
            channel_id=channel.id,
            points=page,
            count=total,
            returned=len(page),
            next_cursor=next_cursor,
            limit=bound,
            unit=channel.unit,
            truncated=has_more,
        )

    # ------------------------------------------------------------------ rules

    async def rules_for(self, channel: TimeSeries) -> list[AlertRule]:
        """The enabled rules that watch a channel, resolved by key within the channel's own scope.

        Scoping is resolved in Python over a small set rather than in SQL, because a rule may be scoped to
        a well and the channel to a wellbore: the rule applies if its scope is a *prefix* of the
        channel's, which the token makes a string comparison rather than a join.
        """

        candidates = list(
            (
                await self.session.execute(
                    select(AlertRule).where(
                        AlertRule.org_id == self.org_id,
                        AlertRule.channel_key == channel.channel_key,
                        AlertRule.is_enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        token = channel.scope_token or ""
        return [
            rule
            for rule in candidates
            if not rule.scope_token or token.startswith(rule.scope_token)
        ]


@dataclass
class _Prepared:
    """A validated measurement, ready to be matched against what the channel holds."""

    row: TimeSeriesPoint
    identity: Any
    #: Position in the batch the caller sent, so a refusal names the row the caller can look at.
    index: int


def _aware(moment: dt.datetime | None) -> dt.datetime | None:
    """Treat a stored naive instant as UTC.

    Every timestamp the platform writes is UTC-aware; a naive value can only come from a database column
    that was written by something else, and reinterpreting it in the server's local zone would move a
    measurement by hours. Reading it as UTC is the only interpretation that does not invent an offset.
    """

    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
