"""What makes two uploads the same document — and what makes them different documents.

The defect this replaces
------------------------

Deduplication keyed on the bytes alone. The pipeline hashed the upload, found an existing
``RawArtifact`` with that digest, then looked for *any* document in the organization pointing at it
and returned the first one. The consequence was reproduced against the running application: the same
bytes uploaded for well A and then for well B returned **well A's document** on the second call, with
``201 Created``, and well B was never given a document. A daily report photographed once and filed
against the wrong well cannot be corrected afterwards, because the platform believes it was never
uploaded.

The model that fixes it
-----------------------

Two identities, deliberately separate:

**RawArtifact — the bytes.** Content-addressed by SHA-256, stored once, never mutated. The same PDF
uploaded by six people is one artefact and six logical documents, because the artefact answers "do we
already hold these bytes?" and nothing else.

**Document — the logical engineering record.** A reviewable record of *this report about this scope*.
Its identity is the artefact it came from, the scope it is attached to, its type and its revision.

That gives the four cases the brief asks to be told apart:

=============================================  =================================================
situation                                      outcome
=============================================  =================================================
same bytes, same scope, same revision          **the same document.** No new row; the caller is told
                                               it was a repeat and given the document that exists.
same bytes, *different* scope                  **two documents.** One artefact, two logical records,
                                               each attached to its own well. This is the case the
                                               old code got wrong.
same scope, *different* revision               **two documents.** A revised report is a new record,
                                               and the earlier revision is not overwritten.
different bytes, same document number          **two documents**, because the identity is the bytes
                                               and the scope, not a number printed on the cover. The
                                               platform links them by ``document_number``; it does not
                                               decide that a re-scan is a revision. Making that call
                                               would mean overwriting a record because two PDFs share a
                                               string in a header.
=============================================  =================================================

The revision is part of the identity and the number is not, which is the opposite of what a
document-control system does — deliberately. A document-control system owns the identifier it prints;
here the identifier arrives on the paper and the platform has no authority to decide that two files
are the same report just because they claim the same number.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.db.models import Document

#: Scope precedence, from most specific to least. A section is more specific than the wellbore it sits
#: in, which is more specific than the well, which is more specific than the project. Two uploads aimed
#: at the same place are the same logical document even if one caller named the well and the other
#: named the wellbore, because the scope *resolves* to the same hole.
#:
#: ``section_id`` was missing from this list at first, and the omission had the same shape as the
#: artefact-keyed defect it was written to fix: filing a report against a section and then against its
#: wellbore with the same bytes produced *one* document, so the section-level filing silently became a
#: wellbore-level one. Any scope field the model can store has to be in this tuple, or two different
#: filings collapse.
_SCOPE_PRECEDENCE = ("section_id", "wellbore_id", "well_id", "project_id")


def scope_token(
    *,
    section_id: str | None = None,
    wellbore_id: str | None = None,
    well_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """The most specific thing this document is about, as a string.

    Not a hash of all four: a document attached to a wellbore and one attached to its well describe
    different intents — a report about the hole versus a report about the field — and collapsing them
    would make two genuinely different filings look like a repeat.

    An empty string means "no scope was named", which is a legitimate filing: a report with no home
    yet. It is returned as ``""`` rather than as a word like ``"unscoped"`` so that it can never
    collide with an identifier, and the key's field count keeps the position unambiguous.
    """
    values = {
        "section_id": section_id,
        "wellbore_id": wellbore_id,
        "well_id": well_id,
        "project_id": project_id,
    }
    for field in _SCOPE_PRECEDENCE:
        if values[field]:
            return values[field]
    return ""


def logical_key(
    *,
    content_sha256: str | None,
    section_id: str | None = None,
    wellbore_id: str | None = None,
    well_id: str | None = None,
    project_id: str | None = None,
    doc_type: str | None = None,
    revision: str | None = None,
) -> str | None:
    """The identity of a logical document, or ``None`` when it has no bytes to be identified by.

    The input is the **content digest**, not the artefact's primary key, and the difference is not
    cosmetic. On a first upload there is no artefact row yet, so the only stable name for the bytes is
    the digest; on a repeat upload the row exists and carries an id. Keying on the id would compute
    two different keys for the same file — the first document would be filed under one identity and
    every later copy searched for under another, so deduplication would never fire. That was written
    and caught by ``test_identical_upload_is_deduplicated`` rather than by review.

    Normalising case here rather than at the call sites means a revision recorded as ``"B"`` and one
    recorded as ``"b"`` are the same revision — the platform does not get to decide that a person's
    capitalisation made a new document.
    """
    if not content_sha256:
        return None
    parts = "|".join(
        [
            content_sha256,
            scope_token(
                section_id=section_id,
                wellbore_id=wellbore_id,
                well_id=well_id,
                project_id=project_id,
            ),
            (doc_type or "").strip().lower(),
            (revision or "").strip().lower(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:64]


async def find_logical_document(
    session: AsyncSession,
    *,
    org_id: str,
    logical_key_value: str,
    exclude_document_id: str | None = None,
) -> Document | None:
    """The existing document this key names, if one was already filed.

    Scoped by organization as well as by key: the key already contains the scope, and the scope
    contains identifiers that are only unique within an organization, so an unscoped lookup could
    match across tenants.
    """
    stmt = select(Document).where(
        Document.org_id == org_id,
        Document.logical_key == logical_key_value,
    )
    if exclude_document_id:
        stmt = stmt.where(Document.id != exclude_document_id)
    return (await session.execute(stmt.limit(1))).scalars().first()
