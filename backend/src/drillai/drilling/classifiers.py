"""Content-signal classifiers used to turn report text into structured drilling data.

Two hard rules drive this module:

1. **Filename is a hint, never the rule.** Classification reads the text of a row/line and
   matches registered content signals. A file called ``DDR.xlsx`` that contains a casing tally
   classifies as casing, and a file called ``report.csv`` that contains a DDR header classifies
   as a DDR.
2. **A classification carries its own evidence.** Every hit records the signal that matched, so a
   reviewer can see *why* a row was called "stuck pipe" instead of trusting a black box.

The rule tables are data (tuples of :class:`Signal`), per-organization extensible through the
``npt_codes`` table, and deterministic: the same text always classifies the same way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.db.models import NptCode


@dataclass(frozen=True)
class Signal:
    """A content signal: a regex plus the label it assigns when it matches."""

    pattern: str
    label: str
    weight: float = 1.0

    def search(self, text: str) -> re.Match[str] | None:
        return re.search(self.pattern, text, re.IGNORECASE)


@dataclass
class Classification:
    """Result of classifying one piece of text."""

    label: str | None
    matched_pattern: str | None
    matched_text: str | None
    confidence: float
    alternates: list[tuple[str, str]]  # (label, matched text) for the runner-up signals

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "matched_pattern": self.matched_pattern,
            "matched_text": self.matched_text,
            "confidence": round(self.confidence, 3),
            "alternates": [{"label": label, "text": text} for label, text in self.alternates],
        }


#: Operation-kind signals, declared most-specific-first through ``weight``: a *narrow* phrase
#: ("differentially stuck pipe") must beat a *broad* one ("tripping") even when the broad phrase is
#: longer, because the narrow phrase is the actual evidence. Ties fall back to the longer match,
#: then to signal order, so classification is deterministic.
OPERATION_KIND_SIGNALS: tuple[Signal, ...] = (
    # 3.0 — a specific problem/remedial activity: the row is *about* this, not merely near it.
    Signal(r"\bdifferentially\s+stuck\b|\bstuck\s+(pipe|string|bha|tool)\b", "jarring", 3.0),
    Signal(r"\bunit\s+pull", "fishing", 3.0),
    Signal(r"\bfish(ing|ed)?\s+(job|run|operation)?\b|\bovershot\b|\bjunk\s+basket\b", "fishing", 3.0),
    Signal(r"\bmill(ing|ed)?\b", "milling", 3.0),
    Signal(r"\bjar(ring|red)?\b", "jarring", 3.0),
    Signal(r"\blost\s+circulation\b|\blosses\b|\blcm\b|\bseepage\b", "circulating", 3.0),
    Signal(r"\bkick\b|\bwell\s+control\b|\bshut[\s-]?in\b|\bbop\s+closed\b", "well_control", 3.0),
    Signal(r"\bpack[\s-]?off\b|\btight\s+hole\b|\bcaving\b|\bhole\s+collapse\b|\brestrict", "reaming", 3.0),
    # 2.0 — a definite, unambiguous activity.
    Signal(r"\bback[\s-]?ream", "back_reaming", 2.0),
    Signal(r"\bream(ing|ed|er)\b|\bream\s+up\b", "reaming", 2.0),
    Signal(r"\bwiper\s+trip\b", "wiper_trip", 2.0),
    Signal(r"\bcement(ing|ed|ation)?\b|\bslurry\b", "cementing", 2.0),
    Signal(r"\bwoc\b|\bwait(ing)?\s+on\s+cement\b", "curing_woc", 2.0),
    Signal(r"\bcasing\b|\brun\s+.{0,12}\bcasing\b|\bliner\b", "casing", 2.0),
    Signal(r"\bpressure\s+test\b|\btest\s+(the\s+)?(bop|wellhead|liner|packer)\b", "pressure_test", 2.0),
    Signal(r"\bbop\s+test\b|\bblow[\s-]?out\s+preventer\s+test\b", "blow_out_preventer_test", 2.0),
    Signal(r"\bnipple\s+up\b|\bn/d\s+up\b", "nipple_up", 2.0),
    Signal(r"\bspud\b", "spud", 2.0),
    Signal(r"\bcoring\b|\bcore\s+(barrel|point|sample)\b", "coring", 2.0),
    Signal(r"\bwireline\b|\blog(ging|s)?\s+(run|while|tools?)\b", "logging", 2.0),
    Signal(r"\bperforat(e|ing)\b", "perforating", 2.0),
    Signal(r"\bcompletion\b", "completion", 2.0),
    Signal(r"\bdrill(ing|ed)?\s+ahead\b|\brotary\s+drill", "drilling", 2.0),
    Signal(r"\bslid(e|ing)\b", "slide_drilling", 2.0),
    Signal(r"\brotat(ing|e|ion)\b(?=.{0,30}\bdrill)", "rotary_drilling", 2.0),
    Signal(r"\bmake\s+up\s+(a\s+)?(joint|connection)\b|\bmade\s+connection\b", "connection", 2.0),
    Signal(r"\btrip\s+(out|in)\b|\btripping\b|\bpooh\b|\brih\b", "trip_out", 2.0),
    Signal(r"\bcircular?(e|ing|te)\b|\bbottoms[\s-]?up\b", "circulating", 2.0),
    Signal(r"\bcondition(s|ing)?\s+(the\s+)?mud\b|\bmud\s+condition", "mud_conditioning", 2.0),
    Signal(r"\bconnection\b|\bconnect\b", "connection", 1.0),
    Signal(r"\bdrill(ing|ed)?\b", "drilling", 1.0),
    Signal(r"\bcirculat(e|ing)\b", "circulating", 1.0),
)

#: NPT signals. Each entry is ``(signal, code, category, operator_controllable)``:
#:
#: * ``code`` is the industry-style NPT *code* (``STUCK_PIPE``, ``LOST_CIRC``…) and must match a row
#:   in the seeded :data:`STANDARD_NPT_CODES` catalogue, because that is what carries the
#:   subcategory and the controllability judgement. Emitting a category here instead of a code
#:   silently breaks that join and turns a known-controllable loss into an unknown one.
#: * ``category`` is the coarse bucket the Pareto is built on (``stuck_pipe``…).
#: * ``operator_controllable`` mirrors the judgement encoded in the seeded NPT catalogue.
NPT_SIGNALS: tuple[tuple[Signal, str, str, bool], ...] = (
    (Signal(r"\bstuck\s+(pipe|string|bha|tool)\b", "STUCK_PIPE"), "STUCK_PIPE", "stuck_pipe", True),
    (Signal(r"\bdifferentially\s+stuck\b", "STUCK_PIPE_DIFF"), "STUCK_PIPE_DIFF", "stuck_pipe", True),
    (Signal(r"\blost\s+circulation\b|\blosses\b|\bpartial\s+loss\b|\bseepage\b", "LOST_CIRC"), "LOST_CIRC", "lost_circulation", True),
    (Signal(r"\bkick\b|\bwell\s+control\b|\bshut[\s-]?in\b|\bsecondary\s+barrier\b", "WELL_CONTROL"), "WELL_CONTROL", "well_control", True),
    (Signal(r"\btwist[\s-]?off\b|\bwashout\b|\bback[\s-]?off\b", "TOOL_FAILURE"), "TOOL_FAILURE", "downhole_tools", True),
    (Signal(r"\bfish(ing)?\s+(job|run)\b|\bjunk\b|\bfish\b", "FISHING"), "FISHING", "downhole_tools", True),
    (Signal(r"\bmotor\s+fail|\bmwd\s+fail|\blwd\s+fail|\btool\s+fail|\bbha\s+fail", "DOWNHOLE_TOOL_FAIL"), "DOWNHOLE_TOOL_FAIL", "downhole_tools", True),
    (Signal(r"\bbit\s+(was\s+)?pull(ed)?\b|\bdrill\s+bit\s+fail|\bdamaged\s+bit\b|\bbit\s+balling\b", "BIT_FAILURE"), "BIT_FAILURE", "equipment_failure", True),
    (Signal(r"\bpump\s+(fail|repair|down)\b|\btop\s+drive\s+(fail|repair|down)\b|\bdraw[\s-]?works\b", "SURFACE_EQUIP_FAIL"), "SURFACE_EQUIP_FAIL", "surface_equipment", True),
    (Signal(r"\brig\s+repair\b|\bequipment\s+fail|\bmechanical\s+fail", "EQUIP_FAIL"), "EQUIP_FAIL", "equipment_failure", True),
    (Signal(r"\bwait(ing)?\s+on\s+(weather|third\s+party|cement|logistics|orders)\b", "WAITING"), "WAITING", "waiting", False),
    (Signal(r"\bweather\b|\bstorm\b|\bwind\b", "WEATHER"), "WEATHER", "weather", False),
    (Signal(r"\bhole\s+(problem|caving|collapse|pack[\s-]?off)\b|\bcaving\b|\bpack[\s-]?off\b", "HOLE_PROBLEM"), "HOLE_PROBLEM", "hole_problems", True),
    (Signal(r"\btight\s+hole\b|\btight\s+spot\b|\brestrict", "HOLE_PROBLEM_TIGHT"), "HOLE_PROBLEM_TIGHT", "hole_problems", True),
    (Signal(r"\bthird\s+part|\bcontractor\b|\bservice\s+company\b", "THIRD_PARTY"), "THIRD_PARTY", "third_party", False),
    (Signal(r"\bwaiting\s+on\s+(supervisor|engineer|instructions|decision)\b", "WAITING_DECISION"), "WAITING_DECISION", "waiting", False),
)

#: Seeded NPT code catalogue (IADC-style category/subcategory spelling used across the industry).
#: It is *data*, written per organization, because operators extend and re-map codes.
STANDARD_NPT_CODES: tuple[dict[str, object], ...] = (
    {"code": "STUCK_PIPE", "name": "Stuck pipe / differentially stuck", "category": "stuck_pipe", "subcategory": "pipe_stuck", "controllable": True},
    {"code": "STUCK_PIPE_DIFF", "name": "Differentially stuck pipe", "category": "stuck_pipe", "subcategory": "differential", "controllable": True},
    {"code": "LOST_CIRC", "name": "Lost circulation", "category": "lost_circulation", "subcategory": "losses", "controllable": True},
    {"code": "WELL_CONTROL", "name": "Well control / kick", "category": "well_control", "subcategory": "kick", "controllable": True},
    {"code": "TOOL_FAILURE", "name": "Downhole tool failure (twist-off, washout)", "category": "downhole_tools", "subcategory": "failure", "controllable": True},
    {"code": "DOWNHOLE_TOOL_FAIL", "name": "Downhole tool failure (motor/MWD/LWD)", "category": "downhole_tools", "subcategory": "failure", "controllable": True},
    {"code": "FISHING", "name": "Fishing operations", "category": "downhole_tools", "subcategory": "fishing", "controllable": True},
    {"code": "BIT_FAILURE", "name": "Bit failure / balling", "category": "equipment_failure", "subcategory": "bit", "controllable": True},
    {"code": "EQUIP_FAIL", "name": "Rig equipment failure", "category": "equipment_failure", "subcategory": "rig", "controllable": True},
    {"code": "SURFACE_EQUIP_FAIL", "name": "Surface equipment failure", "category": "surface_equipment", "subcategory": "pumps_topdrive", "controllable": True},
    {"code": "HOLE_PROBLEM", "name": "Hole problems (caving, pack-off, collapse)", "category": "hole_problems", "subcategory": "instability", "controllable": True},
    {"code": "HOLE_PROBLEM_TIGHT", "name": "Tight hole / tight spot", "category": "hole_problems", "subcategory": "tight_hole", "controllable": True},
    {"code": "WAITING", "name": "Waiting on weather / third party / logistics", "category": "waiting", "subcategory": "external", "controllable": False},
    {"code": "WAITING_DECISION", "name": "Waiting on decision or instructions", "category": "waiting", "subcategory": "decision", "controllable": False},
    {"code": "WEATHER", "name": "Weather downtime", "category": "weather", "subcategory": "environment", "controllable": False},
    {"code": "THIRD_PARTY", "name": "Third-party / service company time", "category": "third_party", "subcategory": "service", "controllable": False},
)


def classify_operation_kind(text: str) -> Classification:
    """Assign an operation kind from row text using content signals."""
    hits: list[tuple[float, int, int, str, str]] = []
    for order, signal in enumerate(OPERATION_KIND_SIGNALS):
        match = signal.search(text)
        if match:
            hits.append((signal.weight, len(match.group(0)), -order, signal.label, match.group(0)))
    if not hits:
        return Classification(None, None, None, 0.0, [])
    # Specificity, then length of the matched evidence, then declaration order: deterministic.
    hits.sort(key=lambda hit: (-hit[0], -hit[1], -hit[2]))
    weight, _, _, label, matched = hits[0]
    alternates = [(other, text_) for *_, other, text_ in hits[1:4]]
    confidence = min(0.45 + 0.15 * weight + 0.05 * len(matched.split()), 0.93)
    return Classification(label, matched, matched, confidence, alternates)


def classify_npt(text: str) -> tuple[Classification, str | None, bool | None]:
    """Detect NPT from row text.

    Returns the classification (whose ``label`` is the coarse *category*), the catalogue NPT
    *code* it maps to, and whether the loss was operator controllable. Text without an NPT signal
    returns ``label=None`` — absence of a signal is *not* evidence of absence of NPT, and the caller
    must not treat it as such.
    """
    best: tuple[float, int, int, str, str, str, str, bool] | None = None
    alternates: list[tuple[str, str]] = []
    for order, (signal, code, category, controllable) in enumerate(NPT_SIGNALS):
        match = signal.search(text)
        if not match:
            continue
        rank = (signal.weight, len(match.group(0)), -order)
        if best is None or rank > (best[0], best[1], best[2]):
            if best is not None:
                alternates.append((best[3], best[5]))
            best = (
                rank[0],
                rank[1],
                rank[2],
                code,
                category,
                match.group(0),
                signal.pattern,
                controllable,
            )
        else:
            alternates.append((code, match.group(0)))
    if best is None:
        return Classification(None, None, None, 0.0, []), None, None
    weight, _, _, code, category, matched, pattern, controllable = best
    confidence = min(0.5 + 0.15 * weight + 0.05 * len(matched.split()), 0.95)
    return (
        Classification(category, pattern, matched, confidence, alternates[:3]),
        code,
        controllable,
    )


def classify_depth_kind(label: str) -> str | None:
    """Map a depth label to a canonical role used when updating the twin/sections."""
    lowered = label.lower()
    if any(token in lowered for token in ("tvd", "vert")):
        return "tvd"
    if any(token in lowered for token in ("md", "measured", "driller")):
        return "md"
    return None


async def ensure_standard_npt_codes(session: AsyncSession, org_id: str) -> int:
    """Seed the standard NPT code catalogue for an organization (idempotent).

    Returns the number of codes created. Existing codes are never overwritten: an operator's
    local mapping wins over the platform default.
    """
    existing = {
        row.code
        for row in (
            await session.execute(select(NptCode).where(NptCode.org_id == org_id))
        ).scalars().all()
    }
    created = 0
    for spec in STANDARD_NPT_CODES:
        code = str(spec["code"])
        if code in existing:
            continue
        session.add(
            NptCode(
                org_id=org_id,
                code=code,
                name=str(spec["name"]),
                category=str(spec["category"]),
                subcategory=str(spec.get("subcategory")) if spec.get("subcategory") else None,
                description=None,
                is_operator_controllable=bool(spec["controllable"]),
                parent_code=None,
                is_system=True,
            )
        )
        created += 1
    if created:
        await session.flush()
    return created


def iter_signals() -> Iterable[tuple[str, str]]:
    """Debug/introspection helper: every registered content signal with its label."""
    for signal in OPERATION_KIND_SIGNALS:
        yield ("operation_kind", signal.label)
    for npt_signal, code, category, _ in NPT_SIGNALS:
        yield ("npt", code)
        yield ("npt_category", category)
        _ = npt_signal  # kept for symmetry with the operation-kind signals above
