"""The document life cycle: one vocabulary, and the rules that reach it.

Before CP8 the platform held six document statuses and knew four of them. The ingestion pipeline wrote
``parsing`` while it worked, then ``parsed`` or ``ingested`` when it finished; ``drilling/ddr.py`` wrote
``validated`` onto extracted records; ``drilling/state.py`` *read* ``validated``. None of those six
appeared in ``DOC_STATUSES`` or ``VALIDATION_STATES``. These tests are the guard that keeps it that
way: they read the vocabulary from the model, the values from the database and the API, and assert that
the sets agree — rather than asserting a particular string, which is how the drift went unnoticed.
"""

from __future__ import annotations

import pathlib

import pytest
from tests.documents.conftest import DDR_BYTES, Fabric
from tests.fixtures.documents_bytes import scanned_pdf

from drillai.db.models import (
    DOC_STATUSES,
    INGESTION_JOB_STATUSES,
    VALIDATION_STATES,
    Document,
    ExtractedRecord,
    IngestionJob,
)
from drillai.documents.lifecycle import (
    DOCUMENT_EXTRACTED_STATUSES,
    DOCUMENT_TERMINAL_STATUSES,
    DOCUMENT_TRANSITIONS,
    VALIDATED_STATES,
    VALIDATION_TRANSITIONS,
    extraction_completeness,
    plan_document_status,
)

# --------------------------------------------------------------------------- the vocabulary itself


def test_document_status_vocabulary_has_no_duplicates_or_dead_entries() -> None:
    assert len(set(DOC_STATUSES)) == len(DOC_STATUSES)
    assert "uploaded" in DOC_STATUSES and "archived" in DOC_STATUSES
    # The three values the pipeline used to write and that nothing declared. If one is ever added back,
    # this test fails and whoever added it has to explain which of the canonical states it replaces.
    assert not {"parsing", "parsed", "ingested"} & set(DOC_STATUSES)


def test_every_declared_status_has_a_transition_entry() -> None:
    """A status with no entry is a status nothing can reach or leave — an orphan in the vocabulary."""
    assert set(DOCUMENT_TRANSITIONS) == set(DOC_STATUSES)
    for source, targets in DOCUMENT_TRANSITIONS.items():
        unknown = set(targets) - set(DOC_STATUSES)
        assert not unknown, f"{source} names transitions to undeclared statuses: {unknown}"


def test_terminal_and_extracted_sets_are_drawn_from_the_vocabulary() -> None:
    assert set(DOCUMENT_TERMINAL_STATUSES) <= set(DOC_STATUSES)
    assert set(DOCUMENT_EXTRACTED_STATUSES) <= set(DOC_STATUSES)
    for terminal in DOCUMENT_TERMINAL_STATUSES:
        assert not DOCUMENT_TRANSITIONS[terminal], f"{terminal} is terminal but has transitions"


def test_validation_vocabulary_and_derived_sets_agree() -> None:
    assert not {"unvalidated", "validated"} & set(VALIDATION_STATES)
    assert set(VALIDATION_STATES) >= VALIDATED_STATES
    assert "extracted" not in VALIDATED_STATES, "machine extraction is not validation"
    assert set(VALIDATION_TRANSITIONS) == set(VALIDATION_STATES)


def test_ingestion_job_status_is_a_separate_vocabulary() -> None:
    """§12: a job's outcome and a document's state are different questions with different answers.

    The two sets are *not* disjoint and must not be asserted to be: "failed" is the right word at both
    levels, and pretending otherwise would force a synonym nobody would use. What matters is that the
    job vocabulary is not a subset of the document vocabulary and carries outcomes a document cannot
    have — ``queued``, ``running``, ``succeeded``, ``skipped_duplicate`` — so a client cannot read one
    as the other and be right by accident.
    """
    job_only = set(INGESTION_JOB_STATUSES) - set(DOC_STATUSES)
    assert job_only >= {"queued", "running", "succeeded", "skipped_duplicate"}
    assert set(INGESTION_JOB_STATUSES) - {"failed"} != set(DOC_STATUSES) - {"failed"}
    # "partial" is the job's word; the document's is "partially_extracted". Deliberately different
    # spellings, because they are different facts and a shared string invited exactly the confusion
    # this vocabulary split exists to prevent.
    assert "partial" in INGESTION_JOB_STATUSES
    assert "partial" not in DOC_STATUSES
    assert "partially_extracted" in DOC_STATUSES


# --------------------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"parsed": False, "has_records": False, "has_chunks": False}, "failed"),
        ({"parsed": True, "has_records": False, "has_chunks": False}, "partially_extracted"),
        ({"parsed": True, "has_records": True, "has_chunks": True}, "extracted"),
        (
            {"parsed": True, "has_records": True, "has_chunks": True, "ocr_required": True},
            "partially_extracted",
        ),
        (
            {
                "parsed": True,
                "has_records": True,
                "has_chunks": True,
                "extractor_failures": 1,
            },
            "partially_extracted",
        ),
        (
            {"parsed": True, "has_records": True, "has_chunks": True, "warnings": ["parser note"]},
            "partially_extracted",
        ),
        ({"parsed": True, "has_records": False, "has_chunks": True}, "extracted"),
    ],
)
def test_plan_document_status_is_exhaustive(kwargs: dict, expected: str) -> None:
    assert plan_document_status(**kwargs) == expected
    assert expected in DOC_STATUSES


def test_extraction_completeness_adds_up() -> None:
    partial = extraction_completeness(extracted=3, total=4, warnings=["one extractor failed"])
    assert partial == {
        "extracted": 3,
        "total_candidates": 4,
        "warning_count": 1,
        "complete": False,
    }
    complete = extraction_completeness(extracted=4, total=4)
    assert complete["complete"] is True


# --------------------------------------------------------------------------- what the platform stores


async def test_ingested_document_status_is_in_the_vocabulary(fabric: Fabric) -> None:
    """FINDING B: after ingest the document's status was ``parsed``, which nothing declared."""
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["document"]["status"] in DOC_STATUSES, body["document"]["status"]
    assert body["document"]["status"] == "extracted"
    assert body["job"]["status"] in INGESTION_JOB_STATUSES
    assert body["job"]["status"] == "succeeded"


async def test_extracted_record_states_are_in_the_vocabulary(fabric: Fabric) -> None:
    """FINDING C: records were written ``unvalidated``, which is not a declared state."""
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]
    detail = await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers)
    assert detail.status_code == 200
    records = detail.json()["records"]
    assert records, "the report has readable content; a document with no records is a different bug"
    states = {record["validation_state"] for record in records}
    assert states <= set(VALIDATION_STATES), states - set(VALIDATION_STATES)
    # Nothing has checked these rows, so none of them may claim to have been checked.
    assert not states & VALIDATED_STATES, "extraction must not auto-validate"
    assert states == {"extracted"}


async def test_stored_values_are_a_subset_of_the_declared_vocabularies(fabric: Fabric) -> None:
    """The audit §90 asks for, as a test: DB distinct values ⊆ canonical vocabulary.

    Read straight from the tables rather than through the API, because the point is to catch a value
    written by a code path the API does not serialise.
    """
    from sqlalchemy import select

    for payload, filename in ((DDR_BYTES, "ddr.txt"), (b"plain notes with no table", "notes.txt")):
        response = await fabric.upload(
            fabric.alpha, payload, filename=filename, well_id=fabric.alpha.well_id
        )
        assert response.status_code == 201, response.text

    async with fabric.session() as session:
        document_statuses = set(
            (await session.execute(select(Document.status).distinct())).scalars().all()
        )
        record_states = set(
            (await session.execute(select(ExtractedRecord.validation_state).distinct()))
            .scalars()
            .all()
        )
        job_statuses = set(
            (await session.execute(select(IngestionJob.status).distinct())).scalars().all()
        )

    assert document_statuses <= set(DOC_STATUSES), document_statuses - set(DOC_STATUSES)
    assert record_states <= set(VALIDATION_STATES), record_states - set(VALIDATION_STATES)
    assert job_statuses <= set(INGESTION_JOB_STATUSES), job_statuses - set(INGESTION_JOB_STATUSES)


async def test_duplicate_upload_is_a_job_outcome_not_a_document_state(fabric: Fabric) -> None:
    payload = b"one report, uploaded twice on purpose"
    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    second = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["scope"]["reused_existing"] is True
    assert second.json()["job"]["status"] == "skipped_duplicate"
    # The *first* document's state is unchanged: a re-upload is not an event in its life cycle.
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["document"]["status"] in DOC_STATUSES


async def test_partial_extraction_is_distinguishable_from_success(fabric: Fabric) -> None:
    """A scanned page has no text layer: nothing can be extracted, and the platform says so.

    The status must not be ``extracted`` (that would claim a complete read) and the job must not be
    ``failed`` (the pipeline did exactly what it was asked to). The two answers are different, and both
    are honest.
    """
    scanned = scanned_pdf()
    response = await fabric.upload(
        fabric.alpha, scanned, filename="scan.pdf", content_type="application/pdf",
        well_id=fabric.alpha.well_id,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["document"]["status"] == "partially_extracted"
    assert body["document"]["ocr_required"] is True
    assert body["job"]["status"] == "partial"


def test_no_function_maps_a_job_status_onto_a_document_status() -> None:
    """The mapping would have to be invented, which is exactly why none exists.

    A job can be ``skipped_duplicate`` while the document it declined to duplicate is ``extracted``,
    and both are true at the same time — no conversion can express that, which is why none exists.
    """
    module = (
        pathlib.Path(__file__).resolve().parents[2] / "src" / "drillai" / "documents" / "lifecycle.py"
    ).read_text()
    assert "def job_status" not in module
    # And the one place the pipeline touches both does so by *deciding*, not by mapping: it names the
    # document status from what happened and then names the job status from the document status.
    pipeline = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "drillai"
        / "ingestion"
        / "pipeline.py"
    ).read_text()
    assert 'status="partial" if document.status == "partially_extracted" else "succeeded"' in pipeline
