"""The canonical vocabulary of the asset domain — and the only place that checks it.

Before this module the vocabulary existed but governed nothing. ``WELL_TYPES`` and
``WELLBORE_PURPOSES`` were declared in ``db/models/asset.py``, re-exported by the model package and
enforced at no boundary at all: the create endpoints accepted any string, and their *defaults* were
values the vocabulary does not contain (``well_type="development"``, ``purpose="production"``). A
client could therefore write ``well_type="not_a_real_type"`` and read it back as a success — verified
against the running API, not inferred.

Two rules follow, and they are the reason this module exists:

* there is exactly **one** definition of each vocabulary — the tuples stay in ``db/models/asset.py``,
  where the schema documents them, and are imported here rather than restated;
* every value that enters the database passes through :func:`canonical_choice`, so the default a
  caller gets when they omit a field is a member of the vocabulary by construction rather than by
  memory.

``SECTION_STATUSES`` and ``WELLBORE_STATUSES`` are new here, and deliberately small. They are
derived from what the product already relies on — ``drilling`` is read by ``drilling/state.py`` to
decide which section is being drilled, and ``planned`` is what the create path writes — rather than
from a speculative model of the drilling process. Adding a value that nothing reads would be
invented vocabulary, which is the drift this mission is here to remove.
"""

from __future__ import annotations

from collections.abc import Iterable

from drillai.core.errors import ValidationFailed
from drillai.db.models.asset import (
    DATUMS,
    SECTION_KINDS,
    WELL_STATUSES,
    WELL_TYPES,
    WELLBORE_PURPOSES,
)

__all__ = [
    "DATUMS",
    "DEFAULT_ELEVATION_DATUM",
    "DEFAULT_SECTION_KIND",
    "DEFAULT_WELLBORE_PURPOSE",
    "DEFAULT_WELL_TYPE",
    "SECTION_AS_DRILLED_FIELDS",
    "SECTION_KINDS",
    "SECTION_NUMBER_SEMANTICS",
    "SECTION_STATUSES",
    "WELLBORE_PURPOSES",
    "WELLBORE_STATUSES",
    "WELL_STATUSES",
    "WELL_TYPES",
    "canonical_choice",
    "has_choice",
]

# The canonical default for each vocabulary. These are the values a caller receives when the field is
# omitted, and every one of them is a member of its vocabulary — the property the old defaults broke.
DEFAULT_WELL_TYPE = "development_producer"
DEFAULT_WELLBORE_PURPOSE = "original"
DEFAULT_SECTION_KIND = "intermediate"
DEFAULT_ELEVATION_DATUM = "msl"

# A wellbore is a hole; its life cycle is the drilling of that hole, not the well's production life.
WELLBORE_STATUSES: tuple[str, ...] = ("planned", "drilling", "suspended", "abandoned")
# A section follows the same shape, one level down. `is_planned_only` remains the flag that says
# whether the recorded geometry is a plan; `status` says how far the hole has actually got.
SECTION_STATUSES: tuple[str, ...] = ("planned", "drilling", "drilled", "abandoned")


#: What each recorded section number *is*, so a client never has to guess from the column name.
#:
#: * ``plan`` — what the programme intends. A revision here changes a plan, not a fact.
#: * ``actual`` — what the hole actually is, as recorded. It cannot be derived from a plan, and a plan
#:   revision never rewrites it.
#: * ``computed`` — produced by an engine from other recorded inputs (no column is currently in this
#:   class: the section's computed values live in engine runs and reach the section through evidence).
#: * ``interpreted`` — read off a measurement rather than designed, e.g. the mud weight equivalent of a
#:   leak-off test. Presenting an interpretation as a design assumption is how a programme inherits a
#:   number nobody can defend.
#: * ``progress`` — where the hole is *now*. It is neither the plan nor the final as-drilled bottom, and
#:   the platform must never display a planned bottom in its place: an un-drilled section has no
#:   current depth, and saying otherwise is the single most misleading thing a depth readout can do.
SECTION_NUMBER_SEMANTICS: dict[str, str] = {
    "planned_top_md_si": "plan",
    "planned_bottom_md_si": "plan",
    "cement_planned_top_md_si": "plan",
    "mud_weight_min_si": "plan",
    "mud_weight_max_si": "plan",
    "pore_pressure_gradient_si": "plan",
    "fracture_gradient_si": "plan",
    "collapse_gradient_si": "plan",
    "actual_top_md_si": "actual",
    "actual_bottom_md_si": "actual",
    "casing_top_md_si": "actual",
    "casing_shoe_md_si": "actual",
    "cement_top_md_si": "actual",
    "mud_weight_si": "actual",
    "lot_fit_equivalent_mw_si": "interpreted",
    "current_md_si": "progress",
}

#: Depths whose presence means the section is no longer described by its plan alone.
SECTION_AS_DRILLED_FIELDS: tuple[str, ...] = (
    "actual_top_md_si",
    "actual_bottom_md_si",
    "current_md_si",
)


def has_choice(value: str | None, allowed: Iterable[str]) -> bool:
    """Whether ``value`` is a member of the vocabulary. ``None`` is not a member."""
    return value is not None and value in tuple(allowed)


def canonical_choice(
    value: str | None,
    allowed: Iterable[str],
    *,
    field: str,
    default: str | None = None,
) -> str:
    """Return ``value`` if it belongs to the vocabulary, ``default`` if it was omitted, else fail.

    The error names the field, the value that was rejected and the accepted set, because a caller
    that sent ``development`` needs to be told what the platform calls that well, not merely that
    something was wrong. ``allowed`` is sorted in the message so the text is stable across runs.
    """
    allowed_tuple = tuple(allowed)
    if value is None or value == "":
        if default is None:
            raise ValidationFailed(
                f"{field} is required",
                details={"field": field, "allowed": sorted(allowed_tuple)},
            )
        return default
    if value not in allowed_tuple:
        raise ValidationFailed(
            f"{field} {value!r} is not a recognised value",
            details={"field": field, "value": value, "allowed": sorted(allowed_tuple)},
        )
    return value
