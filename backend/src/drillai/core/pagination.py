"""Pagination, sorting and list query contracts shared by all API resources."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class PageParams(BaseModel):
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)

    @property
    def end(self) -> int:
        return self.offset + self.limit


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class SortSpec(BaseModel):
    field: str
    descending: bool = False


def apply_order(statement, column_map: dict[str, object], sort: SortSpec | None):
    """Apply a validated ORDER BY to a SQLAlchemy select statement.

    Unmapped fields are ignored rather than raising: list endpoints must stay available
    even if a client requests an unknown sort field.
    """
    if sort is None:
        return statement
    column = column_map.get(sort.field)
    if column is None:
        return statement
    return statement.order_by(column.desc() if sort.descending else column.asc())
