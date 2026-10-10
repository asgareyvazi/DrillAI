"""The engineering context model.

Every consumer — agent, skill, workflow node, engine, dashboard, RAG retriever, prompt builder —
reads the *same* :class:`ContextBundle`. That is the single most important structural decision
in the platform: if an agent had its own data access path, prompts would drift from the twin and
two consumers would disagree about the same well.

Three ideas carry the model:

**Scope.** A request names what it is about (well / wellbore / section / operation / depth
window / time window) and its *purpose*. Purpose matters: ``prompt`` budgets tokens and requires
citations, ``engine`` requires canonical SI values, ``dashboard`` wants compact aggregates,
``rag`` wants chunks. The scope travels with the bundle so a downstream consumer can never
silently widen it — this is what stops an 8½" question from matching a 12¼" answer.

**Sections.** The bundle is a list of named sections. Each section knows where its items came
from (``source_kind``/``source_ids``), how reliable they are, and whether it was truncated. A
section with no data is explicitly empty rather than absent, so "nothing on file" is visible.

**Citations.** Items carry ``cites`` (evidence/document ids). Anything rendered for an LLM keeps
its citations attached, which is what makes "the model may not invent engineering values"
mechanically checkable rather than aspirational.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from drillai.core.clock import UTC

__all__ = [
    "ContextBundle",
    "ContextItem",
    "ContextPurpose",
    "ContextRequest",
    "ContextScope",
    "ContextSection",
    "UnitSystemName",
]


class ContextPurpose(enum.StrEnum):
    PROMPT = "prompt"
    AGENT = "agent"
    ENGINE = "engine"
    WORKFLOW = "workflow"
    DASHBOARD = "dashboard"
    RAG = "rag"
    EXPORT = "export"


class UnitSystemName(enum.StrEnum):
    SI = "si"
    FIELD_US = "field_us"
    METRIC_ENGINEERING = "metric_engineering"


class ContextScope(BaseModel):
    """What the context is about. Explicit, and narrow by construction."""

    model_config = ConfigDict(extra="forbid")

    org_id: str
    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    formation_id: str | None = None
    #: Restrict depth-referenced data to this interval (metres, measured depth). This is the
    #: mechanism that keeps an "8½ inch section" question from matching every 8½ string in the
    #: well: the caller passes the section, and providers filter by both section id and depths.
    depth_from_si: float | None = None
    depth_to_si: float | None = None
    #: Restrict time-referenced data (documents, operations, events) to this window.
    period_from: dt.datetime | None = None
    period_to: dt.datetime | None = None

    def depth_overlaps(self, start: float | None, end: float | None) -> bool:
        """True when an interval intersects the requested depth window (None = unbounded)."""
        if self.depth_from_si is None and self.depth_to_si is None:
            return True
        if start is None and end is None:
            return False
        low = start if start is not None else end
        high = end if end is not None else start
        if low is None or high is None:  # pragma: no cover - defensive
            return False
        if self.depth_from_si is not None and high < self.depth_from_si:
            return False
        return not (self.depth_to_si is not None and low > self.depth_to_si)

    def period_overlaps(self, start: dt.datetime | None, end: dt.datetime | None) -> bool:
        if self.period_from is None and self.period_to is None:
            return True
        if start is None and end is None:
            return False
        low = start if start is not None else end
        high = end if end is not None else start
        if low is None or high is None:  # pragma: no cover - defensive
            return False
        if self.period_from is not None and high < self.period_from:
            return False
        return not (self.period_to is not None and low > self.period_to)


class ContextItem(BaseModel):
    """One fact in the context, with provenance."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="e.g. well, section, trajectory_station, extracted_record, engine_result")
    id: str | None = None
    label: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    state_kind: str | None = Field(default=None, description="twin state kind when the item came from the twin")
    source_kind: str = Field(default="database", description="database | twin | document | engine | user | integration")
    source_ids: list[str] = Field(default_factory=list)
    cites: list[str] = Field(default_factory=list, description="evidence/document ids backing the item")
    units: dict[str, str] = Field(default_factory=dict, description="field -> unit symbol for numeric fields")
    confidence: float | None = None
    data_quality: str | None = None
    observed_at: dt.datetime | None = None
    #: When the platform received the observation, when that differs from when it was measured. A value
    #: that arrived an hour after it was taken is a different fact from one that arrived immediately, and
    #: a consumer deciding whether to trust it needs both.
    received_at: dt.datetime | None = None
    #: ``fresh`` / ``stale`` / ``missing`` / ``unknown`` for live-like items — the platform's own
    #: vocabulary, from the same thresholds the API uses, so an AI answer and a screen cannot disagree
    #: about whether a reading is current.
    freshness: str | None = None
    notes: list[str] = Field(default_factory=list)


class ContextSection(BaseModel):
    """A named group of related items."""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    items: list[ContextItem] = Field(default_factory=list)
    empty_reason: str | None = Field(default=None, description="why the section has no items")
    truncated: bool = False
    omitted_items: int = 0
    source_kind: str = "database"
    source_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.items


class ContextBundle(BaseModel):
    """The assembled context handed to a consumer."""

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    scope: ContextScope
    purpose: ContextPurpose
    unit_system: UnitSystemName = UnitSystemName.SI
    locale: str = "en"
    built_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(tz=UTC))
    sections: list[ContextSection] = Field(default_factory=list)
    twin_snapshot_id: str | None = None
    token_estimate: int = 0
    token_budget: int | None = None
    truncated: bool = False
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    omitted_sections: list[str] = Field(default_factory=list)
    permissions_applied: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    redactions: list[str] = Field(default_factory=list)

    def section(self, key: str) -> ContextSection | None:
        return next((section for section in self.sections if section.key == key), None)

    def item_count(self) -> int:
        return sum(len(section.items) for section in self.sections)

    def all_cites(self) -> list[str]:
        seen: dict[str, None] = {}
        for section in self.sections:
            for item in section.items:
                for cite in item.cites:
                    seen.setdefault(cite, None)
        return list(seen)


class ContextRequest(BaseModel):
    """What to build, for whom."""

    model_config = ConfigDict(extra="forbid")

    scope: ContextScope
    purpose: ContextPurpose = ContextPurpose.PROMPT
    sections: list[str] | None = Field(default=None, description="None = the purpose's default set")
    include_evidence: bool = True
    include_documents: bool = True
    include_engine_results: bool = True
    max_items_per_section: int = 50
    token_budget: int | None = Field(default=None, ge=64)
    unit_system: UnitSystemName = UnitSystemName.SI
    locale: str = "en"
    permissions: frozenset[str] = Field(default_factory=frozenset)
    twin_snapshot_id: str | None = None
