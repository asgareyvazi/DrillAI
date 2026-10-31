"""Shared read predicates for document-backed data.

A quarantined raw artifact may be retained for investigation, but its document and extracted evidence
must not be served, retrieved, or promoted as ordinary engineering input. Missing raw-artifact links
remain eligible for metadata-only documents; a non-null but broken/cross-tenant link fails closed.

Operations and events are durable audit records, but rows derived from a document are not ordinary
engineering input after that document is quarantined, their source record is rejected/superseded, or
the source record no longer satisfies its promotion trust gate. ``operational_source_conditions`` is the
single SQL predicate shared by operational read models; it leaves the historical row in storage while
excluding it from current, trusted reads.
"""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from drillai.db.models import (
    Document,
    DocumentChunk,
    EvidenceLink,
    ExtractedRecord,
    Operation,
    RawArtifact,
    Trajectory,
    TrajectoryStation,
    Well,
    Wellbore,
    WellSection,
)
from drillai.documents.lifecycle import MIN_PROMOTION_CONFIDENCE, PROMOTABLE_VALIDATION_STATES


def document_not_quarantined_condition(document=Document, artifact=RawArtifact):
    """SQL predicate to apply after outer-joining ``artifact`` to ``document``.

    The aliases are parameters so correlated ``EXISTS`` reads can apply the same invariant without
    introducing joins into a caller's paginated statement.
    """
    return or_(
        document.raw_artifact_id.is_(None),
        and_(
            artifact.id == document.raw_artifact_id,
            artifact.org_id == document.org_id,
            artifact.is_quarantined.is_(False),
        ),
    )


def _child_hierarchy_conditions(*, well_id, wellbore_id, section_id, operation_id):
    """Require every non-null child scope tag to be a real same-tenant descendant of the document."""
    well_exists = (
        select(Well.id)
        .where(
            Well.id == well_id,
            Well.org_id == Document.org_id,
            or_(Document.project_id.is_(None), Well.project_id == Document.project_id),
            or_(Document.well_id.is_(None), Well.id == Document.well_id),
        )
        .exists()
    )
    wellbore_exists = (
        select(Wellbore.id)
        .join(Well, Well.id == Wellbore.well_id)
        .where(
            Wellbore.id == wellbore_id,
            Wellbore.org_id == Document.org_id,
            Well.org_id == Document.org_id,
            or_(Document.project_id.is_(None), Well.project_id == Document.project_id),
            or_(Document.well_id.is_(None), Well.id == Document.well_id),
            or_(well_id.is_(None), Wellbore.well_id == well_id),
            or_(Document.wellbore_id.is_(None), Wellbore.id == Document.wellbore_id),
        )
        .exists()
    )
    section_exists = (
        select(WellSection.id)
        .join(Wellbore, Wellbore.id == WellSection.wellbore_id)
        .join(Well, Well.id == Wellbore.well_id)
        .where(
            WellSection.id == section_id,
            WellSection.org_id == Document.org_id,
            Wellbore.org_id == Document.org_id,
            Well.org_id == Document.org_id,
            or_(Document.project_id.is_(None), Well.project_id == Document.project_id),
            or_(Document.well_id.is_(None), Well.id == Document.well_id),
            or_(Document.wellbore_id.is_(None), Wellbore.id == Document.wellbore_id),
            or_(well_id.is_(None), Well.id == well_id),
            or_(wellbore_id.is_(None), Wellbore.id == wellbore_id),
            or_(Document.section_id.is_(None), WellSection.id == Document.section_id),
        )
        .exists()
    )
    operation_exists = (
        select(Operation.id)
        .join(Well, Well.id == Operation.well_id)
        .where(
            Operation.id == operation_id,
            Operation.org_id == Document.org_id,
            Well.org_id == Document.org_id,
            or_(Document.project_id.is_(None), Well.project_id == Document.project_id),
            or_(Document.well_id.is_(None), Operation.well_id == Document.well_id),
            or_(Document.wellbore_id.is_(None), Operation.wellbore_id == Document.wellbore_id),
            or_(Document.section_id.is_(None), Operation.section_id.is_(None), Operation.section_id == Document.section_id),
            or_(well_id.is_(None), Operation.well_id == well_id),
            or_(wellbore_id.is_(None), Operation.wellbore_id == wellbore_id),
            or_(section_id.is_(None), Operation.section_id.is_(None), Operation.section_id == section_id),
            or_(Document.operation_id.is_(None), Operation.id == Document.operation_id),
        )
        .exists()
    )
    return (
        or_(well_id.is_(None), well_exists),
        or_(wellbore_id.is_(None), wellbore_exists),
        or_(section_id.is_(None), section_exists),
        or_(operation_id.is_(None), operation_exists),
    )


def chunk_document_scope_conditions():
    """Reject copied chunk scopes that contradict fields declared by their parent document.

    A document can be filed at a broad scope (for example, a wellbore-level report) while an individual
    passage is associated with a more specific section or operation. A non-null parent scope is binding;
    an unset parent field does not erase legitimate child specificity.
    """
    return (
        or_(Document.well_id.is_(None), DocumentChunk.well_id == Document.well_id),
        or_(Document.wellbore_id.is_(None), DocumentChunk.wellbore_id == Document.wellbore_id),
        or_(Document.section_id.is_(None), DocumentChunk.section_id == Document.section_id),
        or_(Document.operation_id.is_(None), DocumentChunk.operation_id == Document.operation_id),
        DocumentChunk.doc_type.is_not_distinct_from(Document.doc_type),
        *_child_hierarchy_conditions(
            well_id=DocumentChunk.well_id,
            wellbore_id=DocumentChunk.wellbore_id,
            section_id=DocumentChunk.section_id,
            operation_id=DocumentChunk.operation_id,
        ),
    )


def extracted_record_scope_conditions():
    """Reject extracted records whose copied scope conflicts with their source document."""
    return (
        or_(Document.project_id.is_(None), ExtractedRecord.project_id.is_(None), ExtractedRecord.project_id == Document.project_id),
        or_(Document.well_id.is_(None), ExtractedRecord.well_id == Document.well_id),
        or_(Document.wellbore_id.is_(None), ExtractedRecord.wellbore_id == Document.wellbore_id),
        or_(Document.section_id.is_(None), ExtractedRecord.section_id == Document.section_id),
        or_(Document.operation_id.is_(None), ExtractedRecord.operation_id == Document.operation_id),
        *_child_hierarchy_conditions(
            well_id=ExtractedRecord.well_id,
            wellbore_id=ExtractedRecord.wellbore_id,
            section_id=ExtractedRecord.section_id,
            operation_id=ExtractedRecord.operation_id,
        ),
    )


def extracted_record_trust_condition(record=ExtractedRecord):
    """Eligible state and a valid confidence value; humans may override the minimum, not the range."""
    valid_human_confidence = or_(
        record.confidence.is_(None),
        and_(record.confidence >= 0.0, record.confidence <= 1.0),
    )
    return and_(
        record.validation_state.in_(PROMOTABLE_VALIDATION_STATES),
        or_(
            and_(record.validation_state == "human_validated", valid_human_confidence),
            and_(
                record.validation_state != "human_validated",
                record.confidence.is_not(None),
                record.confidence >= MIN_PROMOTION_CONFIDENCE,
                record.confidence <= 1.0,
            ),
        ),
    )


def _operational_owner_scope_conditions(owner, *, operation_scope_id):
    """Verify that an operation/event's own scope tags still form a same-tenant tree."""
    owner_well = aliased(Well)
    owner_bore = aliased(Wellbore)
    owner_section = aliased(WellSection)
    owner_operation = aliased(Operation)
    well_project = (
        select(owner_well.project_id)
        .where(owner_well.id == owner.well_id, owner_well.org_id == owner.org_id)
        .scalar_subquery()
    )
    owner_project = func.coalesce(owner.project_id, well_project)
    well_exists = (
        select(owner_well.id)
        .where(
            owner_well.id == owner.well_id,
            owner_well.org_id == owner.org_id,
            or_(owner.project_id.is_(None), owner_well.project_id == owner.project_id),
        )
        .exists()
    )
    wellbore_exists = (
        select(owner_bore.id)
        .join(owner_well, owner_well.id == owner_bore.well_id)
        .where(
            owner_bore.id == owner.wellbore_id,
            owner_bore.org_id == owner.org_id,
            owner_well.org_id == owner.org_id,
            owner_bore.well_id == owner.well_id,
            or_(owner.project_id.is_(None), owner_well.project_id == owner.project_id),
        )
        .exists()
    )
    section_exists = (
        select(owner_section.id)
        .join(owner_bore, owner_bore.id == owner_section.wellbore_id)
        .join(owner_well, owner_well.id == owner_bore.well_id)
        .where(
            owner_section.id == owner.section_id,
            owner_section.org_id == owner.org_id,
            owner_bore.org_id == owner.org_id,
            owner_well.org_id == owner.org_id,
            owner_section.wellbore_id == owner.wellbore_id,
            owner_bore.well_id == owner.well_id,
            or_(owner.project_id.is_(None), owner_well.project_id == owner.project_id),
        )
        .exists()
    )
    conditions = [
        well_exists,
        or_(owner.wellbore_id.is_(None), wellbore_exists),
        or_(owner.section_id.is_(None), section_exists),
    ]
    if hasattr(owner, "operation_id"):
        operation_exists = (
            select(owner_operation.id)
            .where(
                owner_operation.id == operation_scope_id,
                owner_operation.org_id == owner.org_id,
                *operational_source_conditions(owner_operation),
                owner_operation.well_id == owner.well_id,
                or_(owner.wellbore_id.is_(None), owner_operation.wellbore_id == owner.wellbore_id),
                or_(owner.section_id.is_(None), owner_operation.section_id == owner.section_id),
            )
            .exists()
        )
        conditions.append(or_(operation_scope_id.is_(None), operation_exists))
    return tuple(conditions), owner_project


def operational_source_conditions(owner):
    """Fail closed for unreadable or untrusted document-derived operations/events.

    Rows with no document/record provenance remain ordinary records. Otherwise the source document must
    exist in the same organization, be non-quarantined, and have a scope no narrower or different than
    its operation/event. If a source record is attached, it must belong to that document, agree with both
    scopes, remain in an eligible validation state, and either meet the promotion confidence floor or
    have been explicitly human-validated. A record below the gate stays stored for audit/review but is
    not returned as trusted operational input.
    """
    source_document = aliased(Document)
    source_artifact = aliased(RawArtifact)
    source_record = aliased(ExtractedRecord)
    operation_scope_id = owner.operation_id if hasattr(owner, "operation_id") else owner.id
    owner_scope, owner_project = _operational_owner_scope_conditions(
        owner, operation_scope_id=operation_scope_id
    )

    def document_matches_owner():
        return (
            source_document.org_id == owner.org_id,
            or_(source_document.project_id.is_(None), source_document.project_id == owner_project),
            or_(source_document.well_id.is_(None), source_document.well_id == owner.well_id),
            or_(source_document.wellbore_id.is_(None), source_document.wellbore_id == owner.wellbore_id),
            or_(source_document.section_id.is_(None), source_document.section_id == owner.section_id),
            or_(source_document.operation_id.is_(None), source_document.operation_id == operation_scope_id),
            *owner_scope,
        )

    document_exists = (
        select(source_document.id)
        .outerjoin(source_artifact, source_artifact.id == source_document.raw_artifact_id)
        .where(
            source_document.id == owner.source_document_id,
            document_not_quarantined_condition(source_document, source_artifact),
            *document_matches_owner(),
        )
        .exists()
    )

    record_scope_conditions = (
        or_(source_record.project_id.is_(None), source_record.project_id == owner_project),
        or_(source_record.well_id.is_(None), source_record.well_id == owner.well_id),
        or_(source_record.wellbore_id.is_(None), source_record.wellbore_id == owner.wellbore_id),
        or_(source_record.section_id.is_(None), source_record.section_id == owner.section_id),
        or_(source_record.operation_id.is_(None), source_record.operation_id == operation_scope_id),
        or_(source_document.project_id.is_(None), source_record.project_id.is_(None), source_record.project_id == source_document.project_id),
        or_(source_document.well_id.is_(None), source_record.well_id == source_document.well_id),
        or_(source_document.wellbore_id.is_(None), source_record.wellbore_id == source_document.wellbore_id),
        or_(source_document.section_id.is_(None), source_record.section_id == source_document.section_id),
        or_(source_document.operation_id.is_(None), source_record.operation_id == source_document.operation_id),
    )
    record_exists = (
        select(source_record.id)
        .join(source_document, source_document.id == source_record.document_id)
        .outerjoin(source_artifact, source_artifact.id == source_document.raw_artifact_id)
        .where(
            source_record.id == owner.source_record_id,
            source_record.document_id == owner.source_document_id,
            source_record.org_id == owner.org_id,
            source_document.org_id == owner.org_id,
            document_not_quarantined_condition(source_document, source_artifact),
            extracted_record_trust_condition(source_record),
            *document_matches_owner(),
            *record_scope_conditions,
        )
        .exists()
    )
    return (
        or_(owner.source_document_id.is_(None), document_exists),
        or_(
            owner.source_kind != "ddr_promotion",
            owner.source_record_id.is_not(None),
        ),
        or_(owner.source_record_id.is_(None), record_exists),
    )


def trajectory_source_conditions(trajectory=Trajectory):
    """Keep document-derived trajectories out of context when their source or stations go stale.

    Survey stations point at their extracted source record through ``evidence_ref``. A trajectory summary
    (including its total depth and inclination) is only as trustworthy as every station it summarizes,
    so one unavailable/rejected/superseded/low-confidence source excludes that trajectory from ordinary
    context reads. The trajectory and station rows themselves remain stored for audit.
    """
    document = aliased(Document)
    artifact = aliased(RawArtifact)
    bore = aliased(Wellbore)
    well = aliased(Well)
    scoped_section = aliased(WellSection)
    scoped_operation = aliased(Operation)

    bore_exists = (
        select(bore.id)
        .join(well, well.id == bore.well_id)
        .where(
            bore.id == trajectory.wellbore_id,
            bore.org_id == trajectory.org_id,
            well.org_id == trajectory.org_id,
        )
        .exists()
    )
    document_section_exists = (
        select(scoped_section.id)
        .where(
            scoped_section.id == document.section_id,
            scoped_section.org_id == trajectory.org_id,
            scoped_section.wellbore_id == trajectory.wellbore_id,
        )
        .exists()
    )
    document_operation_exists = (
        select(scoped_operation.id)
        .where(
            scoped_operation.id == document.operation_id,
            scoped_operation.org_id == trajectory.org_id,
            scoped_operation.wellbore_id == trajectory.wellbore_id,
        )
        .exists()
    )
    source_document_exists = (
        select(document.id)
        .outerjoin(artifact, artifact.id == document.raw_artifact_id)
        .join(bore, bore.id == trajectory.wellbore_id)
        .join(well, well.id == bore.well_id)
        .where(
            document.id == trajectory.source_document_id,
            document.org_id == trajectory.org_id,
            document_not_quarantined_condition(document, artifact),
            bore.org_id == trajectory.org_id,
            well.org_id == trajectory.org_id,
            or_(document.project_id.is_(None), document.project_id == well.project_id),
            or_(document.well_id.is_(None), document.well_id == well.id),
            or_(document.wellbore_id.is_(None), document.wellbore_id == trajectory.wellbore_id),
            or_(document.section_id.is_(None), document_section_exists),
            or_(document.operation_id.is_(None), document_operation_exists),
        )
        .exists()
    )

    station = aliased(TrajectoryStation)
    record = aliased(ExtractedRecord)
    record_document = aliased(Document)
    record_artifact = aliased(RawArtifact)
    record_bore = aliased(Wellbore)
    record_well = aliased(Well)
    record_section = aliased(WellSection)
    record_operation = aliased(Operation)
    record_section_exists = (
        select(record_section.id)
        .where(
            record_section.id == record.section_id,
            record_section.org_id == trajectory.org_id,
            record_section.wellbore_id == trajectory.wellbore_id,
        )
        .exists()
    )
    record_operation_exists = (
        select(record_operation.id)
        .where(
            record_operation.id == record.operation_id,
            record_operation.org_id == trajectory.org_id,
            record_operation.wellbore_id == trajectory.wellbore_id,
        )
        .exists()
    )
    trusted_station_record = (
        select(record.id)
        .join(record_document, record_document.id == record.document_id)
        .outerjoin(record_artifact, record_artifact.id == record_document.raw_artifact_id)
        .join(record_bore, record_bore.id == trajectory.wellbore_id)
        .join(record_well, record_well.id == record_bore.well_id)
        .where(
            record.id == station.evidence_ref,
            record.org_id == trajectory.org_id,
            record_document.org_id == trajectory.org_id,
            document_not_quarantined_condition(record_document, record_artifact),
            record_bore.org_id == trajectory.org_id,
            record_well.org_id == trajectory.org_id,
            extracted_record_trust_condition(record),
            or_(record.project_id.is_(None), record.project_id == record_well.project_id),
            or_(record.well_id.is_(None), record.well_id == record_well.id),
            or_(record.wellbore_id.is_(None), record.wellbore_id == trajectory.wellbore_id),
            or_(record.section_id.is_(None), record_section_exists),
            or_(record.operation_id.is_(None), record_operation_exists),
            or_(record_document.project_id.is_(None), record.project_id.is_(None), record.project_id == record_document.project_id),
            or_(record_document.well_id.is_(None), record.well_id == record_document.well_id),
            or_(record_document.wellbore_id.is_(None), record.wellbore_id == record_document.wellbore_id),
            or_(record_document.section_id.is_(None), record.section_id == record_document.section_id),
            or_(record_document.operation_id.is_(None), record.operation_id == record_document.operation_id),
        )
        .exists()
    )
    invalid_station_exists = (
        select(station.id)
        .where(
            station.trajectory_id == trajectory.id,
            station.org_id == trajectory.org_id,
            station.evidence_ref.is_not(None),
            ~trusted_station_record,
        )
        .exists()
    )
    return (
        bore_exists,
        or_(trajectory.source_document_id.is_(None), source_document_exists),
        ~invalid_station_exists,
    )


def trajectory_station_evidence_document_condition(
    station=TrajectoryStation,
    trajectory=Trajectory,
    evidence_document_id=EvidenceLink.document_id,
):
    """Require a station evidence link to name that station's own source document.

    A trajectory may contain stations from multiple reports on the same wellbore. In that case the
    trajectory's ``source_document_id`` identifies the report that first created the trajectory, not
    every station's provenance. A record-backed station must therefore match its own record's document;
    a station without a record may only cite the trajectory-level source (or have no document citation).
    """
    source_record = aliased(ExtractedRecord)
    matching_record_document = (
        select(source_record.id)
        .where(
            source_record.id == station.evidence_ref,
            source_record.org_id == station.org_id,
            source_record.document_id == evidence_document_id,
        )
        # This lookup is nested inside the station ``EXISTS`` predicate, which itself is nested in
        # the outer evidence-link query. Correlate the evidence link explicitly; automatic correlation
        # only sees the immediate parent and otherwise lets a mismatched link borrow another link's
        # document id through a cross join.
        .correlate(station, EvidenceLink)
        .exists()
    )
    return or_(
        and_(
            station.evidence_ref.is_not(None),
            evidence_document_id.is_not(None),
            matching_record_document,
        ),
        and_(
            station.evidence_ref.is_(None),
            or_(evidence_document_id.is_(None), evidence_document_id == trajectory.source_document_id),
        ),
    )


def evidence_document_not_quarantined_condition():
    """Permit documentless evidence; otherwise require its same-tenant source to be available."""
    return or_(
        EvidenceLink.document_id.is_(None),
        and_(
            Document.id == EvidenceLink.document_id,
            Document.org_id == EvidenceLink.org_id,
            document_not_quarantined_condition(),
        ),
    )


__all__ = [
    "chunk_document_scope_conditions",
    "document_not_quarantined_condition",
    "evidence_document_not_quarantined_condition",
    "extracted_record_scope_conditions",
    "extracted_record_trust_condition",
    "operational_source_conditions",
    "trajectory_source_conditions",
    "trajectory_station_evidence_document_condition",
]
