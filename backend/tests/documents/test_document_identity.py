"""Document identity: raw bytes are not the same thing as a logical document.

The defect these tests are built around was reproduced before the fix: uploading the *same bytes* to a
second well returned the **first** well's document, because deduplication keyed on the artefact alone.
A daily report filed against the wrong well could not be corrected — the platform would hand back the
document attached to the other well and report success.

The four cases §15–18 require, each asserted separately because each has a different answer:

===============================================  ==========================================
same bytes, same scope, same revision           one logical document (idempotent)
same bytes, different scope                      two documents sharing one artefact
same bytes, same scope, different revision       two documents sharing one artefact
different bytes, same document number            two documents, two artefacts
===============================================  ==========================================
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from tests.documents.conftest import DDR_BYTES, Fabric

from drillai.documents.identity import logical_key, scope_token
from drillai.documents.scope import DocumentScope, DocumentScopeResolver

# --------------------------------------------------------------------------- the key itself


def test_key_is_content_derived_and_deterministic() -> None:
    base = {
        "content_sha256": "a" * 64,
        "well_id": "wel_1",
        "doc_type": "ddr",
        "revision": "A",
    }
    first = logical_key(**base)
    assert first == logical_key(**base)
    assert first is not None and len(first) == 64


async def test_empty_scope_strings_are_normalized_to_absent(fabric: Fabric) -> None:
    async with fabric.session() as session:
        resolver = DocumentScopeResolver(session, fabric.org_id(fabric.alpha))
        resolved = await resolver.resolve(project_id="")
        narrower = await resolver.resolve(
            DocumentScope(project_id="", well_id=fabric.alpha.well_id)
        )

    assert resolved.as_dict() == {
        "project_id": None,
        "well_id": None,
        "wellbore_id": None,
        "section_id": None,
        "operation_id": None,
    }
    assert resolved.supplied == ()
    assert narrower.project_id == fabric.alpha.project_id
    assert narrower.well_id == fabric.alpha.well_id
    assert narrower.supplied == ("well_id",)


def test_key_ignores_presentation_but_not_identity() -> None:
    """Case-only differences in a revision are the same revision; a different well is not."""
    base = {"content_sha256": "a" * 64, "well_id": "wel_1", "revision": "B"}
    assert logical_key(**base) == logical_key(**{**base, "revision": "b"})
    assert logical_key(**base) == logical_key(**{**base, "revision": " B "})

    # Every part of the key changes it when it is the reference that decides the scope.
    unscoped = {"content_sha256": "a" * 64, "doc_type": "ddr", "revision": "B"}
    assert logical_key(**unscoped) != logical_key(**unscoped, well_id="wel_1")
    for field, other in (
        ("wellbore_id", "wlb_1"),
        ("section_id", "sec_1"),
        ("well_id", "wel_2"),
    ):
        assert logical_key(**base) != logical_key(**{**base, field: other}), field
    assert logical_key(**unscoped) != logical_key(**unscoped, project_id="prj_1")
    for field, other in (("doc_type", "mud_report"), ("revision", "C"), ("content_sha256", "b" * 64)):
        assert logical_key(**base) != logical_key(**{**base, field: other}), field


def test_operation_id_is_an_independent_part_of_logical_identity() -> None:
    common = {
        "content_sha256": "f" * 64,
        "well_id": "wel_1",
        "doc_type": "ddr",
        "revision": "A",
    }
    assert logical_key(**common, operation_id="op_1") != logical_key(**common, operation_id="op_2")
    assert logical_key(**common) != logical_key(**common, operation_id="op_1")
    assert logical_key(**common, operation_id="op_1") == logical_key(
        **common, operation_id="op_1"
    )


def test_a_less_specific_reference_beside_a_more_specific_one_does_not_split_the_key() -> None:
    """The scope is *where the document is*, not the set of fields the caller happened to send.

    A client that sends only ``section_id`` and one that sends the section's wellbore, well and project
    as well are filing the same document in the same place, and the resolver derives the rest. If the
    key counted references instead of resolving them, the two would produce two documents.
    """
    content = {"content_sha256": "a" * 64}
    section_only = logical_key(**content, section_id="sec_1")
    redundant = logical_key(
        **content,
        section_id="sec_1",
        wellbore_id="wlb_1",
        well_id="wel_1",
        project_id="prj_1",
    )
    assert section_only == redundant


def test_a_section_filing_is_not_the_same_document_as_a_wellbore_filing() -> None:
    """The scope token needed ``section_id`` too — the same defect one level down.

    Filing the same bytes against a section and then against the wellbore it sits in produced one
    document, so the section-level filing became a wellbore-level one. Caught while writing the first
    version of these tests, which is why the scope tuple now covers every scope field the model stores.
    """
    common = {"content_sha256": "e" * 64, "well_id": "wel_1"}
    by_section = logical_key(**common, section_id="sec_1", wellbore_id="wlb_1")
    by_wellbore = logical_key(**common, wellbore_id="wlb_1")
    assert by_section != by_wellbore


def test_no_bytes_means_no_identity() -> None:
    """Without bytes there is nothing to identify; the caller must not get a key for nothing."""
    assert logical_key(content_sha256=None, well_id="wel_1") is None
    assert logical_key(content_sha256="", well_id="wel_1") is None


def test_scope_token_takes_the_most_specific_reference() -> None:
    """Order of the arguments is irrelevant; order of *specificity* decides the token."""
    assert scope_token(well_id="wel_1", wellbore_id="wlb_1") == "wlb_1"
    assert scope_token(wellbore_id="wlb_1", well_id="wel_1") == "wlb_1"
    # Most specific wins, all the way down the hierarchy.
    assert scope_token(section_id="sec_1", wellbore_id="wlb_1", well_id="wel_1") == "sec_1"
    assert scope_token(well_id="wel_1", project_id="prj_1") == "wel_1"
    # No scope named is a real answer, and an empty string can never collide with an identifier.
    assert scope_token() == ""
    assert scope_token(project_id="") == ""


def _migration_module():
    """Load the migration that backfills ``logical_key``, exactly as Alembic would."""
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "c0f6d8a9b271_document_operation_identity.py"
    )
    spec = importlib.util.spec_from_file_location("_operation_identity_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_forward_migration_key_copy_agrees_with_the_application() -> None:
    """The forward migration carries a frozen copy of the operation-aware key."""
    frozen = _migration_module().document_logical_key
    # The migration copy takes every identity field explicitly, so each case names all eight inputs.
    cases = [
        {
            "content_sha256": "c" * 64,
            "section_id": None,
            "well_id": "wel_1",
            "wellbore_id": None,
            "project_id": "prj_1",
            "operation_id": None,
            "doc_type": None,
            "revision": None,
        },
        {
            "content_sha256": "d" * 64,
            "section_id": "sec_7",
            "well_id": None,
            "wellbore_id": "wlb_9",
            "project_id": None,
            "operation_id": "op_9",
            "doc_type": "ddr",
            "revision": "Rev-B",
        },
        {
            "content_sha256": "f" * 64,
            "section_id": None,
            "well_id": None,
            "wellbore_id": None,
            "project_id": None,
            "operation_id": None,
            "doc_type": " Mud_Report ",
            "revision": " A ",
        },
        {
            "content_sha256": "g" * 64,
            "section_id": None,
            "well_id": None,
            "wellbore_id": None,
            "project_id": None,
            "operation_id": "op_2",
            "doc_type": None,
            "revision": None,
        },
        {
            "content_sha256": None,
            "section_id": None,
            "well_id": "wel_1",
            "wellbore_id": None,
            "project_id": None,
            "operation_id": None,
            "doc_type": None,
            "revision": None,
        },
    ]
    for case in cases:
        assert frozen(**case) == logical_key(**case), case


async def _exercise_operation_identity_migration(session) -> None:
    import datetime as dt

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import select, text
    from sqlalchemy.exc import IntegrityError

    from drillai.db.models import Document, IngestionJob, Organization, RawArtifact

    migration = _migration_module()
    connection = await session.connection()
    # The fixture creates the current model schema. Remove the new indexes first so this test can
    # insert the duplicate legacy keys and byte artifacts that existed before the migration.
    await connection.execute(text("DROP INDEX uq_documents_org_logical_key"))
    await connection.execute(text("DROP INDEX uq_raw_artifacts_org_sha256"))

    org = Organization(id="org_identity_migration", slug="identity-migration", name="Identity migration")
    other_org = Organization(
        id="org_identity_migration_other", slug="identity-migration-other", name="Other tenant"
    )
    created_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    digest = "a" * 64
    raw = RawArtifact(
        id="raw_identity_migration",
        org_id=org.id,
        blob_key="identity-migration/report.txt",
        sha256=digest,
        byte_size=1,
        content_type="text/plain",
        original_filename="original-report.txt",
        source_kind="upload",
        source_ref={"source": "primary"},
        uploaded_by="usr_primary",
        retention_class="standard",
        is_quarantined=False,
        scan_status="clean",
        attributes={"primary_marker": "preserved"},
        created_at=created_at,
    )
    duplicate_raw = RawArtifact(
        id="raw_identity_migration_duplicate",
        org_id=org.id,
        blob_key="identity-migration/report-copy.txt",
        sha256=digest,
        byte_size=1,
        content_type="text/plain",
        original_filename="copied-report.txt",
        source_kind="connector",
        source_ref={"connector": "legacy"},
        uploaded_by="usr_secondary",
        retention_class="legal_hold",
        is_quarantined=True,
        scan_status="blocked",
        attributes={"duplicate_marker": "must survive migration"},
        created_at=created_at + dt.timedelta(days=1),
    )
    legacy_key = logical_key(content_sha256=digest, well_id="wel_legacy", doc_type="ddr")
    assert legacy_key is not None
    documents = [
        Document(
            id="doc_identity_migration_a",
            org_id=org.id,
            raw_artifact_id=raw.id,
            well_id="wel_legacy",
            operation_id="opr_a",
            doc_type="ddr",
            title="operation A, first copy",
            status="uploaded",
            checksum=digest,
            logical_key=legacy_key,
            created_at=created_at,
        ),
        Document(
            id="doc_identity_migration_b",
            org_id=org.id,
            raw_artifact_id=raw.id,
            well_id="wel_legacy",
            operation_id="opr_b",
            doc_type="ddr",
            title="operation B",
            status="uploaded",
            checksum=digest,
            logical_key=legacy_key,
            created_at=created_at + dt.timedelta(days=1),
        ),
        Document(
            id="doc_identity_migration_c",
            org_id=org.id,
            raw_artifact_id=raw.id,
            well_id="wel_legacy",
            operation_id="opr_a",
            doc_type="ddr",
            title="operation A, duplicate legacy copy",
            status="uploaded",
            checksum=digest,
            logical_key=legacy_key,
            created_at=created_at + dt.timedelta(days=2),
        ),
        Document(
            id="doc_identity_migration_d",
            org_id=org.id,
            raw_artifact_id=None,
            well_id="wel_legacy",
            operation_id="opr_c",
            doc_type="ddr",
            title="checksum fallback",
            status="uploaded",
            checksum=digest,
            logical_key=legacy_key,
            created_at=created_at + dt.timedelta(days=3),
        ),
        Document(
            id="doc_identity_migration_e",
            org_id=org.id,
            raw_artifact_id=duplicate_raw.id,
            well_id="wel_legacy",
            operation_id="opr_d",
            doc_type="ddr",
            title="document linked to duplicate raw bytes",
            status="uploaded",
            checksum=digest,
            logical_key=legacy_key,
            created_at=created_at + dt.timedelta(days=4),
        ),
    ]
    session.add_all([org, other_org, raw, duplicate_raw])
    await session.flush()
    session.add_all(documents)
    session.add(
        IngestionJob(
            id="job_identity_migration_duplicate_raw",
            org_id=org.id,
            raw_artifact_id=duplicate_raw.id,
            document_id="doc_identity_migration_e",
            status="queued",
            trigger="upload",
            pipeline=[],
            stats={},
            extractor_versions={},
            attempts=0,
            warnings=[],
        )
    )
    await session.flush()

    def apply(sync_connection, operation) -> None:
        context = MigrationContext.configure(sync_connection)
        with Operations.context(context):
            operation()

    async def values() -> dict[str, str | None]:
        rows = await session.execute(
            select(Document.id, Document.logical_key)
            .where(Document.id.in_([document.id for document in documents]))
            .order_by(Document.id)
        )
        return dict(rows.all())

    key_a = logical_key(content_sha256=digest, well_id="wel_legacy", doc_type="ddr", operation_id="opr_a")
    key_b = logical_key(content_sha256=digest, well_id="wel_legacy", doc_type="ddr", operation_id="opr_b")
    key_c = logical_key(content_sha256=digest, well_id="wel_legacy", doc_type="ddr", operation_id="opr_c")
    key_d = logical_key(content_sha256=digest, well_id="wel_legacy", doc_type="ddr", operation_id="opr_d")
    assert key_a is not None and key_b is not None and key_c is not None and key_d is not None
    expected = {
        "doc_identity_migration_a": key_a,
        "doc_identity_migration_b": key_b,
        "doc_identity_migration_c": None,
        "doc_identity_migration_d": key_c,
        "doc_identity_migration_e": key_d,
    }

    await connection.run_sync(lambda sync_connection: apply(sync_connection, migration.upgrade))
    assert await values() == expected
    remaining_raw_ids = (
        await session.execute(select(RawArtifact.id).where(RawArtifact.org_id == org.id))
    ).scalars().all()
    assert remaining_raw_ids == [raw.id]
    await session.refresh(raw)
    archived_duplicates = raw.attributes["migration_deduplicated_raw_artifacts"]
    assert raw.attributes["primary_marker"] == "preserved"
    assert len(archived_duplicates) == 1
    archived = archived_duplicates[0]
    assert {key: value for key, value in archived.items() if key != "created_at"} == {
        "id": duplicate_raw.id,
        "org_id": org.id,
        "sha256": digest,
        "blob_key": duplicate_raw.blob_key,
        "byte_size": duplicate_raw.byte_size,
        "content_type": duplicate_raw.content_type,
        "original_filename": "copied-report.txt",
        "source_kind": "connector",
        "source_ref": {"connector": "legacy"},
        "uploaded_by": "usr_secondary",
        "retention_class": "legal_hold",
        "is_quarantined": True,
        "scan_status": "blocked",
        "attributes": {"duplicate_marker": "must survive migration"},
    }
    archived_created_at = dt.datetime.fromisoformat(archived["created_at"])
    if archived_created_at.tzinfo is None:
        archived_created_at = archived_created_at.replace(tzinfo=dt.UTC)
    assert archived_created_at == created_at + dt.timedelta(days=1)
    assert raw.retention_class == "mixed"
    assert raw.source_kind == "mixed"
    assert raw.is_quarantined is True
    assert raw.scan_status == "review_required"
    assert raw.original_filename == "original-report.txt"
    assert raw.uploaded_by == "usr_primary"
    assert raw.source_ref == {"source": "primary"}
    assert await session.scalar(
        select(Document.raw_artifact_id).where(Document.id == "doc_identity_migration_e")
    ) == raw.id
    assert await session.scalar(
        select(IngestionJob.raw_artifact_id).where(IngestionJob.id == "job_identity_migration_duplicate_raw")
    ) == raw.id

    await connection.run_sync(lambda sync_connection: apply(sync_connection, migration.downgrade))
    assert await values() == {document.id: legacy_key for document in documents}
    # The downgrade deliberately restores the old key semantics, not duplicate raw rows or their
    # pre-merge references. The full removed-row metadata remains archived for operators.
    assert (
        await session.execute(select(RawArtifact.id).where(RawArtifact.org_id == org.id))
    ).scalars().all() == [raw.id]
    await session.refresh(raw)
    assert raw.attributes["migration_deduplicated_raw_artifacts"] == archived_duplicates
    assert await session.scalar(
        select(Document.raw_artifact_id).where(Document.id == "doc_identity_migration_e")
    ) == raw.id
    assert await session.scalar(
        select(IngestionJob.raw_artifact_id).where(IngestionJob.id == "job_identity_migration_duplicate_raw")
    ) == raw.id

    await connection.run_sync(lambda sync_connection: apply(sync_connection, migration.upgrade))
    assert await values() == expected
    await session.refresh(raw)
    assert len(raw.attributes["migration_deduplicated_raw_artifacts"]) == 1

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                Document(
                    id="doc_identity_migration_duplicate",
                    org_id=org.id,
                    well_id="wel_legacy",
                    operation_id="opr_a",
                    doc_type="ddr",
                    title="must be rejected by the scoped unique index",
                    status="uploaded",
                    checksum=digest,
                    logical_key=key_a,
                )
            )
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                RawArtifact(
                    id="raw_identity_migration_duplicate_after_upgrade",
                    org_id=org.id,
                    blob_key="identity-migration/duplicate-after-upgrade.txt",
                    sha256=digest,
                    byte_size=1,
                    content_type="text/plain",
                    source_kind="upload",
                    retention_class="standard",
                    scan_status="clean",
                    attributes={},
                )
            )
            await session.flush()

    # Both unique indexes are tenant-scoped: another organization may use the same logical key and bytes.
    session.add(
        RawArtifact(
            id="raw_identity_migration_other_tenant",
            org_id=other_org.id,
            blob_key="identity-migration/other-tenant-report.txt",
            sha256=digest,
            byte_size=1,
            content_type="text/plain",
            source_kind="upload",
            retention_class="standard",
            scan_status="clean",
            attributes={},
        )
    )
    session.add(
        Document(
            id="doc_identity_migration_other_tenant",
            org_id=other_org.id,
            well_id="wel_legacy",
            operation_id="opr_a",
            doc_type="ddr",
            title="same key, other tenant",
            status="uploaded",
            checksum=digest,
            logical_key=key_a,
        )
    )
    await session.flush()


async def test_operation_identity_migration_downgrade_preserves_raw_archive_on_sqlite(session) -> None:
    await _exercise_operation_identity_migration(session)


@pytest.mark.postgres
async def test_operation_identity_migration_downgrade_preserves_raw_archive_on_postgres(pg_session) -> None:
    await _exercise_operation_identity_migration(pg_session)


async def test_auto_classified_upload_key_matches_the_migration_and_remains_idempotent(
    fabric: Fabric,
) -> None:
    from drillai.db.models import Document, RawArtifact

    first = await fabric.upload(
        fabric.alpha,
        DDR_BYTES,
        filename="report.txt",
        well_id=fabric.alpha.well_id,
    )
    assert first.status_code == 201, first.text
    document_id = first.json()["document"]["id"]

    async with fabric.session() as session:
        document = await session.get(Document, document_id)
        assert document is not None and document.doc_type == "ddr"
        artifact = await session.get(RawArtifact, document.raw_artifact_id)
        assert artifact is not None
        migration_key = _migration_module().document_logical_key(
            content_sha256=artifact.sha256,
            section_id=document.section_id,
            wellbore_id=document.wellbore_id,
            well_id=document.well_id,
            project_id=document.project_id,
            operation_id=document.operation_id,
            doc_type=document.doc_type,
            revision=document.revision,
        )
        assert document.logical_key == migration_key

    second = await fabric.upload(
        fabric.alpha,
        DDR_BYTES,
        filename="report.txt",
        well_id=fabric.alpha.well_id,
    )
    assert second.status_code == 201, second.text
    assert second.json()["document"]["id"] == document_id
    assert second.json()["scope"]["reused_existing"] is True


async def test_untyped_parse_failure_retry_uses_the_migration_identity(fabric: Fabric) -> None:
    from sqlalchemy import select

    from drillai.db.models import Document, IngestionJob, RawArtifact

    payload = b"%PDF-1.4\nthis is not a valid PDF"
    for _ in range(2):
        response = await fabric.upload(
            fabric.alpha,
            payload,
            filename="broken.pdf",
            content_type="application/pdf",
            well_id=fabric.alpha.well_id,
        )
        assert response.status_code == 422

    async with fabric.session() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        jobs = (await session.execute(select(IngestionJob).order_by(IngestionJob.created_at))).scalars().all()
        artifacts = (await session.execute(select(RawArtifact))).scalars().all()
        assert len(documents) == len(artifacts) == 1
        document = documents[0]
        assert document.status == "failed" and document.doc_type == "other"
        migration_key = _migration_module().document_logical_key(
            content_sha256=artifacts[0].sha256,
            section_id=document.section_id,
            wellbore_id=document.wellbore_id,
            well_id=document.well_id,
            project_id=document.project_id,
            operation_id=document.operation_id,
            doc_type=document.doc_type,
            revision=document.revision,
        )
        assert document.logical_key == migration_key
        assert [job.status for job in jobs] == ["failed", "failed"]
        assert all(job.document_id == document.id for job in jobs)


# --------------------------------------------------------------------------- the four cases


async def test_same_bytes_same_scope_is_idempotent(fabric: Fabric) -> None:
    payload = b"identical DDR bytes, filed once"
    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    second = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    assert first.status_code == second.status_code == 201
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["scope"]["reused_existing"] is True
    assert second.json()["job"]["status"] == "skipped_duplicate"


async def test_operation_scoped_upload_identity_is_returned_and_idempotent(fabric: Fabric) -> None:
    operations = []
    for sequence in (71, 72):
        response = await fabric.http.post(
            "/api/v1/operations",
            json={
                "project_id": fabric.alpha.project_id,
                "well_id": fabric.alpha.well_id,
                "wellbore_id": fabric.alpha.wellbore_id,
                "section_id": fabric.alpha.section_id,
                "sequence": sequence,
                "name": f"Operation-scoped upload {sequence}",
                "kind": "drilling",
                "operation_class": "actual",
                "status": "completed",
            },
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 201, response.text
        operations.append(response.json())
    first_operation, second_operation = operations
    assert first_operation is not None and second_operation is not None

    payload = b"same report bytes, different operation scope"
    common = {
        "well_id": fabric.alpha.well_id,
        "wellbore_id": fabric.alpha.wellbore_id,
        "section_id": fabric.alpha.section_id,
        "doc_type": "ddr",
    }
    first = await fabric.upload(
        fabric.alpha, payload, operation_id=first_operation["id"], **common
    )
    repeat = await fabric.upload(
        fabric.alpha, payload, operation_id=first_operation["id"], **common
    )
    other_operation = await fabric.upload(
        fabric.alpha, payload, operation_id=second_operation["id"], **common
    )

    assert first.status_code == repeat.status_code == other_operation.status_code == 201
    first_document = first.json()["document"]
    repeat_document = repeat.json()["document"]
    other_document = other_operation.json()["document"]
    assert first_document["operation_id"] == first_operation["id"]
    assert first.json()["scope"]["operation_id"] == first_operation["id"]
    assert repeat_document["id"] == first_document["id"]
    assert repeat.json()["job"]["status"] == "skipped_duplicate"
    assert other_document["id"] != first_document["id"]
    assert other_document["operation_id"] == second_operation["id"]
    assert other_document["raw_artifact_id"] == first_document["raw_artifact_id"]
    assert other_document["logical_key"] != first_document["logical_key"]


async def test_same_bytes_different_scope_is_a_second_document(fabric: Fabric) -> None:
    """FINDING D: this returned the first well's document, so a misfiled report was uncorrectable."""
    payload = b"identical DDR bytes, filed against two wells"
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-9")

    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    second = await fabric.upload(fabric.alpha, payload, well_id=other_well)
    assert first.status_code == second.status_code == 201

    first_body, second_body = first.json(), second.json()
    assert second_body["document"]["id"] != first_body["document"]["id"]
    assert second_body["document"]["well_id"] == other_well
    assert second_body["scope"]["reused_existing"] is False
    # One artefact, two documents: §17 explicitly forbids duplicating the bytes for a scope change.
    assert second_body["document"]["raw_artifact_id"] == first_body["document"]["raw_artifact_id"]
    assert second_body["document"]["logical_key"] != first_body["document"]["logical_key"]


async def test_same_bytes_same_scope_different_revision_is_a_second_document(fabric: Fabric) -> None:
    """A reissue is a different logical document, and the first one is not overwritten."""
    payload = b"identical DDR bytes, revision A then revision B"
    first = await fabric.upload(
        fabric.alpha, payload, well_id=fabric.alpha.well_id, revision="A", title="DDR rev A"
    )
    second = await fabric.upload(
        fabric.alpha, payload, well_id=fabric.alpha.well_id, revision="B", title="DDR rev B"
    )
    assert first.status_code == second.status_code == 201
    assert second.json()["document"]["id"] != first.json()["document"]["id"]
    assert second.json()["document"]["revision"] == "B"

    # Both are still readable: the revision did not replace its predecessor.
    listing = await fabric.http.get(
        "/api/v1/documents", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    assert listing.status_code == 200
    ids = {row["id"] for row in listing.json()["items"]}
    assert {first.json()["document"]["id"], second.json()["document"]["id"]} <= ids


async def test_different_bytes_same_document_number_are_two_documents(fabric: Fabric) -> None:
    """Identity is content-derived; a human's document number is metadata, not identity.

    Two documents numbered ``DDR-001`` with different bytes is a real filing mistake, and collapsing
    them would delete one of them. Both are kept, and both keep their own artefact.
    """
    first = await fabric.upload(
        fabric.alpha, DDR_BYTES, well_id=fabric.alpha.well_id, filename="one.txt"
    )
    second = await fabric.upload(
        fabric.alpha, DDR_BYTES + b"\nADDITIONAL NOTE: pump pressure 120 bar\n",
        well_id=fabric.alpha.well_id, filename="two.txt",
    )
    assert first.status_code == second.status_code == 201
    assert second.json()["document"]["id"] != first.json()["document"]["id"]
    assert second.json()["document"]["raw_artifact_id"] != first.json()["document"]["raw_artifact_id"]


async def test_same_bytes_filed_to_different_operations_are_distinct_documents(fabric: Fabric) -> None:
    from drillai.db.models import Operation

    async with fabric.session() as session:
        operations = [
            Operation(
                org_id=fabric.org_id(fabric.alpha),
                project_id=fabric.alpha.project_id,
                well_id=fabric.alpha.well_id,
                wellbore_id=fabric.alpha.wellbore_id,
                section_id=fabric.alpha.section_id,
                name=name,
                sequence=index,
                kind="drilling",
                phase="completed",
                status="completed",
                operation_class="actual",
            )
            for index, name in enumerate(("Drill ahead", "Make connection"), start=1)
        ]
        session.add_all(operations)
        await session.flush()
        operation_ids = [operation.id for operation in operations]
        await session.commit()

    payload = b"same report bytes linked to two different operation records"
    first = await fabric.upload(
        fabric.alpha,
        payload,
        well_id=fabric.alpha.well_id,
        operation_id=operation_ids[0],
    )
    second = await fabric.upload(
        fabric.alpha,
        payload,
        well_id=fabric.alpha.well_id,
        operation_id=operation_ids[1],
    )
    repeat = await fabric.upload(
        fabric.alpha,
        payload,
        well_id=fabric.alpha.well_id,
        operation_id=operation_ids[0],
    )

    assert first.status_code == second.status_code == repeat.status_code == 201
    assert first.json()["document"]["id"] != second.json()["document"]["id"]
    assert first.json()["document"]["raw_artifact_id"] == second.json()["document"]["raw_artifact_id"]
    assert repeat.json()["document"]["id"] == first.json()["document"]["id"]
    assert repeat.json()["scope"]["reused_existing"] is True


async def test_a_cross_scope_upload_does_not_reuse_another_tenants_bytes(fabric: Fabric) -> None:
    """The dedup search is org-scoped: bravo's identical bytes are not alpha's document."""
    payload = b"a file both tenants happen to have"
    alpha = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    bravo = await fabric.upload(fabric.bravo, payload, well_id=fabric.bravo.well_id)
    assert alpha.status_code == bravo.status_code == 201
    assert bravo.json()["scope"]["reused_existing"] is False
    assert bravo.json()["document"]["id"] != alpha.json()["document"]["id"]
    # Two organizations, two artefacts as well: an artefact row is tenant-owned, so reusing one across
    # organizations would leak the fact that the other tenant already had those bytes.
    assert bravo.json()["document"]["raw_artifact_id"] != alpha.json()["document"]["raw_artifact_id"]


@pytest.mark.parametrize("field", ["title", "doc_type", "period_start"])
async def test_metadata_differences_do_not_change_identity(fabric: Fabric, field: str) -> None:
    """§18: a rename or reclassification is not a new document.

    ``doc_type`` is part of the *key*, so changing it is a reclassification and deliberately produces a
    new logical document while keeping one artefact; a title is not part of the key and must not.
    """
    payload = f"payload for the {field} case".encode()
    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    assert first.status_code == 201, first.text
    extra: dict[str, str] = {}
    if field == "period_start":
        extra["period_start"] = "2026-03-01T00:00:00+00:00"
    elif field == "doc_type":
        # A different recognised type: a reclassification, which is a different logical document
        # sharing the same bytes.
        extra["doc_type"] = "mud_report"
    else:
        extra[field] = "renamed or reclassified"
    second = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id, **extra)
    assert second.status_code == 201, second.text

    if field == "doc_type":
        assert second.json()["document"]["id"] != first.json()["document"]["id"]
        assert (
            second.json()["document"]["raw_artifact_id"]
            == first.json()["document"]["raw_artifact_id"]
        )
    else:
        assert second.json()["document"]["id"] == first.json()["document"]["id"]
        assert second.json()["scope"]["reused_existing"] is True


@pytest.mark.postgres
async def test_concurrent_uploads_serialize_to_one_logical_document(pg_engine, monkeypatch) -> None:
    """Two independent transactions that both miss the preflight lookup cannot create duplicates."""
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import drillai.ingestion.pipeline as pipeline_module
    from drillai.db.models import Document, IngestionJob, Organization, RawArtifact
    from drillai.ingestion.pipeline import IngestionPipeline
    from drillai.ingestion.storage import MemoryBlobStore

    factory = async_sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    store = MemoryBlobStore()
    payload = (
        b"concurrent report bytes with no scope-specific mutable data. "
        b"This longer note creates one source chunk without producing extracted engineering records."
    )
    blob = store.put(payload, content_type="text/plain")
    org = Organization(id="org_doc_race", slug="document-race", name="Document race")
    raw = RawArtifact(
        org_id=org.id,
        blob_key=blob.key,
        sha256=blob.sha256,
        byte_size=blob.byte_size,
        content_type=blob.content_type,
        source_kind="upload",
        retention_class="standard",
        scan_status="clean",
        attributes={},
    )
    async with factory() as session:
        session.add_all([org, raw])
        await session.commit()

    original_find = pipeline_module.find_logical_document
    both_preflight_misses = asyncio.Barrier(2)
    initial_lookups = 0

    async def synchronized_find(session, **kwargs):
        nonlocal initial_lookups
        result = await original_find(session, **kwargs)
        if result is None and initial_lookups < 2:
            initial_lookups += 1
            await both_preflight_misses.wait()
        return result

    monkeypatch.setattr(pipeline_module, "find_logical_document", synchronized_find)

    async def upload_once():
        async with factory() as session:
            return await IngestionPipeline(session, store, org_id=org.id).ingest(
                payload,
                filename="concurrent.txt",
                content_type="text/plain",
                well_id="wel_race",
                doc_type="ddr",
                link_to_well=False,
                commit=True,
            )

    first, second = await asyncio.gather(upload_once(), upload_once())
    assert first.document is not None and second.document is not None
    assert first.document.id == second.document.id
    assert sorted((first.job.status, second.job.status)) == ["skipped_duplicate", "succeeded"]

    async with factory() as session:
        document_count = await session.scalar(
            select(func.count()).select_from(Document).where(Document.org_id == org.id)
        )
        artifact_count = await session.scalar(
            select(func.count()).select_from(RawArtifact).where(RawArtifact.org_id == org.id)
        )
        job_count = await session.scalar(
            select(func.count()).select_from(IngestionJob).where(IngestionJob.org_id == org.id)
        )
    assert document_count == 1
    assert artifact_count == 1
    assert job_count == 2


@pytest.mark.postgres
async def test_concurrent_uploads_of_one_artifact_to_different_scopes_create_two_documents(pg_engine):
    """An artifact uniqueness race must not turn a different-scope filing into a failed upload."""
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from drillai.db.models import Document, IngestionJob, Organization, RawArtifact
    from drillai.ingestion.pipeline import IngestionPipeline
    from drillai.ingestion.storage import MemoryBlobStore

    raw_insert_barrier = asyncio.Barrier(2)

    class BarrierSession(AsyncSession):
        async def flush(self, objects=None):
            if not self.info.get("raw_insert_barrier_used") and any(
                isinstance(item, RawArtifact) for item in self.new
            ):
                self.info["raw_insert_barrier_used"] = True
                await raw_insert_barrier.wait()
            await super().flush(objects)

    factory = async_sessionmaker(
        bind=pg_engine,
        expire_on_commit=False,
        autoflush=False,
        class_=BarrierSession,
    )
    store = MemoryBlobStore()
    org = Organization(id="org_doc_scope_race", slug="document-scope-race", name="Document scope race")
    async with factory() as session:
        session.add(org)
        await session.commit()

    payload = (
        b"same immutable bytes, but two separate logical filing targets. "
        b"The source is deliberately long enough to exercise the full parser and persistence path."
    )

    async def upload_once(well_id: str):
        async with factory() as session:
            return await IngestionPipeline(session, store, org_id=org.id).ingest(
                payload,
                filename="scope-race.txt",
                content_type="text/plain",
                well_id=well_id,
                doc_type="ddr",
                link_to_well=False,
                commit=True,
            )

    first, second = await asyncio.gather(upload_once("wel_scope_a"), upload_once("wel_scope_b"))

    assert first.document is not None and second.document is not None
    assert first.document.id != second.document.id
    assert {first.document.well_id, second.document.well_id} == {"wel_scope_a", "wel_scope_b"}
    assert first.raw_artifact.id == second.raw_artifact.id
    async with factory() as session:
        document_count = await session.scalar(
            select(func.count()).select_from(Document).where(Document.org_id == org.id)
        )
        artifact_count = await session.scalar(
            select(func.count()).select_from(RawArtifact).where(RawArtifact.org_id == org.id)
        )
        job_count = await session.scalar(
            select(func.count()).select_from(IngestionJob).where(IngestionJob.org_id == org.id)
        )
    assert document_count == 2
    assert artifact_count == 1
    assert job_count == 2
