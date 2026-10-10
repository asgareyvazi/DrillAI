"""Twin aspect vocabulary.

The digital well twin is a set of **aspects** (a bounded, named slice of the well's state)
each stored in several **state kinds**:

``planned``      as designed / approved in the program
``actual``       as measured in the field
``current``      the best single value for "what is true right now" (actual if it exists,
                 otherwise planned), with the reconciliation recorded
``historical``   superseded values retained for time travel (never deleted)
``predicted``    model output for a future state (trajectory, pore pressure, cost)
``recommended``  engineering recommendation not yet adopted

Keeping these separate is the difference between a twin and a dashboard: a plan change must not
overwrite a measurement, and an actual must not silently become the plan. The aspect catalogue
below fixes the vocabulary so API, RAG, workflow nodes and the UI all speak the same names.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = ["ASPECTS", "AspectDefinition", "StateKind", "aspect", "aspect_keys", "state_kinds_for"]


class StateKind(enum.StrEnum):
    PLANNED = "planned"
    ACTUAL = "actual"
    CURRENT = "current"
    HISTORICAL = "historical"
    PREDICTED = "predicted"
    RECOMMENDED = "recommended"


#: Which state kinds are meaningful for each aspect — used for validation and for the UI.
_UNIVERSAL = (StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.HISTORICAL)


@dataclass(frozen=True)
class AspectDefinition:
    key: str
    name: str
    description: str
    schema_key: str
    state_kinds: tuple[StateKind, ...] = _UNIVERSAL
    unit_hint: str | None = None
    scope: str = "well"  # well | wellbore | section

    def allows(self, state_kind: StateKind | str) -> bool:
        return StateKind(state_kind) in self.state_kinds


ASPECTS: tuple[AspectDefinition, ...] = (
    AspectDefinition(
        key="identity",
        name="Well identity",
        description="Names, UWI/API, operator, field, surface location and datums.",
        schema_key="twin.identity",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT),
    ),
    AspectDefinition(
        key="geometry",
        name="Wellbore geometry",
        description="Hole sections, casings, wellhead and datum-referenced depths.",
        schema_key="twin.geometry",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.HISTORICAL),
    ),
    AspectDefinition(
        key="trajectory",
        name="Trajectory",
        description="Survey-derived position: MD/TVD/inclination/azimuth/DLS.",
        schema_key="twin.trajectory",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.HISTORICAL, StateKind.PREDICTED),
        unit_hint="m, deg",
    ),
    AspectDefinition(
        key="formation_pressure",
        name="Pore pressure and fracture gradient",
        description="Pore pressure, fracture gradient, collapse gradient by depth, with source.",
        schema_key="twin.pressures",
        unit_hint="kg/m3 equivalent or Pa",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.PREDICTED),
    ),
    AspectDefinition(
        key="mud_program",
        name="Mud program",
        description="Mud type, weight, rheology and treatment by interval.",
        schema_key="twin.mud",
        unit_hint="kg/m3",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.RECOMMENDED),
    ),
    AspectDefinition(
        key="tubulars",
        name="Casing and tubing design",
        description="Strings, grades, weights, connections, design factors and rating checks.",
        schema_key="twin.tubulars",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.RECOMMENDED),
    ),
    AspectDefinition(
        key="cement_job",
        name="Cement job",
        description="Slurry design, top, placement results and verification.",
        schema_key="twin.cement",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT),
    ),
    AspectDefinition(
        key="barriers",
        name="Well integrity barriers",
        description="Verified barrier elements per interval with verification evidence.",
        schema_key="twin.barriers",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.HISTORICAL),
    ),
    AspectDefinition(
        key="completion",
        name="Completion design",
        description="Tubing, packers, perforations, screens and accessories.",
        schema_key="twin.completion",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.RECOMMENDED),
    ),
    AspectDefinition(
        key="bom",
        name="Bill of materials and consumables",
        description="Required items, quantities, units and their readiness state.",
        schema_key="twin.bom",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT),
    ),
    AspectDefinition(
        key="readiness",
        name="Operational readiness",
        description="Availability of equipment, materials, services and people for an operation.",
        schema_key="twin.readiness",
        state_kinds=(StateKind.CURRENT, StateKind.PREDICTED),
    ),
    AspectDefinition(
        key="schedule",
        name="Operations schedule",
        description="Planned versus actual operations, durations and NPT.",
        schema_key="twin.schedule",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.PREDICTED),
    ),
    AspectDefinition(
        key="cost",
        name="Cost and AFE",
        description="Planned, committed and actual cost by phase and cost code.",
        schema_key="twin.cost",
        state_kinds=(StateKind.PLANNED, StateKind.ACTUAL, StateKind.CURRENT, StateKind.PREDICTED),
    ),
    AspectDefinition(
        key="risk",
        name="Risk register",
        description="Identified risks with probability, impact and mitigation per operation.",
        schema_key="twin.risk",
        state_kinds=(StateKind.CURRENT, StateKind.HISTORICAL),
    ),
    AspectDefinition(
        key="hse",
        name="HSE events",
        description="Safety and environmental events relevant to the well.",
        schema_key="twin.hse",
        state_kinds=(StateKind.ACTUAL, StateKind.CURRENT, StateKind.HISTORICAL),
    ),
    AspectDefinition(
        key="offset_analysis",
        name="Offset analysis",
        description="Ranked offset wells, similarity evidence and extracted lessons.",
        schema_key="twin.offsets",
        state_kinds=(StateKind.CURRENT, StateKind.RECOMMENDED),
    ),
)

ASPECTS_BY_KEY: dict[str, AspectDefinition] = {definition.key: definition for definition in ASPECTS}


def aspect(key: str) -> AspectDefinition:
    try:
        return ASPECTS_BY_KEY[key]
    except KeyError as exc:
        raise KeyError(f"unknown twin aspect {key!r}; known: {', '.join(sorted(ASPECTS_BY_KEY))}") from exc


def aspect_keys() -> list[str]:
    return sorted(ASPECTS_BY_KEY)


def state_kinds_for(key: str) -> tuple[StateKind, ...]:
    return aspect(key).state_kinds
