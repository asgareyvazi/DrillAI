"""Closest approach between caller-supplied piecewise-linear 3-D paths.

This is deterministic geometry only. It neither derives a survey trajectory nor applies a
positional uncertainty model. Each path must already use the same absolute coordinate frame and
vertical datum; caller declarations are not independently verified by this engine.
"""
from __future__ import annotations

import math
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drillai.core.errors import EngineConvergenceError
from drillai.engines.contract import EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    md_si: float = quantity_field(..., "m", ge=0, le=1e7)
    north_si: float = quantity_field(..., "m", ge=-1e7, le=1e7)
    east_si: float = quantity_field(..., "m", ge=-1e7, le=1e7)
    tvd_si: float = quantity_field(..., "m", ge=-1e7, le=1e7)


class Path(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_reference: str = Field(min_length=1, max_length=200)
    coordinate_reference: str = Field(min_length=1, max_length=200)
    vertical_datum: str = Field(min_length=1, max_length=200)
    source_reference: str = Field(min_length=1, max_length=500)
    depth_positive: Literal["down"]
    points: list[Position] = Field(min_length=2, max_length=150)

    @model_validator(mode="after")
    def valid_path(self) -> Path:
        if any(not item.strip() for item in (self.revision_reference, self.coordinate_reference, self.vertical_datum, self.source_reference)):
            raise ValueError("path provenance and references cannot be blank")
        if any(b.md_si <= a.md_si for a, b in pairwise(self.points)):
            raise ValueError("path measured depths must be strictly increasing")
        return self


class ProximityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: Path
    offset: Path

    @model_validator(mode="after")
    def compatible(self) -> ProximityInput:
        if (self.reference.coordinate_reference, self.reference.vertical_datum) != (self.offset.coordinate_reference, self.offset.vertical_datum):
            raise ValueError("coordinate frame and vertical datum must match; no transformation is performed")
        return self


class ProximityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: Literal["GEOMETRIC_PROXIMITY_ONLY"] = "GEOMETRIC_PROXIMITY_ONLY"
    reference_revision: str
    offset_revision: str
    reference_md_si: float
    offset_md_si: float
    distance_si: float
    horizontal_separation_si: float
    vertical_separation_si: float
    north_difference_si: float
    east_difference_si: float
    tvd_difference_si: float
    reference_segment_index: int
    offset_segment_index: int


def _vector(point: Position) -> tuple[float, float, float]:
    return point.north_si, point.east_si, point.tvd_si


def _sub(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(x - y for x, y in zip(a, b, strict=True))


def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _segment_closest(p: tuple[float, ...], q: tuple[float, ...], u: tuple[float, ...], v: tuple[float, ...]) -> tuple[float, float, float]:
    """Minimize squared Euclidean distance on [0,1]^2 via interior and four edges."""
    w = _sub(p, q)
    a, b, c, d, e = _dot(u, u), _dot(u, v), _dot(v, v), _dot(u, w), _dot(v, w)
    candidates = [(0.0, _clamp(e / c) if c else 0.0), (1.0, _clamp((e + b) / c) if c else 0.0),
                  (_clamp(-d / a) if a else 0.0, 0.0), (_clamp((b - d) / a) if a else 0.0, 1.0)]
    cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    determinant = _dot(cross, cross)
    if 0 < determinant <= 1e-14 * a * c:
        raise EngineConvergenceError("nearly parallel segments cannot be resolved reliably", details={"engine": "trajectory.geometric_proximity"})
    if determinant > 0:
        s, t = (b * e - c * d) / determinant, (a * e - b * d) / determinant
        if 0 <= s <= 1 and 0 <= t <= 1:
            candidates.append((s, t))
    def squared(candidate: tuple[float, float]) -> float:
        s, t = candidate
        return sum((wi + s * ui - t * vi) ** 2 for wi, ui, vi in zip(w, u, v, strict=True))
    s, t = min(candidates, key=squared)
    return squared((s, t)), s, t


def calculate_proximity(inputs: ProximityInput) -> EngineResult:
    best: tuple[float, int, int, float, float] | None = None
    for i, (a, b) in enumerate(pairwise(inputs.reference.points)):
        p, u = _vector(a), _sub(_vector(b), _vector(a))
        for j, (c, d) in enumerate(pairwise(inputs.offset.points)):
            q, v = _vector(c), _sub(_vector(d), _vector(c))
            dist2, s, t = _segment_closest(p, q, u, v)
            candidate = (dist2, i, j, s, t)
            if best is None or candidate < best:
                best = candidate
    assert best is not None
    dist2, i, j, s, t = best
    a, b = inputs.reference.points[i:i + 2]
    c, d = inputs.offset.points[j:j + 2]
    delta = tuple(x + s * (y - x) - z - t * (w - z) for x, y, z, w in zip(_vector(a), _vector(b), _vector(c), _vector(d), strict=True))
    output = ProximityOutput(reference_revision=inputs.reference.revision_reference, offset_revision=inputs.offset.revision_reference,
        reference_md_si=a.md_si + s * (b.md_si - a.md_si), offset_md_si=c.md_si + t * (d.md_si - c.md_si),
        distance_si=math.sqrt(dist2), horizontal_separation_si=math.hypot(delta[0], delta[1]),
        vertical_separation_si=abs(delta[2]), north_difference_si=delta[0], east_difference_si=delta[1],
        tvd_difference_si=delta[2], reference_segment_index=i, offset_segment_index=j)
    return EngineResult(outputs=output, warnings=["GEOMETRIC_PROXIMITY_ONLY: no positional uncertainty, separation factor, or collision risk is computed."],
        assumptions_applied=["linear interpolation between supplied absolute positions", "caller-declared frame and datum are compatible, not independently verified"])


SPEC = EngineSpec(key="trajectory.geometric_proximity", name="Geometric trajectory proximity", version="1.0.0",
    domain_pack="Directional Pack", category="directional", summary="Closest approach of two bounded 3-D polylines; not a collision risk model.",
    inputs_model=ProximityInput, outputs_model=ProximityOutput, consumes=("trajectory.stations",), produces=("deviation.summary",),
    assumptions=("straight line between supplied positions", "common absolute coordinate frame and vertical datum declared by caller"),
    limitations=("GEOMETRIC_PROXIMITY_ONLY", "no uncertainty covariance, separation factor or ISCWSA model", "survey curvature between stations is not modeled", "caller-provided coordinates and references are not independently verified"),
    references=("Euclidean closest points on two line segments (constrained quadratic minimization)",),
    tags=("directional", "geometry", "proximity"), validation_status="formula_derived")
register_engine(FunctionEngine(SPEC, calculate_proximity))
