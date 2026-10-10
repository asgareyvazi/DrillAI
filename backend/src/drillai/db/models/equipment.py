"""Tools, materials, consumables, equipment, services, inventory and readiness.

A tool or a sack of cement is **not a string in a report**. For a drilling operation to
be executable, the platform must be able to derive *what is needed, how much, where it
is, what it costs, whether it is certified, whether it is compatible and whether it will
arrive in time* — and then record whether that requirement was actually satisfied.

Therefore:

``CatalogItem`` (what the thing *is*) → ``InventoryItem`` / ``EquipmentUnit`` (what we
own, where, in what condition) → ``BomRequirement`` (what this operation needs, with
contingency and derivation basis) → ``ReadinessAssessment`` / ``ReadinessItem``
(whether the operation may proceed).

``Service`` is modelled as a catalogue of *capabilities* supplied by a company, because
service selection (directional, cementing, logging, MWD…) is an engineering decision
with certification and lead-time consequences — not free text.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    Money,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

ITEM_CATEGORIES = (
    "equipment",           # rig/wellsite equipment
    "tool",                # downhole tools, handling tools, fishing tools
    "drillstring",         # drill pipe, HWDP, collars, subs, jars, motors
    "material",            # cement, barite, LCM, casing, tubing, chemicals
    "consumable",          # nozzles, seals, O-rings, inserts, bits
    "spare_part",
    "schematic_component", # wellhead, xmas tree, packers, SCSSV (also schematic entities)
)
SERVICE_CATEGORIES = (
    "directional_drilling",
    "mwd_lwd",
    "mud_logging",
    "wireline_logging",
    "slickline",
    "coiled_tubing",
    "cementing",
    "mud_engineering",
    "well_control",
    "fishing_tools",
    "casing_running",
    "completion",
    "stimulation",
    "perforating",
    "testing",
    "inspections",
    "rental_tools",
    "transport_logistics",
    "hse_services",
    "other",
)
READINESS_DIMENSIONS = (
    "engineering",
    "data",
    "procedure",
    "personnel",
    "material",
    "tool",
    "equipment",
    "service",
    "certification",
    "hse",
    "contingency",
    "logistics",
    "regulatory",
)
READINESS_STATUSES = ("ready", "at_risk", "not_ready", "unknown", "not_applicable")


class ServiceCompany(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "service_companies"
    id_prefix = "sco"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(40), index=True)
    country: Mapped[str | None] = mapped_column(String(80))
    contact: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    qualifications: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    rating: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="approved", nullable=False)
    notes: Mapped[str | None] = mapped_column(TextType)


class Service(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A service capability offered by a company for a scope (project/field/well)."""

    __tablename__ = "services"
    id_prefix = "srv"

    service_company_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_companies.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(60), default="other", nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(TextType)
    capabilities: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    certifications: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    lead_time_days: Mapped[int | None] = mapped_column(IntType)
    mobilization_hours: Mapped[float | None] = mapped_column(Float)
    day_rate_usd: Mapped[Decimal | None] = mapped_column(Money)
    mobilisation_cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    field_id: Mapped[str | None] = mapped_column(String(64), index=True)
    contract_ref: Mapped[str | None] = mapped_column(String(120))
    available_from: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class CatalogItem(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A catalogue item: tool, material, consumable, equipment or drillstring component.

    ``specs`` holds engineering attributes with canonical-SI values (OD, ID, length,
    weight, pressure rating, tensile rating, temperature rating…). ``compatible_with``
    and ``incompatible_with`` encode selection knowledge used by BOM/optimization.
    """

    __tablename__ = "catalog_items"
    id_prefix = "itm"
    __table_args__ = (UniqueConstraint("org_id", "item_code"),)

    item_code: Mapped[str | None] = mapped_column(String(80), index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subcategory: Mapped[str | None] = mapped_column(String(60), index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(TextType)
    manufacturer: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(160))
    part_number: Mapped[str | None] = mapped_column(String(120), index=True)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("service_companies.id", ondelete="SET NULL"))
    unit_of_measure: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    specs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    dimensions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    pressure_rating_si: Mapped[float | None] = mapped_column(Float)
    temperature_rating_max_si: Mapped[float | None] = mapped_column(Float)
    tensile_rating_si: Mapped[float | None] = mapped_column(Float)
    torque_rating_si: Mapped[float | None] = mapped_column(Float)
    compatible_with: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    incompatible_with: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    standards: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    requires_certification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lead_time_days: Mapped[int | None] = mapped_column(IntType)
    unit_cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    is_rental: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_consumable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class InventoryItem(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Physical stock of a catalogue item at a location."""

    __tablename__ = "inventory_items"
    id_prefix = "inv"

    catalog_item_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    location_kind: Mapped[str] = mapped_column(String(32), default="warehouse", nullable=False, index=True)
    location_id: Mapped[str | None] = mapped_column(String(64), index=True)
    location_name: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    reserved_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reorder_point: Mapped[float | None] = mapped_column(Float)
    condition: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    batch_number: Mapped[str | None] = mapped_column(String(80))
    last_movement_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    expiry_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    @property
    def available_quantity(self) -> float:
        return max(0.0, self.quantity - self.reserved_quantity)


class EquipmentUnit(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A tracked serialised unit (tool serial, BHA component, packaged equipment)."""

    __tablename__ = "equipment_units"
    id_prefix = "eqp"

    catalog_item_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    serial_number: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)
    location_name: Mapped[str | None] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(60), default="operator", nullable=False)
    accumulated_hours: Mapped[float | None] = mapped_column(Float)
    hours_limit: Mapped[float | None] = mapped_column(Float)
    accumulated_revolutions: Mapped[float | None] = mapped_column(Float)
    last_inspection_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    next_inspection_due: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    installed_in_wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    condition_notes: Mapped[str | None] = mapped_column(TextType)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Certification(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Certification / inspection record for an item, unit, company or person."""

    __tablename__ = "certifications"
    id_prefix = "crt"

    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cert_number: Mapped[str | None] = mapped_column(String(120))
    issuing_body: Mapped[str | None] = mapped_column(String(200))
    scope: Mapped[str | None] = mapped_column(TextType)
    valid_from: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    valid_to: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    status: Mapped[str] = mapped_column(String(24), default="valid", nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    def is_valid_at(self, when: dt.datetime) -> bool:
        if self.status not in ("valid", "active"):
            return False
        if self.valid_from and when < self.valid_from:
            return False
        return not (self.valid_to and when > self.valid_to)


class BomRequirement(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Engineering BOM line: what an operation needs, and *why*."""

    __tablename__ = "bom_requirements"
    id_prefix = "bom"

    well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    catalog_item_id: Mapped[str | None] = mapped_column(String(64), index=True)
    service_id: Mapped[str | None] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(40), default="material", nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    specification: Mapped[str | None] = mapped_column(TextType)
    quantity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    contingency_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    contingency_basis: Mapped[str | None] = mapped_column(String(80), comment="percent|rule|historical")
    contingency_rationale: Mapped[str | None] = mapped_column(TextType)
    source: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    derivation: Mapped[dict] = mapped_column(
        JsonType, default=dict, nullable=False, comment="how the quantity was derived (offsets, procedure, engine)"
    )
    required_by: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    required_at_location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="required", nullable=False, index=True)
    availability: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    compatibility_notes: Mapped[str | None] = mapped_column(TextType)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    lead_time_days: Mapped[int | None] = mapped_column(IntType)
    is_available: Mapped[bool | None] = mapped_column(Boolean)
    is_certified: Mapped[bool | None] = mapped_column(Boolean)
    risk_if_missing: Mapped[str | None] = mapped_column(TextType)
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class ReadinessAssessment(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Go / no-go assessment for a planned operation, dimension by dimension."""

    __tablename__ = "readiness_assessments"
    id_prefix = "rdy"

    well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    planned_operation: Mapped[str] = mapped_column(String(300), nullable=False)
    planned_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    overall_status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False, index=True)
    gate_decision: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    summary: Mapped[str | None] = mapped_column(TextType)
    blockers: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    risk_notes: Mapped[str | None] = mapped_column(TextType)
    assessed_by: Mapped[str | None] = mapped_column(String(64))
    assessed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    approval_request_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class ReadinessItem(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """One readiness dimension (engineering/data/procedure/material/...)."""

    __tablename__ = "readiness_items"
    id_prefix = "rdi"

    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("readiness_assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str | None] = mapped_column(TextType)
    missing_items: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64))
    due_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rule_ref: Mapped[str | None] = mapped_column(String(120))
    references: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
