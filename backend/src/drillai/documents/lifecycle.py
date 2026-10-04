"""The canonical life cycle of a document and of an extracted record — and the rules that reach them.

Why this module exists
----------------------

``db/models/documents.py`` declared ``DOC_STATUSES`` and ``VALIDATION_STATES`` and enforced neither.
The ingestion pipeline then wrote values from neither set: a document became ``parsing`` while it was
being read, and ``parsed`` or ``ingested`` when it finished, and an extracted record became
``unvalidated``. ``drilling/ddr.py`` wrote ``validated`` onto records, and ``drilling/state.py``
*read* ``validated`` when deciding how to label a value's quality. So the platform held six status
values, of which the defined vocabulary knew four, and one reader depended on a string that was never
in any vocabulary. Every one of those was reproduced against the running application before this
module was written, not inferred from the code.

The fix is not to widen the vocabulary to absorb the drift — that would keep three spellings of
"extraction finished". It is to name the states once, state which transition each pipeline outcome
produces, and repair the stored rows in a migration.

Document status versus job status
---------------------------------

These are two different questions and the model already distinguishes them by table:

* ``Document.status`` — *what state is this engineering record in now?* It is a property of the record
  and survives the run that produced it.
* ``IngestionJob.status`` — *what did the last attempt to process it do?* It is a property of an
  execution.

They are allowed to disagree, and the disagreement is informative: a document can be
``partially_extracted`` because three of four extractors ran, while the job that produced it
``succeeded`` — the job did its work, the record is incomplete. What must never happen is a silent
mapping from a failed execution to a complete document.
"""

from __future__ import annotations

from typing import Any

from drillai.db.models.documents import DOC_STATUSES, INGESTION_JOB_STATUSES, VALIDATION_STATES

__all__ = [
    "DOCUMENT_EXTRACTED_STATUSES",
    "DOCUMENT_TERMINAL_STATUSES",
    "DOCUMENT_TRANSITIONS",
    "DOC_STATUSES",
    "INGESTION_JOB_STATUSES",
    "VALIDATED_STATES",
    "VALIDATION_STATES",
    "VALIDATION_TRANSITIONS",
    "extraction_completeness",
    "plan_document_status",
]

# --------------------------------------------------------------------------- document life cycle

# The vocabulary itself is ``DOC_STATUSES`` in ``db/models/documents.py``: it constrains a column, so
# it is declared with the column, and re-exported here so that "what states exist" and "which moves
# between them are legal" have one import site. ``extracted`` claims every applicable extractor ran and
# reported nothing incomplete; ``partially_extracted`` is the honest state when something did not
# complete — an extractor failed, OCR is still required, or a page yielded nothing. ``failed`` is
# reserved for parsing that did not finish at all: no pages, no records, no evidence.

#: Terminal states: nothing moves out of them, because at least one of them is the record's end.
DOCUMENT_TERMINAL_STATUSES: tuple[str, ...] = ("archived",)

#: Allowed moves. ``extracted`` can fall back to ``partially_extracted`` because a re-ingest of the
#: same document by a stricter parser is a correction, not a regression — but nothing returns to
#: ``uploaded``: a document that has been read cannot be un-read.
DOCUMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"extracting", "failed"}),
    "extracting": frozenset({"extracted", "partially_extracted", "failed"}),
    "extracted": frozenset({"validated", "partially_extracted", "archived", "extracting"}),
    "partially_extracted": frozenset({"extracted", "validated", "failed", "archived", "extracting"}),
    "failed": frozenset({"extracting", "archived"}),
    "validated": frozenset({"partially_extracted", "archived"}),
    "archived": frozenset(),
}

#: The states that mean "the machine has finished reading this".
DOCUMENT_EXTRACTED_STATUSES: tuple[str, ...] = (
    "extracted",
    "partially_extracted",
    "validated",
)

# --------------------------------------------------------------------------- record validation

#: What the rest of the platform means by "this value has been checked by something other than the
#: extractor". This replaces the ad-hoc ``!= "validated"`` comparison in ``drilling/state.py``, which
#: tested against a string that never existed in the vocabulary.
VALIDATED_STATES: frozenset[str] = frozenset({"rule_validated", "human_validated"})

VALIDATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "extracted": frozenset({"rule_validated", "human_validated", "rejected", "superseded"}),
    "rule_validated": frozenset({"human_validated", "rejected", "superseded"}),
    "human_validated": frozenset({"rejected", "superseded"}),
    "rejected": frozenset({"superseded"}),
    "superseded": frozenset(),
}


def plan_document_status(
    *,
    parsed: bool,
    has_records: bool,
    has_chunks: bool,
    warnings: list[str] | None = None,
    extractor_failures: int = 0,
    ocr_required: bool = False,
) -> str:
    """The document status a finished ingestion *is*, from what actually happened.

    The rule, in the order it is applied:

    1. parsing did not produce a document at all → ``failed``;
    2. anything incomplete — an extractor that raised, OCR still outstanding, a parser warning, or no
       usable content at all — → ``partially_extracted``;
    3. otherwise → ``extracted``.

    Step 2 is deliberately broad. "Complete" is a claim about the whole document, and the only
    evidence for it is that nothing was reported as incomplete. A pipeline that reports ``extracted``
    while holding a warning teaches every consumer to distrust the field, which is worse than an
    extra ``partially_extracted``.
    """
    if not parsed:
        return "failed"
    reported = list(warnings or [])
    if extractor_failures > 0 or ocr_required or reported:
        return "partially_extracted"
    if not has_records and not has_chunks:
        # Read successfully, produced nothing usable. That is not a failure of parsing and not a
        # complete extraction either.
        return "partially_extracted"
    return "extracted"


def extraction_completeness(
    *,
    extracted: int,
    total: int,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """A reconciliation block every extraction response can carry.

    Counts that do not add up are how a partially-extracted document comes to look complete: a caller
    sees "12 records" and never learns that a fourth extractor raised. The numbers here are meant to
    be checkable — ``promoted + needs_review + not_promoted == candidates`` — rather than decorative.
    """
    return {
        "extracted": extracted,
        "total_candidates": total,
        "warning_count": len(warnings or []),
        "complete": extracted == total and not warnings,
    }
