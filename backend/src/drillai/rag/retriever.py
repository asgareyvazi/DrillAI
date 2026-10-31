"""Retrieval over the document corpus.

Retrieval is *structured first*: every chunk carries its well, section, operation, document type,
depth interval and period (see ``DocumentChunk``), so a question about "the 8½ inch section" is
answered by filtering to that section before ranking — a naive vector index would happily return
an 8½ inch *casing* passage from the 12¼ inch section because the tokens match.

Modes:

``metadata``  filter only (newest first) — deterministic, always available;
``lexical``   BM25-style scoring over tokens: no model, no cost, reproducible;
``semantic``  vector similarity when an embedding provider and stored embeddings exist;
``hybrid``    reciprocal-rank fusion of lexical and semantic results (falls back to lexical when
              embeddings are unavailable, and says so in the result).

Every hit reports *why* it matched (``explain``) and carries its citations, so the answer that a
model builds from it can be traced to a page.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.logging import get_logger
from drillai.db.models import Document, DocumentChunk, RawArtifact
from drillai.documents.access import (
    chunk_document_scope_conditions,
    document_not_quarantined_condition,
)

logger = get_logger(__name__)

__all__ = [
    "RetrievalHit",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrieveMode",
    "ensure_pgvector_schema",
    "expand_neighbours",
    "index_chunk_embeddings",
    "retrieve",
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{1,}")
MAX_RETRIEVAL_LIMIT = 100
MAX_RETRIEVAL_CANDIDATES = 2000
MIN_RETRIEVAL_CANDIDATES = 200
RETRIEVAL_CANDIDATE_MULTIPLIER = 20
MAX_NEIGHBOUR_RADIUS = 2

_STOPWORDS = {
    "the", "and", "for", "was", "were", "with", "that", "this", "from", "have", "has", "had",
    "are", "not", "but", "you", "your", "our", "its", "into", "over", "under", "than", "then",
    "what", "which", "when", "where", "how", "why", "does", "did", "done", "been", "being",
}


class RetrieveMode(enum.StrEnum):
    METADATA = "metadata"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True)
class RetrievalQuery:
    text: str | None = None
    mode: RetrieveMode | str = RetrieveMode.HYBRID
    limit: int = 10
    org_id: str | None = None
    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    formation_id: str | None = None
    doc_type: str | None = None
    chunk_kind: str | None = None
    depth_from_si: float | None = None
    depth_to_si: float | None = None
    period_from: Any = None
    period_to: Any = None
    min_score: float = 0.0
    include_neighbours: int = 0


@dataclass
class RetrievalHit:
    chunk_id: str
    document_id: str
    text: str
    score: float
    page_number: int | None
    section_id: str | None
    well_id: str | None
    doc_type: str | None
    depth_from_si: float | None
    depth_to_si: float | None
    period_start: Any
    period_end: Any
    org_id: str = field(repr=False)
    wellbore_id: str | None = None
    operation_id: str | None = None
    formation_id: str | None = None
    service_category: str | None = None
    chunk_kind: str | None = None
    region_id: str | None = None
    source_locator: dict[str, Any] = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)
    explain: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, mode: str = "json") -> dict[str, Any]:  # parity with pydantic models
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "text": self.text,
            "score": self.score,
            "page_number": self.page_number,
            "region_id": self.region_id,
            "source_locator": self.source_locator,
            "section_id": self.section_id,
            "well_id": self.well_id,
            "wellbore_id": self.wellbore_id,
            "operation_id": self.operation_id,
            "formation_id": self.formation_id,
            "service_category": self.service_category,
            "chunk_kind": self.chunk_kind,
            "doc_type": self.doc_type,
            "depth_from_si": self.depth_from_si,
            "depth_to_si": self.depth_to_si,
            "period_start": self.period_start.isoformat() if hasattr(self.period_start, "isoformat") else self.period_start,
            "period_end": self.period_end.isoformat() if hasattr(self.period_end, "isoformat") else self.period_end,
            "citations": self.citations,
            "explain": self.explain,
        }


@dataclass
class RetrievalResult:
    hits: list[RetrievalHit]
    mode_used: str
    notes: list[str] = field(default_factory=list)
    filters_applied: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {
            "hits": [hit.model_dump() for hit in self.hits],
            "mode_used": self.mode_used,
            "notes": self.notes,
            "filters_applied": self.filters_applied,
        }


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in _STOPWORDS]


def _validate_query_scope(org_id: str, query: RetrievalQuery) -> None:
    if not isinstance(org_id, str) or not org_id.strip():
        raise ValueError("authenticated organization scope must be a non-empty identifier")
    if query.org_id is not None and query.org_id != org_id:
        raise ValueError("query.org_id must match the authenticated organization")
    if not query.well_id and not query.project_id:
        raise ValueError("retrieval must be scoped to a well or a project")
    for field_name in (
        "project_id",
        "well_id",
        "wellbore_id",
        "section_id",
        "operation_id",
        "formation_id",
        "doc_type",
        "chunk_kind",
    ):
        value = getattr(query, field_name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{field_name} must be a non-empty identifier when provided")
    for field_name in ("depth_from_si", "depth_to_si"):
        value = getattr(query, field_name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        ):
            raise ValueError(f"{field_name} must be a finite number")
    if (
        query.depth_from_si is not None
        and query.depth_to_si is not None
        and query.depth_from_si > query.depth_to_si
    ):
        raise ValueError("depth_from_si must not exceed depth_to_si")
    for field_name in ("period_from", "period_to"):
        value = getattr(query, field_name)
        if value is not None and not isinstance(value, dt.datetime):
            raise ValueError(f"{field_name} must be a datetime when provided")
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{field_name} must be timezone-aware")
    if query.period_from is not None and query.period_to is not None:
        try:
            period_reversed = query.period_from > query.period_to
        except TypeError as exc:
            raise ValueError("period_from and period_to must be mutually comparable") from exc
        if period_reversed:
            raise ValueError("period_from must not follow period_to")


def _window_scope_conditions(query: RetrievalQuery):
    """Require positive interval metadata whenever a depth or time window is requested.

    Unknown depth/time is not proof that a chunk belongs in a narrowed window. A single endpoint
    is treated as a point; completely untagged chunks fail closed instead of leaking into a scoped
    answer merely because their metadata is absent.
    """
    conditions = []
    if query.depth_from_si is not None:
        depth_end = func.coalesce(DocumentChunk.depth_to_si, DocumentChunk.depth_from_si)
        conditions.append(and_(depth_end.is_not(None), depth_end >= query.depth_from_si))
    if query.depth_to_si is not None:
        depth_start = func.coalesce(DocumentChunk.depth_from_si, DocumentChunk.depth_to_si)
        conditions.append(and_(depth_start.is_not(None), depth_start <= query.depth_to_si))
    if query.period_from is not None:
        period_end = func.coalesce(DocumentChunk.period_end, DocumentChunk.period_start)
        conditions.append(and_(period_end.is_not(None), period_end >= query.period_from))
    if query.period_to is not None:
        period_start = func.coalesce(DocumentChunk.period_start, DocumentChunk.period_end)
        conditions.append(and_(period_start.is_not(None), period_start <= query.period_to))
    return conditions


def _bm25_scores(query_tokens: list[str], documents: list[list[str]], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Small, dependency-free BM25 — deterministic and auditable."""
    if not documents:
        return []
    doc_count = len(documents)
    avg_length = sum(len(doc) for doc in documents) / doc_count or 1.0
    document_frequency: Counter[str] = Counter()
    for document in documents:
        for token in set(document):
            document_frequency[token] += 1
    scores: list[float] = []
    for document in documents:
        score = 0.0
        counts = Counter(document)
        for token in query_tokens:
            if token not in counts:
                continue
            df = document_frequency[token] or 1
            idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
            tf = counts[token]
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(document) / avg_length))
        scores.append(score)
    return scores


async def retrieve(session: AsyncSession, org_id: str, query: RetrievalQuery) -> RetrievalResult:
    """Fetch chunks matching the *filters*, then rank them."""
    mode = RetrieveMode(query.mode)
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_RETRIEVAL_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_RETRIEVAL_LIMIT}")
    if (
        isinstance(query.include_neighbours, bool)
        or not isinstance(query.include_neighbours, int)
        or not 0 <= query.include_neighbours <= MAX_NEIGHBOUR_RADIUS
    ):
        raise ValueError(f"include_neighbours must be an integer between 0 and {MAX_NEIGHBOUR_RADIUS}")
    if (
        isinstance(query.min_score, bool)
        or not isinstance(query.min_score, (int, float))
        or not math.isfinite(query.min_score)
    ):
        raise ValueError("min_score must be a finite number")
    _validate_query_scope(org_id, query)

    stmt = (
        select(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
        .where(
            DocumentChunk.org_id == org_id,
            Document.org_id == org_id,
            document_not_quarantined_condition(),
            *chunk_document_scope_conditions(),
        )
    )
    if query.well_id:
        stmt = stmt.where(DocumentChunk.well_id == query.well_id)
    if query.project_id:
        stmt = stmt.where(Document.project_id == query.project_id)
    if query.wellbore_id:
        stmt = stmt.where(DocumentChunk.wellbore_id == query.wellbore_id)
    if query.section_id:
        stmt = stmt.where(DocumentChunk.section_id == query.section_id)
    if query.operation_id:
        stmt = stmt.where(DocumentChunk.operation_id == query.operation_id)
    if query.formation_id:
        stmt = stmt.where(DocumentChunk.formation_id == query.formation_id)
    if query.doc_type:
        stmt = stmt.where(DocumentChunk.doc_type == query.doc_type)
    if query.chunk_kind:
        stmt = stmt.where(DocumentChunk.kind == query.chunk_kind)
    stmt = stmt.where(*_window_scope_conditions(query))

    # Keep CPU, memory, and embedding-provider work bounded while making the recency shortlist visible
    # in the result whenever it actually truncates the eligible corpus.
    candidate_limit = min(
        MAX_RETRIEVAL_CANDIDATES,
        max(MIN_RETRIEVAL_CANDIDATES, query.limit * RETRIEVAL_CANDIDATE_MULTIPLIER),
    )
    candidate_rows = list(
        (
            await session.execute(
                stmt.order_by(DocumentChunk.created_at.desc(), DocumentChunk.id.asc()).limit(candidate_limit + 1)
            )
        )
        .scalars()
        .all()
    )
    candidate_window_truncated = len(candidate_rows) > candidate_limit
    chunks = candidate_rows[:candidate_limit]
    filters = {
        key: value
        for key, value in {
            "org_id": org_id,
            "well_id": query.well_id,
            "project_id": query.project_id,
            "wellbore_id": query.wellbore_id,
            "section_id": query.section_id,
            "operation_id": query.operation_id,
            "formation_id": query.formation_id,
            "doc_type": query.doc_type,
            "chunk_kind": query.chunk_kind,
            "depth_from_si": query.depth_from_si,
            "depth_to_si": query.depth_to_si,
            "period_from": query.period_from,
            "period_to": query.period_to,
        }.items()
        if value is not None
    }
    if query.include_neighbours:
        filters["include_neighbours"] = query.include_neighbours
    notes: list[str] = []
    if not chunks:
        return RetrievalResult(hits=[], mode_used=str(mode), notes=["no chunks matched the filters"], filters_applied=filters)
    if candidate_window_truncated:
        notes.append(
            f"ranking was bounded to the newest {candidate_limit} matching chunks; older matching chunks may be omitted"
        )

    query_tokens = _tokens(query.text or "")
    # Keep a lexical ranking available even for an explicitly semantic request: if its
    # embedding provider/model is unavailable, the fallback must still be usable.
    lexical_scores = _bm25_scores(query_tokens, [_tokens(chunk.text) for chunk in chunks]) if query_tokens else []
    semantic_scores: list[float] = []
    semantic_chunk_ids: set[str] = set()
    vector_backend: str | None = None
    mode_used = str(mode)
    if mode is RetrieveMode.SEMANTIC or (mode is RetrieveMode.HYBRID and query.text):
        semantic_scores, vector_backend, semantic_chunk_ids = await _semantic_scores_with_backend(
            session, chunks, query.text or ""
        )
        if not semantic_scores:
            mode_used = "lexical" if query_tokens else "metadata"
            if vector_backend == "stale_embedding":
                reason = "vector search unavailable because stored embeddings do not match the current chunk text fingerprint"
            elif vector_backend in {"incompatible_model", "incompatible_dimension"}:
                reason = "vector search unavailable because stored embeddings do not match the active embedding model and dimension"
            elif vector_backend == "no_embedding_provider":
                reason = "vector search unavailable because no embedding provider is configured"
            elif vector_backend == "no_stored_embeddings":
                reason = "vector search unavailable because no stored chunk embeddings are available"
            elif vector_backend == "embedding_method_unavailable":
                reason = "vector search unavailable because the configured provider has no embedding method"
            elif vector_backend == "query_embedding_failed":
                reason = "vector search unavailable because the provider could not encode the query"
            elif vector_backend == "invalid_query_embedding":
                reason = "vector search unavailable because the provider returned no valid query vector"
            else:
                reason = "vector search unavailable for an unclassified embedding failure"
            if query_tokens:
                notes.append(f"{reason}; fell back to lexical scoring")
            else:
                notes.append(f"{reason}; no searchable lexical tokens, returned newest matching chunks")
        elif vector_backend == "pgvector":
            notes.append("vector search executed via postgresql pgvector (<=> cosine distance)")
        elif vector_backend == "python_cosine":
            notes.append("vector search executed via portable cosine similarity")

    if not query_tokens and not semantic_scores and mode is not RetrieveMode.METADATA:
        mode_used = "metadata"
        if not any("no searchable lexical tokens" in note for note in notes):
            notes.append("no searchable lexical tokens or usable vectors; returned newest matching chunks")
    elif not query_tokens and semantic_scores:
        # A hybrid request with no lexical terms is semantic-only; report the ranking actually used.
        mode_used = "semantic"

    ranked: list[tuple[DocumentChunk, float, dict[str, Any]]] = []
    semantic_chunks = [chunk for chunk in chunks if chunk.id in semantic_chunk_ids]
    semantic_by_id = {
        chunk.id: (score, rank)
        for chunk, score, rank in zip(
            semantic_chunks,
            semantic_scores,
            _ranks(semantic_scores),
            strict=True,
        )
    }
    if mode_used == "metadata" or (not query_tokens and not semantic_scores):
        ranked = [(chunk, 1.0, {"reason": "metadata filter only, newest first"}) for chunk in chunks]
    elif mode_used == "semantic" and semantic_scores:
        ranked = [
            (
                chunk,
                semantic_by_id[chunk.id][0],
                {
                    "semantic": round(semantic_by_id[chunk.id][0], 6),
                    "vector_backend": vector_backend or "python_cosine",
                },
            )
            for chunk in semantic_chunks
        ]
    elif query_tokens and semantic_scores:
        # Reciprocal-rank fusion: robust, scale-free, no score normalisation guesswork. Chunks
        # embedded by another model contribute only their lexical rank, never a fake zero-vector
        # semantic position.
        lexical_ranks = _ranks(lexical_scores)
        for index, chunk in enumerate(chunks):
            lexical_score = lexical_scores[index]
            lexical_rank = lexical_ranks[index]
            semantic_entry = semantic_by_id.get(chunk.id)
            score = 0.0
            explain: dict[str, Any] = {"vector_backend": vector_backend}
            if lexical_score > 0:
                score += 1.0 / (60 + lexical_rank)
                explain["lexical_rank"] = lexical_rank
            if semantic_entry is not None:
                semantic_score, semantic_rank = semantic_entry
                score += 1.0 / (60 + semantic_rank)
                explain.update(
                    {
                        "semantic_rank": semantic_rank,
                        "semantic_score": round(semantic_score, 6),
                    }
                )
            if score <= 0:
                continue
            explain["rrf"] = round(score, 6)
            ranked.append((chunk, score, explain))
    else:
        ranked = [
            (chunk, score, {"bm25": round(score, 6)})
            for chunk, score in zip(chunks, lexical_scores, strict=True)
            if score > 0
        ]
        if not ranked and query_tokens and mode_used == "lexical":
            notes.append("lexical search found no token matches; returned no hits")
    if mode_used == "metadata":
        # ``chunks`` already arrive in stable created_at DESC order; a stable score sort preserves the
        # documented newest-first tie order instead of accidentally grouping across documents by index.
        ranked.sort(key=lambda item: -item[1])
    else:
        ranked.sort(key=lambda item: (-item[1], item[0].chunk_index, item[0].id))
    top = [item for item in ranked if item[1] >= query.min_score][: query.limit]
    hits = [
        RetrievalHit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            text=chunk.text,
            score=round(score, 6),
            page_number=chunk.page_number,
            section_id=chunk.section_id,
            well_id=chunk.well_id,
            doc_type=chunk.doc_type,
            depth_from_si=chunk.depth_from_si,
            depth_to_si=chunk.depth_to_si,
            period_start=chunk.period_start,
            period_end=chunk.period_end,
            org_id=chunk.org_id,
            wellbore_id=chunk.wellbore_id,
            operation_id=chunk.operation_id,
            formation_id=chunk.formation_id,
            service_category=chunk.service_category,
            chunk_kind=chunk.kind,
            region_id=chunk.region_id,
            source_locator=(chunk.attributes or {}).get("source_locator", {}),
            citations=list(
                dict.fromkeys(
                    value for value in (chunk.document_id, chunk.region_id, chunk.id) if value
                )
            ),
            explain=explain,
        )
        for chunk, score, explain in top
    ]
    if query.include_neighbours:
        expanded_hits = await expand_neighbours(
            session,
            hits,
            radius=query.include_neighbours,
            org_id=org_id,
            query=query,
        )
        added_count = len(expanded_hits) - len(hits)
        if added_count > 0:
            notes.append(f"included {added_count} adjacent chunk(s) inside the same filtered scope")
        hits = expanded_hits
    return RetrievalResult(hits=hits, mode_used=mode_used, notes=notes, filters_applied=filters)


def _ranks(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda index: -scores[index])
    ranks = [0] * len(scores)
    for position, index in enumerate(order, start=1):
        ranks[index] = position
    return ranks


def _is_postgres(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bool(bind is not None and bind.dialect.name == "postgresql")


def _format_vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in vec) + "]"


def _embedding_model_name(provider: Any) -> str | None:
    model_name = getattr(provider, "model_name", None)
    if model_name:
        return str(model_name)
    from drillai.ai.providers import LLMCapability

    return next(
        (
            spec.model
            for spec in provider.models()
            if LLMCapability.EMBEDDING in spec.capabilities
        ),
        None,
    )


def _embedding_space_id(provider: Any, model_name: str | None = None) -> str | None:
    """Identify a vector space by provider, implementation, endpoint and exact model name.

    The opaque versioned digest avoids storing private endpoint identifiers while preventing two
    providers that happen to call their models by the same name from sharing a vector space.
    """
    model_name = model_name or _embedding_model_name(provider)
    if not model_name:
        return None
    provider_name = str(getattr(provider, "name", "provider"))
    provider_type = f"{type(provider).__module__}.{type(provider).__qualname__}"
    endpoint = str(getattr(provider, "base_url", ""))
    identity = "\\0".join((provider_name, provider_type, endpoint, model_name))
    return "space-v1:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


async def ensure_pgvector_schema(session: AsyncSession) -> bool:
    """Ensure the PostgreSQL `vector` extension and `document_chunk_vectors` index table exist."""
    if not _is_postgres(session):
        return False
    await session.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
    await session.execute(
        sql_text(
            """
            CREATE TABLE IF NOT EXISTS document_chunk_vectors (
                chunk_id VARCHAR(64) PRIMARY KEY REFERENCES document_chunks(id) ON DELETE CASCADE,
                org_id VARCHAR(64) NOT NULL,
                well_id VARCHAR(64),
                section_id VARCHAR(64),
                embedding_model VARCHAR(120) NOT NULL,
                embedding_dim INTEGER NOT NULL,
                embedding vector NOT NULL
            )
            """
        )
    )
    await session.execute(
        sql_text(
            """
            CREATE INDEX IF NOT EXISTS ix_document_chunk_vectors_scope
            ON document_chunk_vectors (org_id, well_id, section_id)
            """
        )
    )
    return True


async def _sync_pgvector_rows(session: AsyncSession, chunks: list[DocumentChunk]) -> bool:
    """Upsert any chunk embeddings into `document_chunk_vectors` as native PostgreSQL `vector` rows."""
    if not _is_postgres(session):
        return False
    try:
        # A missing extension, unsupported vector type, or permission failure must not poison the
        # caller's transaction; portable cosine is still a valid fallback after this savepoint rolls back.
        async with session.begin_nested():
            await ensure_pgvector_schema(session)
            for chunk in chunks:
                if not chunk.embedding:
                    continue
                vec_lit = _format_vector_literal(chunk.embedding)
                dim = len(chunk.embedding)
                model_name = chunk.embedding_model or "default"
                await session.execute(
                    sql_text(
                        """
                        INSERT INTO document_chunk_vectors (
                            chunk_id, org_id, well_id, section_id, embedding_model, embedding_dim, embedding
                        )
                        VALUES (
                            :chunk_id, :org_id, :well_id, :section_id, :model_name, :dim, CAST(:vec_lit AS vector)
                        )
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            embedding_model = EXCLUDED.embedding_model,
                            embedding_dim = EXCLUDED.embedding_dim,
                            embedding = EXCLUDED.embedding
                        """
                    ),
                    {
                        "chunk_id": chunk.id,
                        "org_id": chunk.org_id,
                        "well_id": chunk.well_id,
                        "section_id": chunk.section_id,
                        "model_name": model_name,
                        "dim": dim,
                        "vec_lit": vec_lit,
                    },
                )
            await session.flush()
        return True
    except Exception as exc:
        logger.warning(
            "pgvector sync failed",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )
        return False


async def index_chunk_embeddings(
    session: AsyncSession,
    chunks: list[DocumentChunk],
    *,
    provider: Any = None,
) -> int:
    """Generate and persist embeddings for `chunks` (and sync to `document_chunk_vectors` on PostgreSQL)."""
    from drillai.ai.providers import LLMCapability, provider_registry

    if not chunks:
        return 0
    if provider is None:
        reg = provider_registry()
        provider = next(
            (item for item in reg.providers().values() if item.supports(LLMCapability.EMBEDDING)),
            None,
        )
    if provider is None or getattr(provider, "embed", None) is None:
        return 0
    space_id = _embedding_space_id(provider)
    if space_id is None:
        return 0
    texts = [c.text for c in chunks]
    vectors = await provider.embed(texts)
    if len(vectors) != len(chunks):
        raise ValueError("embedding provider returned a different number of vectors than input chunks")
    count = 0
    for chunk, vec in zip(chunks, vectors, strict=True):
        try:
            normalized_vector = [float(value) for value in vec]
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding provider returned a non-numeric vector") from exc
        if not normalized_vector or any(not math.isfinite(value) for value in normalized_vector):
            raise ValueError("embedding provider returned an empty or non-finite vector")
        chunk.embedding = normalized_vector
        chunk.embedding_dim = len(normalized_vector)
        chunk.embedding_model = space_id
        chunk.embedding_content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        count += 1
    await session.flush()
    await _sync_pgvector_rows(session, chunks)
    return count


async def _pgvector_semantic_scores(
    session: AsyncSession,
    chunks: list[DocumentChunk],
    query_vector: list[float],
    model_name: str,
) -> list[float] | None:
    """Execute cosine similarity `1 - (embedding <=> :query_vec::vector)` in PostgreSQL pgvector."""
    if not _is_postgres(session) or not chunks or not query_vector:
        return None
    synced = await _sync_pgvector_rows(session, chunks)
    if not synced:
        return None
    q_lit = _format_vector_literal(query_vector)
    dim = len(query_vector)
    org_id = chunks[0].org_id
    chunk_ids = [c.id for c in chunks]
    try:
        async with session.begin_nested():
            rows = (
                await session.execute(
                    sql_text(
                        """
                        SELECT chunk_id, 1.0 - (embedding <=> CAST(:q_lit AS vector)) AS cosine_sim
                        FROM document_chunk_vectors
                        WHERE org_id = :org_id
                          AND embedding_model = :model_name
                          AND embedding_dim = :dim
                          AND chunk_id = ANY(:chunk_ids)
                        """
                    ),
                    {
                        "q_lit": q_lit,
                        "org_id": org_id,
                        "model_name": model_name,
                        "dim": dim,
                        "chunk_ids": chunk_ids,
                    },
                )
            ).all()
    except Exception as exc:
        logger.warning(
            "pgvector query failed",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )
        return None
    score_by_id = {str(r[0]): float(r[1]) for r in rows if r[1] is not None}
    if not score_by_id:
        return None
    return [score_by_id.get(c.id, 0.0) for c in chunks]


async def _semantic_scores_with_backend(
    session: AsyncSession,
    chunks: list[DocumentChunk],
    text: str,
) -> tuple[list[float], str | None, set[str]]:
    """Score only vectors made by the exact active model and with its exact dimension."""
    from drillai.ai.providers import LLMCapability, provider_registry

    registry = provider_registry()
    provider = next(
        (item for item in registry.providers().values() if item.supports(LLMCapability.EMBEDDING)),
        None,
    )
    if provider is None:
        return [], "no_embedding_provider", set()
    with_vectors = [chunk for chunk in chunks if chunk.embedding is not None]
    if not with_vectors:
        return [], "no_stored_embeddings", set()
    embed = getattr(provider, "embed", None)
    if not callable(embed):
        return [], "embedding_method_unavailable", set()

    model_name = _embedding_model_name(provider)
    space_id = _embedding_space_id(provider, model_name)
    if model_name is None or space_id is None:
        return [], "incompatible_model", set()
    model_space_chunks = [chunk for chunk in with_vectors if chunk.embedding_model == space_id]
    if not model_space_chunks:
        return [], "incompatible_model", set()
    model_chunks = [
        chunk
        for chunk in model_space_chunks
        if chunk.embedding_content_hash
        == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    ]
    if not model_chunks:
        return [], "stale_embedding", set()

    try:
        vectors = await embed([text])
    except Exception as exc:
        # Provider exceptions may contain endpoint or response details; classify, do not log them.
        logger.warning(
            "embedding failed",
            extra={"extra_fields": {"provider": provider.name, "error_type": type(exc).__name__}},
        )
        return [], "query_embedding_failed", set()
    if not vectors or len(vectors) != 1:
        return [], "invalid_query_embedding", set()
    try:
        query_vector = [float(value) for value in vectors[0]]
    except (TypeError, ValueError):
        return [], "invalid_query_embedding", set()
    query_norm = math.hypot(*query_vector) if query_vector else 0.0
    if (
        not query_vector
        or any(not math.isfinite(value) for value in query_vector)
        or not math.isfinite(query_norm)
        or query_norm == 0
    ):
        return [], "invalid_query_embedding", set()

    compatible: list[DocumentChunk] = []
    vectors_by_id: dict[str, list[float]] = {}
    for chunk in model_chunks:
        if chunk.embedding_dim != len(query_vector) or not isinstance(chunk.embedding, list):
            continue
        try:
            stored_vector = [float(value) for value in chunk.embedding]
        except (TypeError, ValueError):
            continue
        stored_norm = math.hypot(*stored_vector) if stored_vector else 0.0
        if (
            len(stored_vector) != len(query_vector)
            or any(not math.isfinite(value) for value in stored_vector)
            or not math.isfinite(stored_norm)
            or stored_norm == 0
        ):
            continue
        compatible.append(chunk)
        vectors_by_id[chunk.id] = stored_vector
    if not compatible:
        return [], "incompatible_dimension", set()

    pg_scores = await _pgvector_semantic_scores(session, compatible, query_vector, space_id)
    if pg_scores is not None:
        return pg_scores, "pgvector", {chunk.id for chunk in compatible}
    scores = [_cosine(query_vector, vectors_by_id[chunk.id]) for chunk in compatible]
    return scores, "python_cosine", {chunk.id for chunk in compatible}


async def _semantic_scores(
    session: AsyncSession, chunks: list[DocumentChunk], text: str
) -> list[float]:
    scores, _backend, _compatible_ids = await _semantic_scores_with_backend(session, chunks, text)
    return scores


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    norm_left = math.hypot(*left)
    norm_right = math.hypot(*right)
    if (
        norm_left == 0
        or norm_right == 0
        or not math.isfinite(norm_left)
        or not math.isfinite(norm_right)
    ):
        return 0.0
    # Divide before multiplying so large but finite provider values cannot overflow the dot product.
    return math.fsum((a / norm_left) * (b / norm_right) for a, b in zip(left, right, strict=True))


async def expand_neighbours(
    session: AsyncSession,
    hits: list[RetrievalHit],
    radius: int = 1,
    *,
    org_id: str,
    query: RetrievalQuery,
) -> list[RetrievalHit]:
    """Add adjacent chunks only inside the hit's tenant, identity scope, and query filters.

    The query is required: a hit alone does not retain the depth/period filters that produced it, so
    expanding without the original query would silently weaken those boundaries.
    """
    if isinstance(radius, bool) or not isinstance(radius, int) or not 0 <= radius <= MAX_NEIGHBOUR_RADIUS:
        raise ValueError(f"radius must be an integer between 0 and {MAX_NEIGHBOUR_RADIUS}")
    _validate_query_scope(org_id, query)
    if not hits:
        return hits
    document_ids = {hit.document_id for hit in hits if hit.org_id == org_id}
    if not document_ids:
        return []
    stmt = (
        select(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
        .where(
            Document.org_id == org_id,
            DocumentChunk.org_id == org_id,
            DocumentChunk.document_id.in_(document_ids),
            document_not_quarantined_condition(),
            *chunk_document_scope_conditions(),
        )
    )
    if query.project_id:
        stmt = stmt.where(Document.project_id == query.project_id)
    for field_name in (
        "well_id",
        "wellbore_id",
        "section_id",
        "operation_id",
        "formation_id",
        "doc_type",
    ):
        value = getattr(query, field_name)
        if value is not None:
            stmt = stmt.where(getattr(DocumentChunk, field_name) == value)
    if query.chunk_kind:
        stmt = stmt.where(DocumentChunk.kind == query.chunk_kind)
    stmt = stmt.where(*_window_scope_conditions(query))

    def same_scope(hit: RetrievalHit, row: DocumentChunk) -> bool:
        return (
            hit.org_id == org_id
            and row.org_id == org_id
            and hit.document_id == row.document_id
            and hit.well_id == row.well_id
            and hit.wellbore_id == row.wellbore_id
            and hit.section_id == row.section_id
            and hit.operation_id == row.operation_id
            and hit.formation_id == row.formation_id
            and hit.service_category == row.service_category
            and hit.chunk_kind == row.kind
            and hit.doc_type == row.doc_type
        )

    hit_ids = {hit.chunk_id for hit in hits if hit.org_id == org_id}
    source_rows = (
        await session.execute(stmt.where(DocumentChunk.id.in_(hit_ids)))
    ).scalars().all()
    source_by_id = {row.id: row for row in source_rows}
    authorized_hits = [
        hit
        for hit in hits
        if (source := source_by_id.get(hit.chunk_id)) is not None and same_scope(hit, source)
    ]
    if radius == 0 or not authorized_hits:
        return authorized_hits

    # Resolve each hit to a verified database row first, then fetch only the index windows around those
    # rows. Loading every chunk in a long document just to return at most four neighbours per hit was
    # an unbounded read disguised as a small result limit.
    windows = [
        and_(
            DocumentChunk.document_id == source_by_id[hit.chunk_id].document_id,
            DocumentChunk.chunk_index >= max(0, source_by_id[hit.chunk_id].chunk_index - radius),
            DocumentChunk.chunk_index <= source_by_id[hit.chunk_id].chunk_index + radius,
        )
        for hit in authorized_hits
    ]
    neighbour_rows = (
        await session.execute(
            stmt.where(or_(*windows)).order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        )
    ).scalars().all()
    by_document: dict[str, list[DocumentChunk]] = {}
    for row in neighbour_rows:
        by_document.setdefault(row.document_id, []).append(row)

    extra: list[RetrievalHit] = []
    seen = {hit.chunk_id for hit in authorized_hits}
    for hit in authorized_hits:
        source = source_by_id[hit.chunk_id]
        for neighbour in by_document.get(hit.document_id, []):
            if (
                neighbour.id == hit.chunk_id
                or neighbour.id in seen
                or not same_scope(hit, neighbour)
                or abs(neighbour.chunk_index - source.chunk_index) > radius
            ):
                continue
            seen.add(neighbour.id)
            extra.append(
                RetrievalHit(
                    chunk_id=neighbour.id,
                    document_id=neighbour.document_id,
                    text=neighbour.text,
                    score=round(hit.score * 0.5, 6),
                    page_number=neighbour.page_number,
                    section_id=neighbour.section_id,
                    well_id=neighbour.well_id,
                    doc_type=neighbour.doc_type,
                    depth_from_si=neighbour.depth_from_si,
                    depth_to_si=neighbour.depth_to_si,
                    period_start=neighbour.period_start,
                    period_end=neighbour.period_end,
                    org_id=neighbour.org_id,
                    wellbore_id=neighbour.wellbore_id,
                    operation_id=neighbour.operation_id,
                    formation_id=neighbour.formation_id,
                    service_category=neighbour.service_category,
                    chunk_kind=neighbour.kind,
                    region_id=neighbour.region_id,
                    source_locator=(neighbour.attributes or {}).get("source_locator", {}),
                    citations=list(
                        dict.fromkeys(
                            value
                            for value in (neighbour.document_id, neighbour.region_id, neighbour.id)
                            if value
                        )
                    ),
                    explain={"reason": f"neighbour of {hit.chunk_id}", "parent_score": hit.score},
                )
            )
    return [*authorized_hits, *extra]
