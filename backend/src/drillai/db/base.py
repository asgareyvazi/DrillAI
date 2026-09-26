"""Declarative base, mixins and portable column types.

Design decisions (see ``docs/adr/0004-persistence-and-migrations.md``):

* **Portable types only** — ``Uuid``/``JSON``/``DateTime(timezone=True)`` map to native
  PostgreSQL types in production and to SQLite equivalents in tests, so the same
  migration set runs in CI and in production.
* **Enums are stored as text** with the value validated by Pydantic on the way in.
  Extending a drilling vocabulary (a new operation type, a new NPT code) must never
  require a database migration; the DB still enforces not-null and indexes.
* **Every timestamp is timezone-aware UTC** — DDR text, rig clocks and HQ time zones all
  disagree; the platform refuses naïve datetimes (see ``core.clock.ensure_utc``).
* **Money is ``Numeric``** (never float); measurements are ``Float`` stored in canonical
  SI with the originating unit preserved next to it (provenance), see ``drillai.units``.
* **JSON columns always declare a schema key/version** so that structured payloads can be
  migrated and validated instead of drifting into free-form blobs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, MetaData, Numeric, String, Text, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator

from drillai.core.clock import utc_now
from drillai.core.ids import new_id


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps that behave identically on every backend.

    ``DateTime(timezone=True)`` is native on PostgreSQL but SQLite (tests, local dev) stores a
    naive string and hands back a *naive* datetime. Since every instant in the platform is UTC,
    the type re-attaches UTC on load and refuses to write naïve values — a silent local-time
    timestamp is the classic cause of a DDR that lands in the wrong shift.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naïve datetime rejected: all platform timestamps are timezone-aware UTC")
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: dt.datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSON,
        list[str]: JSON,
        list[dict[str, Any]]: JSON,
        dt.datetime: UtcDateTime,
    }

    def to_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        """Plain JSON-serializable representation (Decimal -> float)."""
        excluded = exclude or set()
        result: dict[str, Any] = {}
        for column in self.__table__.columns:
            if column.name in excluded:
                continue
            value = getattr(self, column.name)
            if isinstance(value, Decimal):
                value = float(value)
            result[column.name] = value
        return result

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} {getattr(self, 'id', None)}>"


class IdMixin:
    """ULID primary key with a type prefix (``wel_...``, ``doc_...``).

    Subclasses set ``id_prefix``; the id is generated on flush, so application code never
    constructs identifiers by hand and every row carries the same mint source (strictly
    monotonic within a millisecond, see ``core.ids``).

    ``declared_attr`` is what makes the prefix resolution per-class: a plain column default
    cannot see ``cls``, and generating the id in ``__init__`` would break bulk inserts.
    """

    id_prefix: str = "obj"

    @declared_attr
    def id(cls) -> Mapped[str]:
        prefix = getattr(cls, "id_prefix", "obj")
        return mapped_column(String(64), primary_key=True, default=lambda: new_id(prefix))


class CreatedAtMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utc_now, server_default=func.now(), nullable=False, index=True
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utc_now, onupdate=utc_now, server_default=func.now(), nullable=False
    )


class OrgScopedMixin:
    """Tenant boundary. Every query in the service layer filters by ``org_id``."""

    org_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class SoftDeleteMixin:
    deleted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


def default_id(prefix: str):
    """Column default factory for prefixed ULID ids."""

    def _factory() -> str:
        return new_id(prefix)

    return _factory


def make_id(prefix: str) -> str:
    return new_id(prefix)


# Re-exported names so model modules have one import for column construction.
Money = Numeric(18, 4)
ShortStr = String(64)
NameStr = String(200)
LongStr = String(512)
TextType = Text
JsonType = JSON
BoolType = Boolean
IntType = Integer
FloatType = Float
UuidType = Uuid
DateTimeType = UtcDateTime
