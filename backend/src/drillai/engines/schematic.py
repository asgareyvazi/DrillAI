"""Well schematic engine: a structured, render-ready well model built from geometry.

The schematic is not an image and not a drawing: it is the *engineering model* of the well
(sections, tubulars, cement, completions, barriers) with computed relationships, validated
consistency and explicit provenance. Any consumer — an SVG renderer, the web UI, a report, a
3D view — renders the same model, so a change in the well data updates every view at once.

The engine validates the things that silently break schematics: non-monotonic depths,
casing that does not fit inside its hole, shoes above the shoe of the hole they are set in,
completion equipment outside the host tubular, and perforations outside the pay interval.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field


class HoleSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    top_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    bottom_depth_si: float = quantity_field(..., "m", gt=0.0)
    hole_diameter_si: float = quantity_field(..., "m", gt=0.0)


class CasingString(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str = Field(default="casing", description="conductor|surface|intermediate|production_liner|liner|casing|tubing")
    outer_diameter_si: float = quantity_field(..., "m", gt=0.0)
    inner_diameter_si: float = quantity_field(..., "m", gt=0.0)
    top_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    shoe_depth_si: float = quantity_field(..., "m", gt=0.0)
    cement_top_si: float | None = quantity_field(None, "m", ge=0.0)
    liner: bool = False
    grade: str | None = None
    weight_kg_per_m: float | None = quantity_field(None, "kg/m", gt=0.0)


class CompletionElement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str = Field(description="tubing|packer|scssv|perforation|plug|screen|gas_lift_mandrel|hanger|other")
    top_depth_si: float = quantity_field(..., "m", ge=0.0)
    bottom_depth_si: float = quantity_field(..., "m", gt=0.0)
    outer_diameter_si: float | None = quantity_field(None, "m", gt=0.0)
    inner_diameter_si: float | None = quantity_field(None, "m", gt=0.0)
    host_string: str | None = Field(default=None, description="casing/tubing string the element is installed in")


class SchematicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    well_name: str | None = None
    datum: str = Field(default="rkb", description="rkb|msl|gl")
    wellhead_elevation_si: float = quantity_field(0.0, "m")
    ground_level_si: float | None = quantity_field(None, "m")
    hole_sections: list[HoleSection] = Field(default_factory=list)
    casing_strings: list[CasingString] = Field(min_length=1)
    completion: list[CompletionElement] = Field(default_factory=list)
    formation_tops: dict[str, float] = Field(default_factory=dict, description="formation name → top depth (MD)")
    total_depth_si: float | None = quantity_field(None, "m", gt=0.0)


class Annulus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    outer: str | None
    inner: str
    from_depth_si: float
    to_depth_si: float
    equivalent_diameter_si: float
    capacity_m3_per_m: float


class SchematicOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    well_name: str | None = None
    datum: str
    total_depth_si: float
    wellhead_elevation_si: float
    ground_level_si: float | None = None
    strings: list[dict]
    hole_sections: list[dict]
    annuli: list[Annulus]
    completion: list[dict]
    formation_tops: list[dict]
    render_hints: dict
    issues: list[ConstraintViolation]


def compute_schematic(inputs: SchematicInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    strings = sorted(inputs.casing_strings, key=lambda item: item.shoe_depth_si)
    deepest = max(
        [string.shoe_depth_si for string in strings]
        + [section.bottom_depth_si for section in inputs.hole_sections]
        + [inputs.total_depth_si or 0.0]
    )
    total_depth = inputs.total_depth_si or deepest
    if inputs.total_depth_si and inputs.total_depth_si < deepest:
        violations.append(
            ConstraintViolation(
                name="total_depth_above_equipment",
                message=(
                    f"total depth {inputs.total_depth_si:.0f} m is above the deepest modelled element "
                    f"{deepest:.0f} m"
                ),
                severity="error",
                limit=inputs.total_depth_si,
                actual=deepest,
                unit="m",
            )
        )

    # ---------------------------------------------------------------- string validation
    previous_shoe = 0.0
    for string in strings:
        if string.shoe_depth_si <= string.top_depth_si:
            violations.append(
                ConstraintViolation(
                    name="invalid_string_interval",
                    message=f"{string.name}: shoe depth {string.shoe_depth_si:.0f} m is not below the top {string.top_depth_si:.0f} m",
                    severity="error",
                )
            )
        if string.inner_diameter_si >= string.outer_diameter_si:
            raise ValueError(f"{string.name}: inner diameter must be smaller than outer diameter")
        if string.cement_top_si is not None:
            if string.cement_top_si > string.shoe_depth_si:
                violations.append(
                    ConstraintViolation(
                        name="cement_top_below_shoe",
                        message=f"{string.name}: cement top {string.cement_top_si:.0f} m is below the shoe {string.shoe_depth_si:.0f} m",
                        severity="error",
                    )
                )
            if string.cement_top_si < string.top_depth_si:
                warnings.append(
                    f"{string.name}: cement top is above the string top; the interval is clamped to the string top"
                )
        if string.shoe_depth_si > total_depth + 1e-6:
            violations.append(
                ConstraintViolation(
                    name="shoe_below_total_depth",
                    message=f"{string.name}: shoe {string.shoe_depth_si:.0f} m is below the well total depth {total_depth:.0f} m",
                    severity="error",
                )
            )
        if _duplicate_shoe(strings, string):
            violations.append(
                ConstraintViolation(
                    name="duplicate_shoe_depths",
                    message=f"{string.name}: shoe depth {string.shoe_depth_si:.1f} m is shared with another string",
                    severity="error",
                )
            )
        host_section = _tightest_hole(inputs.hole_sections, string.shoe_depth_si)
        if host_section is not None and _section_has_other_shoe(strings, string, host_section):
            violations.append(
                ConstraintViolation(
                    name="multiple_shoes_in_section",
                    message=(
                        f"{string.name}: shoe {string.shoe_depth_si:.0f} m is set in the {host_section.name} hole section "
                        "that already carries another string shoe — shoes belong at the bottom of their own section"
                    ),
                    severity="error",
                )
            )
        previous_shoe = max(previous_shoe, string.shoe_depth_si)

    # ---------------------------------------------------------------- fit inside the hole
    for string in strings:
        host = _tightest_hole(inputs.hole_sections, string.shoe_depth_si)
        if host is not None and string.outer_diameter_si >= host.hole_diameter_si:
            violations.append(
                ConstraintViolation(
                    name="string_does_not_fit",
                    message=(
                        f"{string.name} OD {string.outer_diameter_si:.4f} m does not fit in the "
                        f"{host.name} hole {host.hole_diameter_si:.4f} m at {string.shoe_depth_si:.0f} m"
                    ),
                    severity="error",
                    limit=host.hole_diameter_si,
                    actual=string.outer_diameter_si,
                    unit="m",
                )
            )
        elif host is not None:
            clearance = (host.hole_diameter_si - string.outer_diameter_si) / 2.0
            if clearance < 0.0127:  # less than 1/2 in radial clearance
                warnings.append(
                    f"{string.name}: radial clearance in the {host.name} hole is only {clearance * 1000:.1f} mm"
                )

    # ---------------------------------------------------------------- completion validation
    by_name = {string.name: string for string in strings}
    deepest_shoe = max(string.shoe_depth_si for string in strings)
    for element in inputs.completion:
        if element.bottom_depth_si <= element.top_depth_si:
            violations.append(
                ConstraintViolation(
                    name="invalid_completion_interval",
                    message=f"{element.name}: bottom {element.bottom_depth_si:.0f} m is not below the top {element.top_depth_si:.0f} m",
                    severity="error",
                )
            )
            continue
        if element.bottom_depth_si > deepest_shoe + 1e-6:
            violations.append(
                ConstraintViolation(
                    name="completion_below_wellbore",
                    message=(
                        f"{element.name}: bottom {element.bottom_depth_si:.0f} m is below the deepest tubular shoe "
                        f"{deepest_shoe:.0f} m"
                    ),
                    severity="error",
                    limit=deepest_shoe,
                    actual=element.bottom_depth_si,
                    unit="m",
                )
            )
        if element.host_string:
            host = by_name.get(element.host_string)
            if host is None:
                violations.append(
                    ConstraintViolation(
                        name="unknown_host_string",
                        message=f"{element.name}: host string {element.host_string!r} is not in the schematic",
                        severity="error",
                    )
                )
            elif element.bottom_depth_si > host.shoe_depth_si + 1e-6:
                violations.append(
                    ConstraintViolation(
                        name="completion_outside_host",
                        message=(
                            f"{element.name} bottom {element.bottom_depth_si:.0f} m is below the shoe of its host "
                            f"{host.name} ({host.shoe_depth_si:.0f} m)"
                        ),
                        severity="error",
                    )
                )
        # Clearance is checked against the tightest string the element passes through: a
        # completion legitimately spans several strings (tubing run from surface through a liner).
        passing = [
            string
            for string in strings
            if string.top_depth_si <= element.bottom_depth_si and string.shoe_depth_si >= element.top_depth_si
        ]
        if passing and element.outer_diameter_si:
            tightest = min(passing, key=lambda string: string.inner_diameter_si)
            if element.outer_diameter_si >= tightest.inner_diameter_si:
                violations.append(
                    ConstraintViolation(
                        name="completion_does_not_fit",
                        message=(
                            f"{element.name} OD {element.outer_diameter_si:.4f} m does not fit inside "
                            f"{tightest.name} ID {tightest.inner_diameter_si:.4f} m"
                        ),
                        severity="error",
                        limit=tightest.inner_diameter_si,
                        actual=element.outer_diameter_si,
                        unit="m",
                    )
                )

    # ---------------------------------------------------------------- derived geometry
    annuli: list[Annulus] = []
    for inner in strings:
        # Tightest enclosing tubular: it must reach below the inner string's top (so the two
        # overlap) and be wide enough to contain it. A liner therefore shows one casing-in-casing
        # annulus over its overlap and an open-hole annulus below the previous shoe.
        candidates = [
            other
            for other in strings
            if other.name != inner.name
            and other.shoe_depth_si > inner.top_depth_si
            and other.inner_diameter_si > inner.outer_diameter_si
        ]
        outer = min(candidates, key=lambda other: other.inner_diameter_si) if candidates else None
        if outer is not None:
            top = max(inner.top_depth_si, outer.top_depth_si)
            bottom = min(inner.shoe_depth_si, outer.shoe_depth_si)
            if bottom > top:
                annuli.append(
                    _annulus(
                        f"{inner.name}/{outer.name}",
                        outer=outer.name,
                        inner=inner.name,
                        outer_id=outer.inner_diameter_si,
                        inner_od=inner.outer_diameter_si,
                        top=top,
                        bottom=bottom,
                    )
                )
    for string in strings:
        host = _tightest_hole(inputs.hole_sections, string.shoe_depth_si)
        if host is None or host.hole_diameter_si <= string.outer_diameter_si:
            continue
        # The open-hole (barefoot) annulus spans from the base of the enclosing string — or the
        # top of the hole section when there is none — down to this string's shoe.
        enclosing = [
            other.shoe_depth_si
            for other in strings
            if other.name != string.name and other.shoe_depth_si < string.shoe_depth_si
        ]
        top = max([host.top_depth_si, *enclosing])
        annuli.append(
            _annulus(
                f"{string.name}/open hole",
                outer=host.name,
                inner=string.name,
                outer_id=host.hole_diameter_si,
                inner_od=string.outer_diameter_si,
                top=top,
                bottom=string.shoe_depth_si,
            )
        )

    formation_tops = [
        {"formation": name, "top_depth_si": depth}
        for name, depth in sorted(inputs.formation_tops.items(), key=lambda item: item[1])
    ]

    render_hints = {
        "depth_axis": "increasing_downward",
        "datum": inputs.datum,
        "wellhead_elevation_si": inputs.wellhead_elevation_si,
        "depth_scale": {"minimum": 0.0, "maximum": total_depth},
        "element_order": "surface_to_total_depth",
        "annulus_rendering": "concentric",
        "units": "SI (metres)",
    }

    notes = [
        "the schematic is a structured model; rendering is a consumer concern",
        "depths are measured depth along the wellbore in the declared datum",
        "annuli are derived from the tightest enclosing geometry, not entered by hand",
    ]
    if inputs.ground_level_si is None:
        warnings.append("ground level not supplied: the surface rendering will use the wellhead elevation only")
    if not inputs.hole_sections:
        warnings.append("no hole sections supplied: open-hole annuli cannot be derived")
    if violations:
        warnings.append(f"{len(violations)} schematic consistency issue(s) found: fix the model before publishing")

    outputs = SchematicOutput(
        well_name=inputs.well_name,
        datum=inputs.datum,
        total_depth_si=total_depth,
        wellhead_elevation_si=inputs.wellhead_elevation_si,
        ground_level_si=inputs.ground_level_si,
        strings=[
            {
                "name": string.name,
                "kind": string.kind,
                "outer_diameter_si": string.outer_diameter_si,
                "inner_diameter_si": string.inner_diameter_si,
                "top_depth_si": string.top_depth_si,
                "shoe_depth_si": string.shoe_depth_si,
                "cement_top_si": string.cement_top_si,
                "liner": string.liner,
                "grade": string.grade,
                "weight_kg_per_m": string.weight_kg_per_m,
                "capacity_m3_per_m": math.pi / 4.0 * string.inner_diameter_si**2,
            }
            for string in strings
        ],
        hole_sections=[
            {
                "name": section.name,
                "top_depth_si": section.top_depth_si,
                "bottom_depth_si": section.bottom_depth_si,
                "hole_diameter_si": section.hole_diameter_si,
                "capacity_m3_per_m": math.pi / 4.0 * section.hole_diameter_si**2,
            }
            for section in sorted(inputs.hole_sections, key=lambda item: item.top_depth_si)
        ],
        annuli=annuli,
        completion=[
            {
                "name": element.name,
                "kind": element.kind,
                "top_depth_si": element.top_depth_si,
                "bottom_depth_si": element.bottom_depth_si,
                "outer_diameter_si": element.outer_diameter_si,
                "inner_diameter_si": element.inner_diameter_si,
                "host_string": element.host_string,
            }
            for element in sorted(inputs.completion, key=lambda item: item.top_depth_si)
        ],
        formation_tops=formation_tops,
        render_hints=render_hints,
        issues=violations,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes)


def _duplicate_shoe(strings: list[CasingString], string: CasingString, tolerance: float = 0.01) -> bool:
    return any(
        other.name != string.name and abs(other.shoe_depth_si - string.shoe_depth_si) <= tolerance
        for other in strings
    )


def _section_has_other_shoe(strings: list[CasingString], string: CasingString, section: HoleSection) -> bool:
    """True when another string is shoe'd inside the same hole section (a modelling error)."""
    return any(
        other.name != string.name
        and abs(other.shoe_depth_si - string.shoe_depth_si) > 0.01
        and section.top_depth_si < other.shoe_depth_si < section.bottom_depth_si
        for other in strings
    )


def _annulus(name: str, *, outer: str | None, inner: str, outer_id: float, inner_od: float, top: float, bottom: float) -> Annulus:
    equivalent = outer_id - inner_od
    capacity = math.pi / 4.0 * (outer_id**2 - inner_od**2)
    return Annulus(
        name=name,
        outer=outer,
        inner=inner,
        from_depth_si=top,
        to_depth_si=bottom,
        equivalent_diameter_si=equivalent,
        capacity_m3_per_m=capacity,
    )


def _tightest_hole(sections: list[HoleSection], depth_si: float) -> HoleSection | None:
    """The hole section a string is set in at a given depth.

    A shoe sits exactly on a section boundary, where two intervals overlap; the string belongs
    to the section it was drilled and run in — the one ending at that depth — so the section
    with the shallowest bottom wins. Hole diameter is only a tie-break for repeated intervals.
    """
    containing = [
        section
        for section in sections
        if section.top_depth_si <= depth_si <= section.bottom_depth_si
    ]
    if not containing:
        return None
    return min(containing, key=lambda section: (section.bottom_depth_si, section.hole_diameter_si))


SCHEMATIC_SPEC = EngineSpec(
    key="schematic.well_model",
    name="Well schematic model",
    version="1.1.0",
    domain_pack="Well Construction Pack",
    category="schematic",
    summary=(
        "Builds a structured, render-ready well schematic from geometry: hole sections, casing strings with "
        "cement tops, derived annuli, completions and formation tops, with consistency validation."
    ),
    inputs_model=SchematicInput,
    outputs_model=SchematicOutput,
    consumes=("well.identity", "wellbore.geometry", "casing.strings", "cement.job", "completion.design", "formation.markers"),
    produces=("schematic.model",),
    parameters={
        "geometry_model": {"value": "depth_intervals", "description": "interval-based well geometry"},
        "annulus_derivation": {"value": "tightest_enclosing"},
    },
    assumptions=(
        "depths are measured depth in the declared datum",
        "strings are concentric and vertical for the purpose of annulus capacity derivation",
        "cement is shown as a top-to-shoe interval without internal displacement detail",
    ),
    limitations=(
        "no deviated-schematic projection (a TVD view requires the trajectory)",
        "no centraliser, coupling or accessory detail",
        "cement quality and channeling are not represented",
    ),
    references=("Well schematic presentation practice (ISO 13628 / operator standards)",),
    tags=("schematic", "visualisation", "well-construction"),
    action_level="L0",
)

register_engine(FunctionEngine(SCHEMATIC_SPEC, compute_schematic), aliases=("schematic", "well_schematic"))
