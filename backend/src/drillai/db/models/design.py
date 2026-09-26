"""Engineering artefacts: plans, designs, programs, procedures, standards, reports.

A single versioned **Artifact** model is used for every engineering document kind
instead of one table per kind. Rationale:

* plan / design / program / procedure / report share the same lifecycle (draft → review
  → approved → superseded) and the same governance requirements;
* a change to a program must be linkable to the engine results, risks and approvals it
  invalidates — that is a property of *artefact versions*, not of the document type;
* the *content* shape differs per kind and is therefore validated by a versioned payload
  schema (``schema_key`` + ``schema_version``) rather than by duplicating columns.

`Risk`, `Lesson`, `Requirement` and `ValidationFinding` attach to wells, sections or
artefact versions and form the engineering-assurance layer (traffic-light validation is
how modern well-planning suites communicate design coherence — see research notes).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

ARTIFACT_KINDS = (
    "plan",              # well plan / basis of design
    "design",            # casing design, completion design, BHA design, mud program
    "program",           # drilling program / completion program
    "procedure",         # operating procedure, work instruction, SOP
    "report",            # DDR, EOWR, service report, recap
    "standard",          # internal standard / guideline
    "risk_assessment",   # HAZID / risk register snapshot
    "readiness",         # readiness / go-no-go assessment for an operation
)
ARTIFACT_STATES = ("draft", "in_review", "approved", "superseded", "archived", "rejected")


class Artifact(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Identity of a versioned engineering artefact."""

    __tablename__ = "artifacts"
    id_prefix = "art"
    __table_args__ = (UniqueConstraint("org_id", "code"),)

    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    well_id: Mapped[str | None] = mapped_column(ForeignKey("wells.id", ondelete="CASCADE"), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(40), default="draft", nullable=False, index=True)
    current_version: Mapped[int] = mapped_column(default=1, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64))
    domain_pack: Mapped[str | None] = mapped_column(
        String(64), comment="domain pack that owns the artefact (e.g. drilling_engineering)"
    )
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    versions: Mapped[list[ArtifactVersion]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan", order_by="ArtifactVersion.version"
    )


class ArtifactVersion(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """One immutable revision of an artefact, with its own approval trail."""

    __tablename__ = "artifact_versions"
    id_prefix = "artv"
    __table_args__ = (UniqueConstraint("artifact_id", "version"),)

    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="draft", nullable=False, index=True)
    schema_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    summary: Mapped[str | None] = mapped_column(TextType)
    document_id: Mapped[str | None] = mapped_column(String(64), comment="source document when imported")
    authored_by: Mapped[str | None] = mapped_column(String(64))
    authored_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    approval_request_id: Mapped[str | None] = mapped_column(String(64))
    change_reason: Mapped[str | None] = mapped_column(TextType)
    supersedes_version_id: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    validation_summary: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    engine_run_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)

    artifact: Mapped[Artifact] = relationship(back_populates="versions")


class Requirement(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A requirement the design must satisfy (company, client, regulatory)."""

    __tablename__ = "requirements"
    id_prefix = "req"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    standard_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="engineering", nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    statement: Mapped[str] = mapped_column(TextType, nullable=False)
    rationale: Mapped[str | None] = mapped_column(TextType)
    criticality: Mapped[str] = mapped_column(String(24), default="high", nullable=False)
    verification_method: Mapped[str | None] = mapped_column(String(120))
    verification_state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Standard(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Internal or external standard / regulation, addressable for retrieval."""

    __tablename__ = "standards"
    id_prefix = "std"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(120), index=True)
    edition: Mapped[str | None] = mapped_column(String(60))
    scope: Mapped[str | None] = mapped_column(TextType)
    applicable_domains: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)


class Risk(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A drilling/engineering risk with depth, cause, mitigation and contingency."""

    __tablename__ = "risks"
    id_prefix = "rsk"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    formation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(60), default="drilling", nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(TextType)
    cause: Mapped[str | None] = mapped_column(TextType)
    consequence: Mapped[str | None] = mapped_column(TextType)
    probability: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    impact: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    severity: Mapped[str] = mapped_column(String(24), default="low", nullable=False, index=True)
    mitigation: Mapped[str | None] = mapped_column(TextType)
    contingency: Mapped[str | None] = mapped_column(TextType)
    depth_from_md_si: Mapped[float | None] = mapped_column(Float)
    depth_to_md_si: Mapped[float | None] = mapped_column(Float)
    owner: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    source_well_id: Mapped[str | None] = mapped_column(String(64), comment="offset well the risk came from")
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    @property
    def risk_score(self) -> int:
        return int(self.probability) * int(self.impact)


class Lesson(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A captured lesson / operational learning, reusable across wells."""

    __tablename__ = "lessons"
    id_prefix = "lsn"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    field_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(60), default="operations", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    recommendation: Mapped[str | None] = mapped_column(TextType)
    applicability: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    recurrence_count: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False)
    adopted_in_artifact_version_id: Mapped[str | None] = mapped_column(String(64))
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class ValidationFinding(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Design-validation result (the "traffic light" of well design coherence).

    Findings are produced by engines or rule sets and always reference the engine run
    that produced them, so a red flag can be traced to inputs and assumptions.
    """

    __tablename__ = "validation_findings"
    id_prefix = "vfd"

    artifact_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    subject_kind: Mapped[str] = mapped_column(String(40), default="well", nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(64), index=True)
    rule_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    rule_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    engine_key: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), default="not_run", nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(24), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    message: Mapped[str | None] = mapped_column(TextType)
    limit_value: Mapped[float | None] = mapped_column(Float)
    actual_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    margin: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    engine_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    computed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    acknowledgement_note: Mapped[str | None] = mapped_column(TextType)
