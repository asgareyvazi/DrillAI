"""Source-backed survey review, trajectory calculation and immutable revision service.

This layer reuses the registered minimum-curvature engine and the existing trajectory/station tables.
Every mutation creates a new numbered trajectory revision; only the previous revision's ``is_current``
selection flag changes. Its engineering inputs and stations are never edited by this service.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.serializers import bounded_json_preview
from drillai.core.audit import record_audit
from drillai.core.clock import ensure_utc
from drillai.core.errors import Conflict, EngineInputInvalid, NotFound, UnitError, ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import (
    Document,
    EvidenceLink,
    ExtractedRecord,
    RawArtifact,
    Trajectory,
    TrajectoryStation,
    Well,
    Wellbore,
)
from drillai.documents.access import (
    document_not_quarantined_condition,
    extracted_record_trust_condition,
    trajectory_source_conditions,
)
from drillai.documents.unit_review import public_extracted_record_payload, unit_inference_review_is_current
from drillai.engines.service import EngineExecution, execute_engine
from drillai.engines.trajectory import SurveyStationInput, TrajectoryEngineInput
from drillai.security.actions import Principal
from drillai.units.registry import convert, get_unit

TrajectoryKind = Literal[
    "plan", "design", "actual", "revised", "proposed", "interpolated", "anti_collision"
]
Datum = Literal["rkb", "msl", "gl", "cf", "derrick_floor", "unknown"]
AzimuthReference = Literal["grid", "true", "magnetic", "unknown"]
TargetAzimuthReference = Literal["grid", "true", "magnetic"]
LengthUnit = Literal["m", "ft"]
AngleUnit = Literal["deg", "rad"]
MAX_SURVEY_STATIONS = 2000
SURVEY_SOURCE_DOC_TYPES = ("ddr", "drilling_document", "program", "survey_report", "other")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SurveyInputStation(_StrictModel):
    """Operator-entered station values with explicit source units and optional prior station link."""

    md_value: float = Field(ge=0, allow_inf_nan=False)
    md_unit: LengthUnit
    inclination_value: float = Field(allow_inf_nan=False)
    inclination_unit: AngleUnit = "deg"
    azimuth_value: float = Field(allow_inf_nan=False)
    azimuth_unit: AngleUnit = "deg"
    azimuth_reference: AzimuthReference
    azimuth_reference_id: str | None = Field(default=None, max_length=120)
    grid_correction_value: float | None = Field(default=None, allow_inf_nan=False)
    grid_correction_unit: AngleUnit = "deg"
    source: str = Field(default="manual", min_length=1, max_length=40)
    toolface_value: float | None = Field(default=None, allow_inf_nan=False)
    toolface_unit: AngleUnit = "deg"
    source_station_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("azimuth_reference_id")
    @classmethod
    def normalize_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("azimuth reference identity must be non-empty printable text")
        return value

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("station source must be non-empty printable text")
        return value


class SurveyCreateRequest(_StrictModel):
    wellbore_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    kind: TrajectoryKind
    expected_current_version: int = Field(ge=0, strict=True)
    base_trajectory_id: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
    datum: Datum
    kb_elevation_value: float | None = Field(default=None, allow_inf_nan=False)
    kb_elevation_unit: LengthUnit = "m"
    target_azimuth_reference: TargetAzimuthReference | None = None
    target_azimuth_reference_id: str | None = Field(default=None, max_length=120)
    vertical_section_azimuth_value: float | None = Field(default=None, allow_inf_nan=False)
    vertical_section_azimuth_unit: AngleUnit = "deg"
    vertical_section_azimuth_reference: AzimuthReference = "unknown"
    vertical_section_azimuth_reference_id: str | None = Field(default=None, max_length=120)
    max_dls_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_inclination_limit: float | None = Field(default=None, gt=0, le=180, allow_inf_nan=False)
    stations: list[SurveyInputStation] = Field(min_length=2, max_length=MAX_SURVEY_STATIONS)

    @field_validator("name", "reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("text must be non-empty and printable")
        return value

    @field_validator("target_azimuth_reference_id", "vertical_section_azimuth_reference_id")
    @classmethod
    def strip_optional_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("reference identity must be non-empty printable text")
        return value

    @model_validator(mode="after")
    def validate_explicit_reference_metadata(self) -> SurveyCreateRequest:
        if self.target_azimuth_reference is None and self.target_azimuth_reference_id is not None:
            raise ValueError("target azimuth reference identity requires an explicit target reference")
        if self.target_azimuth_reference is not None and self.target_azimuth_reference_id is None:
            raise ValueError("an explicit target azimuth reference requires its exact frame identity")
        if self.vertical_section_azimuth_value is None:
            if self.vertical_section_azimuth_reference != "unknown" or self.vertical_section_azimuth_reference_id is not None:
                raise ValueError("vertical-section reference metadata requires a vertical-section azimuth")
        elif self.vertical_section_azimuth_reference == "unknown" or self.vertical_section_azimuth_reference_id is None:
            raise ValueError("vertical-section azimuth requires an explicit reference and frame identity")
        if self.datum != "rkb" and self.kb_elevation_value is not None:
            raise ValueError("KB elevation is only accepted when the declared trajectory datum is rkb")
        if len({station.source_station_id for station in self.stations if station.source_station_id}) != sum(
            station.source_station_id is not None for station in self.stations
        ):
            raise ValueError("a prior station may be referenced only once in a revision")
        return self


class SurveyImportRequest(_StrictModel):
    wellbore_id: str = Field(min_length=1, max_length=64)
    source_document_id: str = Field(min_length=1, max_length=64)
    selected_record_ids: list[str] = Field(min_length=2, max_length=MAX_SURVEY_STATIONS)
    name: str = Field(min_length=1, max_length=160)
    kind: TrajectoryKind
    expected_current_version: int = Field(ge=0, strict=True)
    base_trajectory_id: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
    datum: Datum
    kb_elevation_value: float | None = Field(default=None, allow_inf_nan=False)
    kb_elevation_unit: LengthUnit = "m"
    target_azimuth_reference: TargetAzimuthReference | None = None
    target_azimuth_reference_id: str | None = Field(default=None, max_length=120)
    vertical_section_azimuth_value: float | None = Field(default=None, allow_inf_nan=False)
    vertical_section_azimuth_unit: AngleUnit = "deg"
    vertical_section_azimuth_reference: AzimuthReference = "unknown"
    vertical_section_azimuth_reference_id: str | None = Field(default=None, max_length=120)
    max_dls_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_inclination_limit: float | None = Field(default=None, gt=0, le=180, allow_inf_nan=False)

    @field_validator("name", "reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("text must be non-empty and printable")
        return value

    @field_validator("target_azimuth_reference_id", "vertical_section_azimuth_reference_id")
    @classmethod
    def strip_optional_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("reference identity must be non-empty printable text")
        return value

    @field_validator("selected_record_ids")
    @classmethod
    def selected_ids_must_be_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("selected_record_ids must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_explicit_reference_metadata(self) -> SurveyImportRequest:
        if self.target_azimuth_reference is None and self.target_azimuth_reference_id is not None:
            raise ValueError("target azimuth reference identity requires an explicit target reference")
        if self.target_azimuth_reference is not None and self.target_azimuth_reference_id is None:
            raise ValueError("an explicit target azimuth reference requires its exact frame identity")
        if self.vertical_section_azimuth_value is None:
            if self.vertical_section_azimuth_reference != "unknown" or self.vertical_section_azimuth_reference_id is not None:
                raise ValueError("vertical-section reference metadata requires a vertical-section azimuth")
        elif self.vertical_section_azimuth_reference == "unknown" or self.vertical_section_azimuth_reference_id is None:
            raise ValueError("vertical-section azimuth requires an explicit reference and frame identity")
        if self.datum != "rkb" and self.kb_elevation_value is not None:
            raise ValueError("KB elevation is only accepted when the declared trajectory datum is rkb")
        return self


@dataclass
class _StationBuild:
    engine_input: SurveyStationInput
    source_values: dict[str, Any]
    conversion_metadata: dict[str, Any]
    evidence_ref: str | None = None
    measured_at: dt.datetime | None = None
    measured_at_provenance: str = "unknown"
    quality_flags: list[str] | None = None
    raw_source: dict[str, Any] | None = None
    source_record: ExtractedRecord | None = None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _safe_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": list(error.get("loc", ())), "type": error.get("type"), "msg": error.get("msg")}
        for error in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def _conversion_record(value: float, source_unit: str, canonical_unit: str) -> tuple[float, dict[str, Any]]:
    if not _finite(value):
        raise ValidationFailed("survey source values must be finite numbers")
    try:
        source = get_unit(source_unit)
        target = get_unit(canonical_unit)
        canonical = convert(float(value), source_unit, canonical_unit)
    except UnitError as exc:
        raise ValidationFailed("survey source unit is not supported for this quantity") from exc
    if not math.isfinite(canonical):
        raise ValidationFailed("unit conversion did not produce a finite canonical value")
    return canonical, {
        "source_value": float(value),
        "source_unit": source_unit,
        "canonical_value": canonical,
        "canonical_unit": canonical_unit,
        "factor": source.factor / target.factor,
        "offset": (source.offset - target.offset) / target.factor,
        "conversion_kind": "affine" if source.is_affine or target.is_affine else "linear",
        "registry_source": "drillai.units.registry",
    }


def _source_conversion(value: Any, unit: Any, canonical_unit: str) -> dict[str, Any]:
    if not _finite(value) or not isinstance(unit, str) or not unit.strip():
        return {"status": "not_available", "reason": "source numeric value or unit was not captured"}
    try:
        _canonical, conversion = _conversion_record(float(value), unit.strip(), canonical_unit)
    except ValidationFailed:
        return {
            "status": "unsupported_source_unit",
            "source_value": float(value),
            "source_unit": unit,
            "canonical_unit": canonical_unit,
        }
    return {"status": "computed_from_preserved_source", **conversion}


def _unit_inference_review_is_current(record: ExtractedRecord) -> bool:
    return unit_inference_review_is_current(record.payload, record.review_version)


def _station_from_record(record: ExtractedRecord) -> _StationBuild:
    payload = record.payload if isinstance(record.payload, dict) else {}
    if "unit_inferred" in (record.quality_flags or []) and not _unit_inference_review_is_current(record):
        raise ValidationFailed(
            "a survey record with inferred units requires explicit unit review in its current human-review version",
            details={"record_id": record.id},
        )
    required = ("md_si", "inclination_deg", "azimuth_deg")
    missing = [key for key in required if not _finite(payload.get(key))]
    if missing:
        raise ValidationFailed(
            "a selected, human-validated survey record is missing finite accepted station values",
            details={"record_id": record.id, "missing_fields": missing},
        )
    datum = payload.get("datum", "unknown")
    if not isinstance(datum, str) or datum not in {"rkb", "msl", "gl", "cf", "derrick_floor", "unknown"}:
        raise ValidationFailed(
            "a selected survey record has an invalid or conflicting datum declaration",
            details={"record_id": record.id},
        )
    source = payload.get("source", "unknown")
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 40 or not source.strip().isprintable():
        source = "unknown"
    try:
        station_input = SurveyStationInput(
            md_si=float(payload["md_si"]),
            inclination_deg=float(payload["inclination_deg"]),
            azimuth_deg=float(payload["azimuth_deg"]),
            azimuth_reference=payload.get("azimuth_reference", "unknown"),
            azimuth_reference_id=payload.get("azimuth_reference_id"),
            source=source.strip(),
            measured_at=ensure_utc(record.observed_at) if record.observed_at is not None else None,
            grid_correction_deg=payload.get("grid_correction_deg"),
            quality_flags=list(record.quality_flags or []),
            toolface_deg=payload.get("toolface_deg"),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            details = {"record_id": record.id, "issues": _safe_validation_errors(exc)}
        else:
            details = {"record_id": record.id}
        raise ValidationFailed("a selected survey record has invalid accepted station metadata", details=details) from exc

    units = record.unit_context if isinstance(record.unit_context, dict) else {}
    source_values = {
        key: payload.get(key)
        for key in (
            "md_source_value",
            "md_source_unit",
            "inclination_source_value",
            "inclination_source_unit",
            "azimuth_source_value",
            "azimuth_source_unit",
            "grid_correction_source_value",
            "grid_correction_source_unit",
            "unit_provenance",
        )
        if key in payload
    }
    conversion_metadata = {
        "md": _source_conversion(payload.get("md_source_value"), payload.get("md_source_unit", units.get("md_si")), "m"),
        "inclination": _source_conversion(
            payload.get("inclination_source_value"),
            payload.get("inclination_source_unit", units.get("inclination_deg")),
            "deg",
        ),
        "azimuth": _source_conversion(
            payload.get("azimuth_source_value"),
            payload.get("azimuth_source_unit", units.get("azimuth_deg")),
            "deg",
        ),
        "grid_correction": _source_conversion(
            payload.get("grid_correction_source_value"),
            payload.get("grid_correction_source_unit", units.get("grid_correction_deg")),
            "deg",
        ),
        "accepted_values_are_reviewed_record_payload": True,
    }
    public_record_payload = public_extracted_record_payload(payload)
    raw_source = {
        "source_kind": "extracted_record",
        "source_document_id": record.document_id,
        "source_record_id": record.id,
        "record_payload": public_record_payload,
        "source_values": source_values,
        "unit_context": units,
        "conversion_metadata": conversion_metadata,
        "source_locator": {"page_number": record.page_number, "region_id": record.region_id},
        "extraction": {"method": record.method, "method_version": record.method_version},
        "human_review": {
            "validation_state": record.validation_state,
            "review_version": record.review_version,
            "unit_inference_reviewed": _unit_inference_review_is_current(record),
            "unit_inference_review_version": (
                (payload.get("_unit_inference_review") or {}).get("review_version")
                if isinstance(payload.get("_unit_inference_review"), dict)
                else None
            ),
            "validated_by": record.validated_by,
            "validated_at": record.validated_at.isoformat() if record.validated_at else None,
            "validation_notes_sha256": hashlib.sha256((record.validation_notes or "").encode()).hexdigest(),
        },
    }
    observed_at = ensure_utc(record.observed_at) if record.observed_at is not None else None
    return _StationBuild(
        engine_input=station_input,
        source_values=source_values,
        conversion_metadata=conversion_metadata,
        evidence_ref=record.id,
        measured_at=observed_at,
        measured_at_provenance="source_record" if observed_at is not None else "unknown",
        quality_flags=list(record.quality_flags or []),
        raw_source=raw_source,
        source_record=record,
    )


def _manual_station(station: SurveyInputStation) -> _StationBuild:
    conversions: dict[str, Any] = {}
    try:
        md_si, conversions["md"] = _conversion_record(station.md_value, station.md_unit, "m")
        inclination_deg, conversions["inclination"] = _conversion_record(
            station.inclination_value, station.inclination_unit, "deg"
        )
        azimuth_deg, conversions["azimuth"] = _conversion_record(
            station.azimuth_value, station.azimuth_unit, "deg"
        )
        correction_deg = None
        if station.grid_correction_value is not None:
            correction_deg, conversions["grid_correction"] = _conversion_record(
                station.grid_correction_value, station.grid_correction_unit, "deg"
            )
        toolface_deg = None
        if station.toolface_value is not None:
            toolface_deg, conversions["toolface"] = _conversion_record(
                station.toolface_value, station.toolface_unit, "deg"
            )
    except ValidationFailed:
        raise
    except (UnitError, ValueError, OverflowError) as exc:
        raise ValidationFailed("survey source unit could not be converted to the declared canonical unit") from exc

    try:
        engine_input = SurveyStationInput(
            md_si=md_si,
            inclination_deg=inclination_deg,
            azimuth_deg=azimuth_deg,
            azimuth_reference=station.azimuth_reference,
            azimuth_reference_id=station.azimuth_reference_id,
            source=station.source,
            grid_correction_deg=correction_deg,
            toolface_deg=toolface_deg,
            quality_flags=[],
        )
    except ValidationError as exc:
        raise ValidationFailed(
            "manual survey station failed the trajectory input contract",
            details={"issues": _safe_validation_errors(exc)},
        ) from exc

    values = {
        "md_value": station.md_value,
        "md_unit": station.md_unit,
        "inclination_value": station.inclination_value,
        "inclination_unit": station.inclination_unit,
        "azimuth_value": station.azimuth_value,
        "azimuth_unit": station.azimuth_unit,
        "azimuth_reference": station.azimuth_reference,
        "azimuth_reference_id": station.azimuth_reference_id,
        "grid_correction_value": station.grid_correction_value,
        "grid_correction_unit": station.grid_correction_unit,
        "toolface_value": station.toolface_value,
        "toolface_unit": station.toolface_unit,
        "source": station.source,
    }
    return _StationBuild(
        engine_input=engine_input,
        source_values=values,
        conversion_metadata=conversions,
        quality_flags=[],
        raw_source={
            "source_kind": "manual_entry",
            "source_values": values,
            "conversion_metadata": conversions,
        },
    )


def _trajectory_summary(row: Trajectory) -> dict[str, Any]:
    validation = row.validation if isinstance(row.validation, dict) else {}
    return {
        "id": row.id,
        "wellbore_id": row.wellbore_id,
        "name": row.name,
        "kind": row.kind,
        "version": row.version,
        "is_current": row.is_current,
        "datum": row.datum,
        "datum_provenance": row.datum_provenance,
        "azimuth_reference": row.azimuth_reference,
        "azimuth_reference_id": row.azimuth_reference_id,
        "source_document_id": row.source_document_id,
        "engine_run_id": row.engine_run_id,
        "station_count": row.station_count,
        "total_md_si": row.total_md_si,
        "total_tvd_si": row.total_tvd_si,
        "max_inclination_deg": row.max_inclination_deg,
        "revision_reason": row.revision_reason,
        "calculation_state": validation.get("state", "legacy_unclassified"),
        "engineering_acceptance": validation.get("engineering_acceptance", "not_established"),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _trajectory_detail(row: Trajectory) -> dict[str, Any]:
    validation = row.validation if isinstance(row.validation, dict) else {}
    safe_validation, truncated = bounded_json_preview(
        validation, max_bytes=48 * 1024, max_depth=8, max_items=256, max_string_chars=4096, max_nodes=3000
    )
    return {
        **_trajectory_summary(row),
        "notes": row.notes,
        "target_formation_id": row.target_formation_id,
        "validation": safe_validation,
        "validation_truncated": truncated,
        "unit_contract": validation.get("unit_contract", {}),
        "engine_key": validation.get("engine_key"),
        "engine_version": validation.get("engine_version"),
        "inputs_hash": validation.get("inputs_hash"),
        "outputs_hash": validation.get("outputs_hash"),
        "warnings": list(validation.get("warnings", [])),
        "violations": list(validation.get("violations", [])),
        "assumptions": list(validation.get("assumptions", [])),
        "limitations": list(validation.get("limitations", [])),
    }


def _station_out(row: TrajectoryStation) -> dict[str, Any]:
    raw_source = row.raw_source if isinstance(row.raw_source, dict) else {}
    safe_source, truncated = bounded_json_preview(
        raw_source, max_bytes=16 * 1024, max_depth=8, max_items=256, max_string_chars=4096, max_nodes=2000
    )
    return {
        "id": row.id,
        "trajectory_id": row.trajectory_id,
        "wellbore_id": row.wellbore_id,
        "station_index": row.station_index,
        "md_si": row.md_si,
        "inclination_deg": row.inclination_deg,
        "azimuth_deg": row.azimuth_deg,
        "input_azimuth_deg": row.input_azimuth_deg,
        "input_azimuth_reference": row.input_azimuth_reference,
        "input_azimuth_reference_id": row.input_azimuth_reference_id,
        "tvd_si": row.tvd_si,
        "ns_si": row.ns_si,
        "ew_si": row.ew_si,
        "tvdss_si": row.tvdss_si,
        "dls_deg_per_30m": row.dls_deg_per_30m,
        "build_rate_deg_per_30m": row.build_rate_deg_per_30m,
        "turn_rate_deg_per_30m": row.turn_rate_deg_per_30m,
        "closure_distance_si": row.closure_distance_si,
        "closure_azimuth_deg": row.closure_azimuth_deg,
        "vertical_section_si": row.vertical_section_si,
        "toolface_deg": row.toolface_deg,
        "source": row.source,
        "measured_at": row.measured_at.isoformat() if row.measured_at else None,
        "measured_at_provenance": row.measured_at_provenance,
        "grid_correction_deg": row.grid_correction_deg,
        "quality_flags": list(row.quality_flags or []),
        "method": row.method,
        "engine_version": row.engine_version,
        "evidence_ref": row.evidence_ref,
        "raw_source": safe_source,
        "raw_source_truncated": truncated,
    }


def _contract_unit(unit_contract: dict[str, Any], path: str) -> str:
    fields = unit_contract.get("output_fields") if isinstance(unit_contract, dict) else None
    descriptor = fields.get(path) if isinstance(fields, dict) else None
    if isinstance(descriptor, dict) and descriptor.get("status") == "declared":
        unit = descriptor.get("value_unit")
        if isinstance(unit, str) and unit:
            return unit
    return "unknown"


def _csv_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _source_values(raw_source: dict[str, Any]) -> dict[str, Any]:
    values = raw_source.get("source_values")
    if isinstance(values, dict):
        return values
    payload = raw_source.get("record_payload")
    if isinstance(payload, dict):
        return payload
    return {}


class SurveyService:
    """Tenant-scoped survey API/domain operations."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        org_id: str,
        principal: Principal,
        request_id: str | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.principal = principal
        self.request_id = request_id
        self.actor_id = principal.id

    async def _wellbore(self, well_id: str, wellbore_id: str) -> tuple[Well, Wellbore]:
        result = await self.session.execute(
            select(Well, Wellbore)
            .join(Wellbore, Wellbore.well_id == Well.id)
            .where(
                Well.org_id == self.org_id,
                Well.id == well_id,
                Wellbore.org_id == self.org_id,
                Wellbore.id == wellbore_id,
                Wellbore.well_id == Well.id,
            )
        )
        row = result.one_or_none()
        if row is None:
            raise NotFound("wellbore not found in this well and organization")
        return row[0], row[1]

    async def list_trajectories(self, well_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        well = (
            await self.session.execute(
                select(Well).where(Well.id == well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if well is None:
            raise NotFound("well not found")
        query = (
            select(Trajectory)
            .join(Wellbore, Wellbore.id == Trajectory.wellbore_id)
            .where(
                Trajectory.org_id == self.org_id,
                Wellbore.org_id == self.org_id,
                Wellbore.well_id == well_id,
                *trajectory_source_conditions(Trajectory),
            )
            .order_by(Trajectory.wellbore_id, Trajectory.kind, Trajectory.name, Trajectory.version.desc())
        )
        rows = (await self.session.execute(query.offset(offset).limit(limit))).scalars().all()
        total = int(
            await self.session.scalar(
                select(func.count(Trajectory.id))
                .join(Wellbore, Wellbore.id == Trajectory.wellbore_id)
                .where(
                    Trajectory.org_id == self.org_id,
                    Wellbore.org_id == self.org_id,
                    Wellbore.well_id == well_id,
                    *trajectory_source_conditions(Trajectory),
                )
            )
            or 0
        )
        return {"items": [_trajectory_summary(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    async def _trajectory(self, well_id: str, trajectory_id: str) -> Trajectory:
        result = await self.session.execute(
            select(Trajectory)
            .join(Wellbore, Wellbore.id == Trajectory.wellbore_id)
            .join(Well, Well.id == Wellbore.well_id)
            .where(
                Trajectory.id == trajectory_id,
                Trajectory.org_id == self.org_id,
                Wellbore.org_id == self.org_id,
                Wellbore.well_id == well_id,
                Well.org_id == self.org_id,
                Well.id == well_id,
                *trajectory_source_conditions(Trajectory),
            )
        )
        trajectory = result.scalar_one_or_none()
        if trajectory is None:
            raise NotFound("trajectory not found in this well and organization")
        return trajectory

    async def _history(self, trajectory: Trajectory) -> list[Trajectory]:
        rows = (
            await self.session.execute(
                select(Trajectory)
                .where(
                    Trajectory.org_id == self.org_id,
                    Trajectory.wellbore_id == trajectory.wellbore_id,
                    Trajectory.kind == trajectory.kind,
                    Trajectory.name == trajectory.name,
                )
                .order_by(Trajectory.version.desc())
            )
        ).scalars().all()
        return rows

    async def get_trajectory(self, well_id: str, trajectory_id: str) -> dict[str, Any]:
        trajectory = await self._trajectory(well_id, trajectory_id)
        stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.trajectory_id == trajectory.id,
                    TrajectoryStation.wellbore_id == trajectory.wellbore_id,
                )
                .order_by(TrajectoryStation.station_index)
            )
        ).scalars().all()
        history = await self._history(trajectory)
        return {
            "trajectory": _trajectory_detail(trajectory),
            "stations": [_station_out(row) for row in stations],
            "history": [_trajectory_summary(row) for row in history],
            "station_count_returned": len(stations),
        }

    async def compare(self, well_id: str, current_id: str, baseline_id: str) -> dict[str, Any]:
        current = await self._trajectory(well_id, current_id)
        baseline = await self._trajectory(well_id, baseline_id)
        if (
            current.wellbore_id != baseline.wellbore_id
            or current.kind != baseline.kind
            or current.name != baseline.name
        ):
            raise ValidationFailed("only revisions of the same named trajectory series can be compared")
        if current.id == baseline.id:
            raise ValidationFailed("a trajectory revision cannot be compared with itself")
        current_stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.trajectory_id == current.id,
                    TrajectoryStation.wellbore_id == current.wellbore_id,
                )
                .order_by(TrajectoryStation.station_index)
            )
        ).scalars().all()
        baseline_stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.trajectory_id == baseline.id,
                    TrajectoryStation.wellbore_id == baseline.wellbore_id,
                )
                .order_by(TrajectoryStation.station_index)
            )
        ).scalars().all()
        for label, rows in (("baseline", baseline_stations), ("current", current_stations)):
            measured_depths = [row.md_si for row in rows]
            if any(not _finite(md) or md < 0 for md in measured_depths):
                raise Conflict(f"{label} trajectory has invalid measured depths and requires reconciliation")
            if len(set(measured_depths)) != len(measured_depths):
                raise Conflict(f"{label} trajectory has duplicate measured depths and requires reconciliation")
        old_by_md = {row.md_si: row for row in baseline_stations}
        new_by_md = {row.md_si: row for row in current_stations}
        ordered_depths = sorted(set(old_by_md) | set(new_by_md))
        fields = (
            "inclination_deg",
            "input_azimuth_deg",
            "azimuth_deg",
            "tvd_si",
            "ns_si",
            "ew_si",
            "tvdss_si",
            "dls_deg_per_30m",
            "build_rate_deg_per_30m",
            "turn_rate_deg_per_30m",
        )
        items: list[dict[str, Any]] = []
        for md in ordered_depths:
            old = old_by_md.get(md)
            new = new_by_md.get(md)
            if old is None:
                state = "added"
            elif new is None:
                state = "removed"
            else:
                state = "matched_exact_md"
            old_values = {field: getattr(old, field) if old is not None else None for field in fields}
            new_values = {field: getattr(new, field) if new is not None else None for field in fields}
            deltas = {
                field: (new_values[field] - old_values[field])
                if old is not None and new is not None and _finite(old_values[field]) and _finite(new_values[field])
                else None
                for field in fields
            }
            items.append(
                {
                    "md_si": md,
                    "state": state,
                    "baseline_station_id": old.id if old is not None else None,
                    "current_station_id": new.id if new is not None else None,
                    "baseline": old_values,
                    "current": new_values,
                    "delta": deltas,
                }
            )
        return {
            "comparison_basis": "exact canonical measured-depth equality; no interpolation or nearest-neighbour matching",
            "unit_contract": (current.validation or {}).get("unit_contract", {}),
            "baseline": _trajectory_summary(baseline),
            "current": _trajectory_summary(current),
            "matched_count": sum(item["state"] == "matched_exact_md" for item in items),
            "added_count": sum(item["state"] == "added" for item in items),
            "removed_count": sum(item["state"] == "removed" for item in items),
            "items": items,
        }

    async def create_manual(self, well_id: str, payload: SurveyCreateRequest) -> dict[str, Any]:
        well, wellbore = await self._wellbore(well_id, payload.wellbore_id)
        base, history, next_version = await self._prepare_series(
            well_id=well_id,
            wellbore_id=wellbore.id,
            name=payload.name,
            kind=payload.kind,
            expected_version=payload.expected_current_version,
            base_trajectory_id=payload.base_trajectory_id,
        )
        if base is not None and payload.datum != base.datum:
            raise Conflict(
                "trajectory datum cannot change within a correction revision without an explicit datum conversion",
                details={"base_datum": base.datum, "requested_datum": payload.datum},
            )
        station_builds = [_manual_station(station) for station in payload.stations]
        station_builds = await self._inherit_source_stations(
            station_builds, payload.stations, base, well_id=well_id, wellbore_id=wellbore.id
        )
        engine_input, kb_conversion = self._engine_input(
            station_builds,
            datum=payload.datum,
            kb_elevation_value=payload.kb_elevation_value,
            kb_elevation_unit=payload.kb_elevation_unit,
            target_azimuth_reference=payload.target_azimuth_reference,
            target_azimuth_reference_id=payload.target_azimuth_reference_id,
            vertical_section_azimuth_value=payload.vertical_section_azimuth_value,
            vertical_section_azimuth_unit=payload.vertical_section_azimuth_unit,
            vertical_section_azimuth_reference=payload.vertical_section_azimuth_reference,
            vertical_section_azimuth_reference_id=payload.vertical_section_azimuth_reference_id,
            max_dls_limit=payload.max_dls_limit,
            max_inclination_limit=payload.max_inclination_limit,
        )
        datum_provenance = (
            base.datum_provenance
            if base is not None and base.datum == payload.datum
            else "engine_input"
        )
        source_manifest = {
            "kind": "manual_entry" if base is None else "revision_from_trajectory",
            "base_trajectory_id": base.id if base is not None else None,
            "base_version": base.version if base is not None else None,
            "source_document_id": base.source_document_id if base is not None else None,
            "source_record_ids": [station.evidence_ref for station in station_builds if station.evidence_ref],
            "source_records_preserved": sum(station.evidence_ref is not None for station in station_builds),
            "station_ids_preserved": sum(
                request_station.source_station_id is not None for request_station in payload.stations
            ),
            "kb_elevation_conversion": kb_conversion,
        }
        return await self._persist_revision(
            well_id=well_id,
            well=well,
            wellbore=wellbore,
            name=payload.name,
            kind=payload.kind,
            reason=payload.reason,
            expected_version=payload.expected_current_version,
            base=base,
            history=history,
            version=next_version,
            station_builds=station_builds,
            engine_input=engine_input,
            source_document_id=base.source_document_id if base is not None else None,
            datum_provenance=datum_provenance,
            source_manifest=source_manifest,
            input_source_kind="survey_revision" if base is not None else "manual_survey",
            input_source_id=base.id if base is not None else None,
        )

    async def import_document(self, well_id: str, payload: SurveyImportRequest) -> dict[str, Any]:
        well, wellbore = await self._wellbore(well_id, payload.wellbore_id)
        document = await self._survey_document(well_id, wellbore.id, payload.source_document_id)
        records = await self._selected_records(document, payload.selected_record_ids, well, wellbore)
        station_builds = [_station_from_record(record) for record in records]
        base, history, next_version = await self._prepare_series(
            well_id=well_id,
            wellbore_id=wellbore.id,
            name=payload.name,
            kind=payload.kind,
            expected_version=payload.expected_current_version,
            base_trajectory_id=payload.base_trajectory_id,
        )
        if base is not None and payload.datum != base.datum:
            raise Conflict(
                "trajectory datum cannot change within a correction revision without an explicit datum conversion",
                details={"base_datum": base.datum, "requested_datum": payload.datum},
            )
        engine_input, kb_conversion = self._engine_input(
            station_builds,
            datum=payload.datum,
            kb_elevation_value=payload.kb_elevation_value,
            kb_elevation_unit=payload.kb_elevation_unit,
            target_azimuth_reference=payload.target_azimuth_reference,
            target_azimuth_reference_id=payload.target_azimuth_reference_id,
            vertical_section_azimuth_value=payload.vertical_section_azimuth_value,
            vertical_section_azimuth_unit=payload.vertical_section_azimuth_unit,
            vertical_section_azimuth_reference=payload.vertical_section_azimuth_reference,
            vertical_section_azimuth_reference_id=payload.vertical_section_azimuth_reference_id,
            max_dls_limit=payload.max_dls_limit,
            max_inclination_limit=payload.max_inclination_limit,
        )
        selected = set(payload.selected_record_ids)
        all_doc_records = (
            await self.session.execute(
                select(ExtractedRecord)
                .where(
                    ExtractedRecord.org_id == self.org_id,
                    ExtractedRecord.document_id == document.id,
                    ExtractedRecord.record_type == "survey_station",
                )
                .order_by(ExtractedRecord.page_number, ExtractedRecord.region_id, ExtractedRecord.id)
                .limit(MAX_SURVEY_STATIONS + 1)
            )
        ).scalars().all()
        if len(all_doc_records) > MAX_SURVEY_STATIONS:
            raise ValidationFailed(
                "survey report contains more station records than the import manifest can safely retain; curate the record set first",
                details={"maximum_records": MAX_SURVEY_STATIONS},
            )
        excluded = [
            {
                "record_id": record.id,
                "validation_state": record.validation_state,
                "page_number": record.page_number,
                "reason": "not selected for this trajectory revision",
            }
            for record in all_doc_records
            if record.id not in selected
        ]
        source_manifest = {
            "kind": "survey_document_import",
            "source_document_id": document.id,
            "source_document_title": document.title,
            "selected_record_ids": [record.id for record in records],
            "selected_record_count": len(records),
            "excluded_records": excluded,
            "excluded_record_count": len(excluded),
            "datum_source_record_values": sorted(
                {
                    str((record.payload or {}).get("datum", "unknown"))
                    for record in records
                }
            ),
            "kb_elevation_conversion": kb_conversion,
            "base_trajectory_id": base.id if base is not None else None,
            "base_version": base.version if base is not None else None,
        }
        datum_values = {
            (record.payload or {}).get("datum", "unknown") for record in records
        }
        if len(datum_values) > 1:
            raise Conflict(
                "selected source records declare conflicting vertical datums; reconcile each record before calculation",
                details={"record_count": len(records), "distinct_datum_count": len(datum_values)},
            )
        source_datum = next(iter(datum_values))
        if source_datum != "unknown" and source_datum != payload.datum:
            raise Conflict(
                "selected survey records declare a different datum; no datum conversion is performed",
                details={"source_datum": source_datum, "requested_datum": payload.datum},
            )
        datum_provenance = "source_document" if source_datum == payload.datum else "engine_input"
        return await self._persist_revision(
            well_id=well_id,
            well=well,
            wellbore=wellbore,
            name=payload.name,
            kind=payload.kind,
            reason=payload.reason,
            expected_version=payload.expected_current_version,
            base=base,
            history=history,
            version=next_version,
            station_builds=station_builds,
            engine_input=engine_input,
            source_document_id=document.id,
            datum_provenance=datum_provenance,
            source_manifest=source_manifest,
            input_source_kind="survey_import",
            input_source_id=document.id,
        )

    async def _survey_document(self, well_id: str, wellbore_id: str, document_id: str) -> Document:
        artifact = RawArtifact
        result = await self.session.execute(
            select(Document)
            .outerjoin(artifact, artifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == self.org_id,
                Document.well_id == well_id,
                Document.wellbore_id == wellbore_id,
                Document.doc_type.in_(SURVEY_SOURCE_DOC_TYPES),
                document_not_quarantined_condition(Document, artifact),
            )
        )
        document = result.scalar_one_or_none()
        if document is None:
            raise NotFound("survey report not found in this wellbore or is unavailable")
        return document

    async def _selected_records(
        self,
        document: Document,
        selected_ids: list[str],
        well: Well,
        wellbore: Wellbore,
    ) -> list[ExtractedRecord]:
        artifacts = RawArtifact
        result = await self.session.execute(
            select(ExtractedRecord)
            .join(Document, Document.id == ExtractedRecord.document_id)
            .outerjoin(artifacts, artifacts.id == Document.raw_artifact_id)
            .where(
                ExtractedRecord.org_id == self.org_id,
                ExtractedRecord.document_id == document.id,
                ExtractedRecord.id.in_(selected_ids),
                ExtractedRecord.record_type == "survey_station",
                ExtractedRecord.validation_state == "human_validated",
                ExtractedRecord.validated_by.is_not(None),
                ExtractedRecord.validated_at.is_not(None),
                ExtractedRecord.review_version >= 1,
                extracted_record_trust_condition(ExtractedRecord),
                Document.org_id == self.org_id,
                Document.well_id == well.id,
                Document.wellbore_id == wellbore.id,
                Document.doc_type.in_(SURVEY_SOURCE_DOC_TYPES),
                document_not_quarantined_condition(Document, artifacts),
                or_(ExtractedRecord.project_id.is_(None), ExtractedRecord.project_id == well.project_id),
                or_(ExtractedRecord.well_id.is_(None), ExtractedRecord.well_id == well.id),
                or_(ExtractedRecord.wellbore_id.is_(None), ExtractedRecord.wellbore_id == wellbore.id),
            )
            .order_by(ExtractedRecord.page_number, ExtractedRecord.region_id, ExtractedRecord.id)
        )
        records = result.scalars().all()
        if len(records) != len(selected_ids):
            raise Conflict(
                "one or more selected records are missing, unreviewed, rejected, stale, quarantined, or outside this wellbore",
                details={"selected_count": len(selected_ids), "eligible_count": len(records)},
            )
        if len(records) < 2:
            raise ValidationFailed("at least two human-reviewed survey records are required")
        return records

    async def _prepare_series(
        self,
        *,
        well_id: str,
        wellbore_id: str,
        name: str,
        kind: str,
        expected_version: int,
        base_trajectory_id: str | None,
    ) -> tuple[Trajectory | None, list[Trajectory], int]:
        history = (
            await self.session.execute(
                select(Trajectory)
                .where(
                    Trajectory.org_id == self.org_id,
                    Trajectory.wellbore_id == wellbore_id,
                    Trajectory.kind == kind,
                    Trajectory.name == name,
                )
                .order_by(Trajectory.version.desc())
                .with_for_update()
            )
        ).scalars().all()
        current_rows = [row for row in history if row.is_current]
        if len(current_rows) > 1:
            raise Conflict("trajectory series has multiple current revisions and requires reconciliation")
        current = current_rows[0] if current_rows else None
        if current is None and history:
            raise Conflict("trajectory series has history but no current revision; reconcile before writing")
        actual_version = current.version if current is not None else 0
        if actual_version != expected_version:
            raise Conflict(
                "trajectory series changed since it was opened; reload before creating a revision",
                details={"expected_current_version": expected_version, "current_version": actual_version},
            )
        if current is None:
            if base_trajectory_id is not None:
                raise Conflict("a base trajectory was supplied for a new trajectory series")
        elif base_trajectory_id != current.id:
            raise Conflict(
                "the current trajectory revision must be named as the revision base",
                details={"expected_base_trajectory_id": current.id},
            )
        if current is not None:
            # A current actual trajectory with another name would make downstream legacy readers
            # ambiguous. Do not silently deactivate it or let a second actual series become current.
            if kind == "actual":
                other_current = (
                    await self.session.execute(
                        select(Trajectory.id).where(
                            Trajectory.org_id == self.org_id,
                            Trajectory.wellbore_id == wellbore_id,
                            Trajectory.kind == "actual",
                            Trajectory.is_current.is_(True),
                            Trajectory.id != current.id,
                        )
                    )
                ).scalars().first()
                if other_current is not None:
                    raise Conflict("another current actual trajectory exists for this wellbore; select or reconcile it first")
        elif kind == "actual":
            other_current = (
                await self.session.execute(
                    select(Trajectory.id).where(
                        Trajectory.org_id == self.org_id,
                        Trajectory.wellbore_id == wellbore_id,
                        Trajectory.kind == "actual",
                        Trajectory.is_current.is_(True),
                    )
                )
            ).scalars().first()
            if other_current is not None:
                raise Conflict("another current actual trajectory exists for this wellbore; select or reconcile it first")
        wellbore_result = await self.session.execute(
            select(Wellbore)
            .join(Well, Well.id == Wellbore.well_id)
            .where(
                Wellbore.id == wellbore_id,
                Wellbore.well_id == well_id,
                Wellbore.org_id == self.org_id,
                Well.org_id == self.org_id,
                Well.id == well_id,
            )
        )
        if wellbore_result.scalar_one_or_none() is None:
            raise NotFound("wellbore not found in this well and organization")
        return current, history, (current.version + 1 if current is not None else 1)

    async def _inherit_source_stations(
        self,
        builds: list[_StationBuild],
        request_stations: list[SurveyInputStation],
        base: Trajectory | None,
        *,
        well_id: str,
        wellbore_id: str,
    ) -> list[_StationBuild]:
        requested_ids = [station.source_station_id for station in request_stations if station.source_station_id]
        if requested_ids and base is None:
            raise ValidationFailed("source_station_id is only accepted while revising an existing trajectory")
        if not requested_ids:
            return builds
        base_stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.trajectory_id == base.id,
                    TrajectoryStation.wellbore_id == wellbore_id,
                )
            )
        ).scalars().all()
        by_id = {row.id: row for row in base_stations}
        if any(source_id not in by_id for source_id in requested_ids):
            raise NotFound("a referenced source station is not part of the base trajectory revision")

        inherited_record_ids = {
            by_id[source_id].evidence_ref for source_id in requested_ids if by_id[source_id].evidence_ref
        }
        source_records: dict[str, ExtractedRecord] = {}
        if inherited_record_ids:
            docs = RawArtifact
            valid_rows = (
                await self.session.execute(
                    select(ExtractedRecord)
                    .join(Document, Document.id == ExtractedRecord.document_id)
                    .outerjoin(docs, docs.id == Document.raw_artifact_id)
                    .where(
                        ExtractedRecord.id.in_(inherited_record_ids),
                        ExtractedRecord.org_id == self.org_id,
                        ExtractedRecord.record_type == "survey_station",
                        ExtractedRecord.validation_state == "human_validated",
                        ExtractedRecord.validated_by.is_not(None),
                        ExtractedRecord.validated_at.is_not(None),
                        extracted_record_trust_condition(ExtractedRecord),
                        Document.org_id == self.org_id,
                        Document.well_id == well_id,
                        Document.wellbore_id == wellbore_id,
                        Document.doc_type.in_(SURVEY_SOURCE_DOC_TYPES),
                        document_not_quarantined_condition(Document, docs),
                    )
                )
            ).scalars().all()
            source_records = {row.id: row for row in valid_rows}
            if set(source_records) != inherited_record_ids:
                raise Conflict("a source record for this revision is no longer human-validated and available")

        for build, request_station in zip(builds, request_stations, strict=True):
            if request_station.source_station_id is None:
                continue
            previous = by_id[request_station.source_station_id]
            if previous.evidence_ref:
                record = source_records[previous.evidence_ref]
                old_raw = previous.raw_source if isinstance(previous.raw_source, dict) else {}
                reviewed = old_raw.get("human_review")
                captured_review_version = reviewed.get("review_version") if isinstance(reviewed, dict) else None
                if captured_review_version != record.review_version:
                    raise Conflict(
                        "a source record changed after this trajectory revision; import the newly reviewed values before revising",
                        details={"record_id": record.id, "trajectory_review_version": captured_review_version, "current_review_version": record.review_version},
                    )
                if "unit_inferred" in (record.quality_flags or []) and not _unit_inference_review_is_current(record):
                    raise Conflict(
                        "a source record with inferred units is missing explicit review for its current version",
                        details={"record_id": record.id, "current_review_version": record.review_version},
                    )
                source_doc = record.document_id
                if base.source_document_id and source_doc != base.source_document_id:
                    # Trajectories may span source documents historically. Preserve each station's own
                    # document identity; parent source_document_id only identifies the first source.
                    pass
                build.evidence_ref = previous.evidence_ref
                build.source_record = record
                build.measured_at = previous.measured_at
                build.measured_at_provenance = previous.measured_at_provenance
                build.quality_flags = sorted(set(build.quality_flags or []) | set(previous.quality_flags or []))
                old_raw = previous.raw_source if isinstance(previous.raw_source, dict) else {}
                source_keys = (
                    "source_kind",
                    "source_document_id",
                    "source_record_id",
                    "record_payload",
                    "source_values",
                    "unit_context",
                    "conversion_metadata",
                    "source_locator",
                    "extraction",
                    "human_review",
                )
                build.raw_source = {
                    **{key: old_raw[key] for key in source_keys if key in old_raw},
                    "source_kind": "trajectory_revision",
                    "source_document_id": old_raw.get("source_document_id", source_doc),
                    "source_record_id": previous.evidence_ref,
                    "source_station_id": previous.id,
                    "revision_input": build.raw_source,
                }
            else:
                old_raw = previous.raw_source if isinstance(previous.raw_source, dict) else {}
                build.raw_source = {
                    "source_kind": "trajectory_revision",
                    "source_station_id": previous.id,
                    "previous_manual_source": old_raw.get("source_values"),
                    "revision_input": build.raw_source,
                }
        return builds

    def _engine_input(
        self,
        station_builds: list[_StationBuild],
        *,
        datum: str,
        kb_elevation_value: float | None,
        kb_elevation_unit: str,
        target_azimuth_reference: str | None,
        target_azimuth_reference_id: str | None,
        vertical_section_azimuth_value: float | None,
        vertical_section_azimuth_unit: str,
        vertical_section_azimuth_reference: str,
        vertical_section_azimuth_reference_id: str | None,
        max_dls_limit: float | None,
        max_inclination_limit: float | None,
    ) -> tuple[TrajectoryEngineInput, dict[str, Any] | None]:
        kb_si: float | None = None
        kb_conversion: dict[str, Any] | None = None
        if kb_elevation_value is not None:
            kb_si, kb_conversion = _conversion_record(kb_elevation_value, kb_elevation_unit, "m")
        vertical_section_deg = None
        if vertical_section_azimuth_value is not None:
            vertical_section_deg, _vertical_conversion = _conversion_record(
                vertical_section_azimuth_value, vertical_section_azimuth_unit, "deg"
            )
        payload: dict[str, Any] = {
            "stations": [station.engine_input.model_dump(mode="json") for station in station_builds],
            "datum": datum,
            "kb_elevation_si": kb_si,
            "vertical_section_azimuth_deg": vertical_section_deg,
            "vertical_section_azimuth_reference": vertical_section_azimuth_reference,
            "vertical_section_azimuth_reference_id": vertical_section_azimuth_reference_id,
            "target_azimuth_reference": target_azimuth_reference,
            "target_azimuth_reference_id": target_azimuth_reference_id,
            "max_dls_limit": max_dls_limit,
            "max_inc_limit": max_inclination_limit,
        }
        try:
            engine_input = TrajectoryEngineInput.model_validate(payload)
        except ValidationError as exc:
            raise ValidationFailed(
                "survey input does not satisfy the minimum-curvature engine contract",
                details={"issues": _safe_validation_errors(exc)},
            ) from exc
        return engine_input, kb_conversion

    async def _persist_revision(
        self,
        *,
        well_id: str,
        well: Well,
        wellbore: Wellbore,
        name: str,
        kind: str,
        reason: str,
        expected_version: int,
        base: Trajectory | None,
        history: list[Trajectory],
        version: int,
        station_builds: list[_StationBuild],
        engine_input: TrajectoryEngineInput,
        source_document_id: str | None,
        datum_provenance: str,
        source_manifest: dict[str, Any],
        input_source_kind: str,
        input_source_id: str | None,
    ) -> dict[str, Any]:
        trajectory_id = new_id("trj")
        try:
            async with self.session.begin_nested():
                if base is not None:
                    # The old revision's engineering content and station rows stay intact. Only the
                    # selection pointer changes so readers can identify the new current version.
                    base.is_current = False
                try:
                    execution: EngineExecution = await execute_engine(
                        self.session,
                        engine_key="trajectory.minimum_curvature",
                        inputs=engine_input,
                        org_id=self.org_id,
                        subject_kind="trajectory",
                        subject_id=trajectory_id,
                        project_id=well.project_id,
                        well_id=well_id,
                        wellbore_id=wellbore.id,
                        input_source_kind=input_source_kind,
                        input_source_id=input_source_id,
                        triggered_by="api",
                        triggered_by_id=self.actor_id,
                        persist=True,
                    )
                except EngineInputInvalid as exc:
                    raise ValidationFailed(
                        "minimum-curvature input failed engine validation",
                        details={"issues": exc.details.get("errors", [])},
                    ) from exc
                except ValueError as exc:
                    raise ValidationFailed(
                        "minimum-curvature calculation rejected inconsistent stations or reference frames",
                        details={"reason": str(exc)[:500]},
                    ) from exc

                outputs = execution.outputs
                result_stations = outputs.get("stations")
                if not isinstance(result_stations, list) or len(result_stations) != len(station_builds):
                    raise ValidationFailed("minimum-curvature engine returned an incomplete station result")
                reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
                validation = {
                    "state": "formula_derived",
                    "calculation_status": "succeeded" if execution.is_feasible else "succeeded_with_violations",
                    "engineering_acceptance": "not_established",
                    "datum": outputs.get("datum", engine_input.datum),
                    "datum_provenance": datum_provenance,
                    "azimuth_reference": outputs.get("azimuth_reference"),
                    "azimuth_reference_id": outputs.get("azimuth_reference_id"),
                    "calculation_parameters": {
                        "datum": engine_input.datum,
                        "kb_elevation_si": engine_input.kb_elevation_si,
                        "vertical_section_azimuth_deg": engine_input.vertical_section_azimuth_deg,
                        "vertical_section_azimuth_reference": engine_input.vertical_section_azimuth_reference,
                        "vertical_section_azimuth_reference_id": engine_input.vertical_section_azimuth_reference_id,
                        "target_azimuth_reference": engine_input.target_azimuth_reference,
                        "target_azimuth_reference_id": engine_input.target_azimuth_reference_id,
                        "max_dls_limit": engine_input.max_dls_limit,
                        "max_inclination_limit": engine_input.max_inc_limit,
                    },
                    "engine_key": execution.engine_key,
                    "engine_version": execution.engine_version,
                    "engine_run_id": execution.engine_run_id,
                    "inputs_hash": execution.inputs_hash,
                    "outputs_hash": execution.outputs_hash,
                    "unit_contract": execution.units,
                    "warnings": execution.warnings,
                    "violations": execution.violations,
                    "assumptions": execution.assumptions,
                    "limitations": execution.limitations,
                    "is_feasible": execution.is_feasible,
                    "revision_reason_sha256": reason_hash,
                    "source_manifest": source_manifest,
                    "unit_policy": "canonical values are engine inputs; original source values and explicit conversions are retained per station",
                }
                trajectory = Trajectory(
                    id=trajectory_id,
                    org_id=self.org_id,
                    wellbore_id=wellbore.id,
                    name=name,
                    kind=kind,
                    version=version,
                    datum=outputs.get("datum", engine_input.datum),
                    datum_provenance=datum_provenance,
                    is_current=True,
                    created_by=self.actor_id,
                    engine_run_id=execution.engine_run_id,
                    source_document_id=source_document_id,
                    station_count=len(result_stations),
                    total_md_si=outputs.get("total_md_si"),
                    total_tvd_si=outputs.get("total_tvd_si"),
                    max_inclination_deg=outputs.get("max_inclination_deg"),
                    azimuth_reference=outputs.get("azimuth_reference"),
                    azimuth_reference_id=outputs.get("azimuth_reference_id"),
                    revision_reason=reason,
                    validation=validation,
                    notes=(
                        "Formula-derived trajectory. Engineering and field acceptance have not been established."
                    ),
                )
                self.session.add(trajectory)
                await self.session.flush()

                evidence_links: list[EvidenceLink] = []
                for index, (station_result, build) in enumerate(zip(result_stations, station_builds, strict=True)):
                    input_station = build.engine_input
                    source_raw = dict(build.raw_source or {})
                    source_raw["calculation"] = {
                        "engine_key": execution.engine_key,
                        "engine_version": execution.engine_version,
                        "engine_run_id": execution.engine_run_id,
                        "inputs_hash": execution.inputs_hash,
                        "outputs_hash": execution.outputs_hash,
                        "station_index": index,
                        "accepted_engine_input": input_station.model_dump(mode="json"),
                    }
                    source_raw["revision"] = {
                        "trajectory_id": trajectory.id,
                        "version": version,
                        "reason_sha256": reason_hash,
                        "created_by": self.actor_id,
                    }
                    station = TrajectoryStation(
                        org_id=self.org_id,
                        trajectory_id=trajectory.id,
                        wellbore_id=wellbore.id,
                        station_index=index,
                        md_si=float(station_result["md_si"]),
                        inclination_deg=float(station_result["inclination_deg"]),
                        azimuth_deg=float(station_result["azimuth_deg"]),
                        input_azimuth_deg=float(station_result["input_azimuth_deg"]),
                        input_azimuth_reference=station_result.get("input_azimuth_reference"),
                        input_azimuth_reference_id=station_result.get("input_azimuth_reference_id"),
                        tvd_si=float(station_result["tvd_si"]),
                        ns_si=float(station_result["ns_si"]),
                        ew_si=float(station_result["ew_si"]),
                        tvdss_si=(float(station_result["tvdss_si"]) if station_result.get("tvdss_si") is not None else None),
                        dls_deg_per_30m=float(station_result.get("dls_deg_per_30m", 0.0)),
                        build_rate_deg_per_30m=float(station_result.get("build_rate_deg_per_30m", 0.0)),
                        turn_rate_deg_per_30m=float(station_result.get("turn_rate_deg_per_30m", 0.0)),
                        closure_distance_si=float(station_result.get("closure_distance_si", 0.0)),
                        closure_azimuth_deg=float(station_result.get("closure_azimuth_deg", 0.0)),
                        vertical_section_si=(float(station_result["vertical_section_si"]) if station_result.get("vertical_section_si") is not None else None),
                        toolface_deg=input_station.toolface_deg,
                        source=input_station.source,
                        measured_at=build.measured_at,
                        measured_at_provenance=build.measured_at_provenance,
                        grid_correction_deg=station_result.get("azimuth_correction_applied_deg"),
                        quality_flags=sorted(
                            set(build.quality_flags or []) | set(station_result.get("quality_flags") or [])
                        ),
                        method=outputs.get("method", execution.engine_key),
                        engine_version=execution.engine_version,
                        evidence_ref=build.evidence_ref,
                        raw_source=source_raw,
                    )
                    self.session.add(station)
                    await self.session.flush()
                    if build.source_record is not None:
                        record = build.source_record
                        evidence_links.append(
                            EvidenceLink(
                                org_id=self.org_id,
                                subject_kind="trajectory_station",
                                subject_id=station.id,
                                evidence_kind="extracted_record.survey_station",
                                evidence_id=record.id,
                                document_id=record.document_id,
                                region_id=record.region_id,
                                page_number=record.page_number,
                                well_id=well_id,
                                locator={
                                    "page": record.page_number,
                                    "region_id": record.region_id,
                                    "record_id": record.id,
                                    "review_version": record.review_version,
                                    "method": record.method,
                                    "method_version": record.method_version,
                                },
                                excerpt=json.dumps(
                                    {
                                        key: value
                                        for key, value in (record.payload or {}).items()
                                        if key != "_unit_inference_review"
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )[:2000],
                                confidence=record.confidence,
                                relevance=1.0,
                                weight=1.0,
                                method="survey_workbench:minimum_curvature",
                                quote_verified=False,
                                quote_check="unavailable",
                                note="Source record was human-validated before this trajectory calculation; quote verification is not implied.",
                                created_by=self.actor_id,
                                attributes={"trajectory_id": trajectory.id, "trajectory_version": version},
                            )
                        )
                if evidence_links:
                    self.session.add_all(evidence_links)
                await self.session.flush()
                await record_audit(
                    self.session,
                    org_id=self.org_id,
                    action=(
                        "survey.import"
                        if input_source_kind == "survey_import"
                        else "survey.revise" if base is not None else "survey.create"
                    ),
                    resource_kind="trajectory",
                    resource_id=trajectory.id,
                    principal=self.principal,
                    permission="survey.write",
                    request_id=self.request_id,
                    well_id=well_id,
                    project_id=well.project_id,
                    details={
                        "wellbore_id": wellbore.id,
                        "kind": kind,
                        "input_source_kind": input_source_kind,
                        "version": version,
                        "base_trajectory_id": base.id if base is not None else None,
                        "base_version": base.version if base is not None else None,
                        "station_count": len(result_stations),
                        "source_document_id": source_document_id,
                        "source_record_count": sum(station.evidence_ref is not None for station in station_builds),
                        "engine_run_id": execution.engine_run_id,
                        "engine_version": execution.engine_version,
                        "inputs_hash": execution.inputs_hash,
                        "outputs_hash": execution.outputs_hash,
                        "revision_reason_sha256": reason_hash,
                        "calculation_status": validation["calculation_status"],
                        "engineering_acceptance": "not_established",
                    },
                )
                await self.session.flush()
        except IntegrityError as exc:
            raise Conflict(
                "trajectory series changed concurrently or violates its revision identity; reload and compare before retrying"
            ) from exc
        return await self.get_trajectory(well_id, trajectory_id)

    async def export_csv(self, well_id: str, trajectory_id: str) -> tuple[bytes, dict[str, Any]]:
        trajectory = await self._trajectory(well_id, trajectory_id)
        stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.trajectory_id == trajectory.id,
                    TrajectoryStation.wellbore_id == trajectory.wellbore_id,
                )
                .order_by(TrajectoryStation.station_index)
            )
        ).scalars().all()
        validation = trajectory.validation if isinstance(trajectory.validation, dict) else {}
        unit_contract = validation.get("unit_contract", {})
        fields = [
            ("trajectory_id", "unknown"),
            ("trajectory_version", "1"),
            ("trajectory_kind", "unknown"),
            ("trajectory_name", "unknown"),
            ("datum", "unknown"),
            ("datum_provenance", "unknown"),
            ("azimuth_reference", "unknown"),
            ("azimuth_reference_id", "unknown"),
            ("station_index", _contract_unit(unit_contract, "stations[].index")),
            ("md_si", _contract_unit(unit_contract, "stations[].md_si")),
            ("source_md_value", "source-recorded unit"),
            ("source_md_unit", "source-recorded unit"),
            ("inclination_deg", _contract_unit(unit_contract, "stations[].inclination_deg")),
            ("source_inclination_value", "source-recorded unit"),
            ("source_inclination_unit", "source-recorded unit"),
            ("input_azimuth_deg", _contract_unit(unit_contract, "stations[].input_azimuth_deg")),
            ("source_azimuth_value", "source-recorded unit"),
            ("source_azimuth_unit", "source-recorded unit"),
            ("azimuth_deg", _contract_unit(unit_contract, "stations[].azimuth_deg")),
            ("source_grid_correction_value", "source-recorded unit"),
            ("source_grid_correction_unit", "source-recorded unit"),
            ("grid_correction_applied_deg", _contract_unit(unit_contract, "stations[].azimuth_correction_applied_deg")),
            ("source_toolface_value", "source-recorded unit"),
            ("source_toolface_unit", "source-recorded unit"),
            ("tvd_si", _contract_unit(unit_contract, "stations[].tvd_si")),
            ("ns_si", _contract_unit(unit_contract, "stations[].ns_si")),
            ("ew_si", _contract_unit(unit_contract, "stations[].ew_si")),
            ("tvdss_si", _contract_unit(unit_contract, "stations[].tvdss_si")),
            ("dls_deg_per_30m", _contract_unit(unit_contract, "stations[].dls_deg_per_30m")),
            ("build_rate_deg_per_30m", _contract_unit(unit_contract, "stations[].build_rate_deg_per_30m")),
            ("turn_rate_deg_per_30m", _contract_unit(unit_contract, "stations[].turn_rate_deg_per_30m")),
            ("measured_at", "UTC instant or unknown"),
            ("measured_at_provenance", "provenance state"),
            ("source_document_id", "identifier"),
            ("source_record_id", "identifier"),
            ("source_method", "extractor method or manual"),
            ("source_method_version", "extractor version or unknown"),
            ("quality_flags", "JSON list"),
            ("engine_run_id", "identifier"),
            ("engine_version", "version"),
            ("inputs_hash", "SHA-256"),
            ("outputs_hash", "SHA-256"),
            ("revision_reason", "operator-entered reason"),
            ("revision_reason_sha256", "SHA-256"),
            ("source_conversion_metadata_json", "JSON provenance"),
            ("previous_manual_source_json", "JSON provenance"),
            ("revision_input_json", "JSON provenance"),
            ("engineering_acceptance", "status"),
        ]
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow([f"{name} [{unit}]" for name, unit in fields])
        for station in stations:
            raw = station.raw_source if isinstance(station.raw_source, dict) else {}
            source_values = _source_values(raw)
            revision_input = raw.get("revision_input") if isinstance(raw.get("revision_input"), dict) else {}
            previous_manual_source = raw.get("previous_manual_source")
            if not source_values:
                revision_source_values = revision_input.get("source_values")
                if isinstance(revision_source_values, dict):
                    source_values = revision_source_values
            if not source_values and isinstance(previous_manual_source, dict):
                source_values = previous_manual_source
            record_payload = raw.get("record_payload") if isinstance(raw.get("record_payload"), dict) else {}
            md_source_value = source_values.get(
                "md_source_value", source_values.get("md_value", record_payload.get("md_source_value"))
            )
            md_source_unit = source_values.get(
                "md_source_unit", source_values.get("md_unit", record_payload.get("md_source_unit"))
            )
            inc_source_value = source_values.get(
                "inclination_source_value",
                source_values.get("inclination_value", record_payload.get("inclination_source_value")),
            )
            inc_source_unit = source_values.get(
                "inclination_source_unit", source_values.get("inclination_unit", record_payload.get("inclination_source_unit"))
            )
            azi_source_value = source_values.get(
                "azimuth_source_value", source_values.get("azimuth_value", record_payload.get("azimuth_source_value"))
            )
            azi_source_unit = source_values.get(
                "azimuth_source_unit", source_values.get("azimuth_unit", record_payload.get("azimuth_source_unit"))
            )
            grid_source_value = source_values.get(
                "grid_correction_source_value",
                source_values.get("grid_correction_value", record_payload.get("grid_correction_source_value")),
            )
            grid_source_unit = source_values.get(
                "grid_correction_source_unit",
                source_values.get("grid_correction_unit", record_payload.get("grid_correction_source_unit")),
            )
            toolface_source_value = source_values.get("toolface_value", record_payload.get("toolface_value"))
            toolface_source_unit = source_values.get("toolface_unit", record_payload.get("toolface_unit"))
            extraction = raw.get("extraction") if isinstance(raw.get("extraction"), dict) else {}
            conversion_metadata = raw.get("conversion_metadata")
            if not isinstance(conversion_metadata, dict):
                revision_conversions = revision_input.get("conversion_metadata")
                conversion_metadata = revision_conversions if isinstance(revision_conversions, dict) else {}
            row = {
                "trajectory_id": trajectory.id,
                "trajectory_version": trajectory.version,
                "trajectory_kind": trajectory.kind,
                "trajectory_name": trajectory.name,
                "datum": trajectory.datum,
                "datum_provenance": trajectory.datum_provenance,
                "azimuth_reference": trajectory.azimuth_reference,
                "azimuth_reference_id": trajectory.azimuth_reference_id,
                "station_index": station.station_index,
                "md_si": station.md_si,
                "source_md_value": md_source_value,
                "source_md_unit": md_source_unit,
                "inclination_deg": station.inclination_deg,
                "source_inclination_value": inc_source_value,
                "source_inclination_unit": inc_source_unit,
                "input_azimuth_deg": station.input_azimuth_deg,
                "source_azimuth_value": azi_source_value,
                "source_azimuth_unit": azi_source_unit,
                "azimuth_deg": station.azimuth_deg,
                "source_grid_correction_value": grid_source_value,
                "source_grid_correction_unit": grid_source_unit,
                "grid_correction_applied_deg": station.grid_correction_deg,
                "source_toolface_value": toolface_source_value,
                "source_toolface_unit": toolface_source_unit,
                "tvd_si": station.tvd_si,
                "ns_si": station.ns_si,
                "ew_si": station.ew_si,
                "tvdss_si": station.tvdss_si,
                "dls_deg_per_30m": station.dls_deg_per_30m,
                "build_rate_deg_per_30m": station.build_rate_deg_per_30m,
                "turn_rate_deg_per_30m": station.turn_rate_deg_per_30m,
                "measured_at": station.measured_at.isoformat() if station.measured_at else None,
                "measured_at_provenance": station.measured_at_provenance,
                "source_document_id": raw.get("source_document_id") or trajectory.source_document_id,
                "source_record_id": station.evidence_ref,
                "source_method": extraction.get("method"),
                "source_method_version": extraction.get("method_version"),
                "quality_flags": json.dumps(station.quality_flags or [], ensure_ascii=False),
                "engine_run_id": trajectory.engine_run_id,
                "engine_version": trajectory.validation.get("engine_version") if isinstance(trajectory.validation, dict) else None,
                "inputs_hash": trajectory.validation.get("inputs_hash") if isinstance(trajectory.validation, dict) else None,
                "outputs_hash": trajectory.validation.get("outputs_hash") if isinstance(trajectory.validation, dict) else None,
                "revision_reason": trajectory.revision_reason,
                "revision_reason_sha256": validation.get("revision_reason_sha256"),
                "source_conversion_metadata_json": json.dumps(conversion_metadata, ensure_ascii=False, sort_keys=True),
                "previous_manual_source_json": json.dumps(
                    previous_manual_source if isinstance(previous_manual_source, dict) else {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "revision_input_json": json.dumps(revision_input, ensure_ascii=False, sort_keys=True),
                "engineering_acceptance": "not_established",
            }
            writer.writerow([_csv_cell(row.get(name)) for name, _unit in fields])
        data = buffer.getvalue().encode("utf-8")
        metadata = {
            "trajectory_id": trajectory.id,
            "version": trajectory.version,
            "station_count": len(stations),
            "sha256": hashlib.sha256(data).hexdigest(),
            "engine_run_id": trajectory.engine_run_id,
            "source_document_id": trajectory.source_document_id,
        }
        await record_audit(
            self.session,
            org_id=self.org_id,
            action="survey.export",
            resource_kind="trajectory",
            resource_id=trajectory.id,
            principal=self.principal,
            permission="survey.export",
            request_id=self.request_id,
            well_id=well_id,
            details={
                **metadata,
                "format": "csv",
                "engineering_acceptance": "not_established",
            },
        )
        return data, metadata
