"""The document domain: what a document *is*, what state it is in, and what it may be attached to.

Two things live here that used to live nowhere in particular, which is why they drifted.

``lifecycle`` states the canonical value sets for a document's life cycle and for the validation
state of an extracted record, and the rules between them, so a pipeline step cannot invent a state the
database will later reject or, worse, accept as an unknown string that no reader understands. Verifying
this by hand found three: the pipeline wrote ``parsed`` and ``ingested`` (neither is a document
status), and ``unvalidated`` / ``validated`` for records (neither is a validation state, and
``drilling/state.py`` was *reading* ``validated``). The tuples themselves live with the columns they
constrain, in ``db/models/documents.py``, and are re-exported here — one definition, no second copy to
drift out of step.

``scope`` resolves the asset hierarchy a document may be attached to. It is one module rather than a
block inside the upload endpoint because the same resolution is needed for a reclassification, a
reprocess and any future connector that lands a document — and because the inline version had already
lost two cases: it did not filter ``wellbore`` or ``section`` by organization at all, so one tenant
could attach a document to another tenant's hole, and it never checked that a supplied ``well_id``
agrees with the supplied ``wellbore_id``.
"""

from __future__ import annotations

from drillai.documents.identity import find_logical_document, logical_key, scope_token
from drillai.documents.lifecycle import (
    DOC_STATUSES,
    DOCUMENT_TRANSITIONS,
    INGESTION_JOB_STATUSES,
    VALIDATED_STATES,
    VALIDATION_STATES,
    VALIDATION_TRANSITIONS,
    extraction_completeness,
    plan_document_status,
)
from drillai.documents.quotes import excerpt_occurs_in, normalize_for_match, verify_quote
from drillai.documents.scope import (
    DocumentScope,
    DocumentScopeResolver,
    ResolvedScope,
)

__all__ = [
    "DOCUMENT_TRANSITIONS",
    "DOC_STATUSES",
    "INGESTION_JOB_STATUSES",
    "VALIDATED_STATES",
    "VALIDATION_STATES",
    "VALIDATION_TRANSITIONS",
    "DocumentScope",
    "DocumentScopeResolver",
    "ResolvedScope",
    "excerpt_occurs_in",
    "extraction_completeness",
    "find_logical_document",
    "logical_key",
    "normalize_for_match",
    "plan_document_status",
    "scope_token",
    "verify_quote",
]
