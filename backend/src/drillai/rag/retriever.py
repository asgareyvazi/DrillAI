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

import enum
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.logging import get_logger
from drillai.db.models import Document, DocumentChunk

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
    citations: list[str] = field(default_factory=list)
    explain: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, mode: str = "json") -> dict[str, Any]:  # parity with pydantic models
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "text": self.text,
            "score": self.score,
            "page_number": self.page_number,
            "section_id": self.section_id,
            "well_id": self.well_id,
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
    if query.well_id is None and query.project_id is None:
        # Refuse unbounded corpus scans: retrieval is always scoped to a well or a project.
        raise ValueError("retrieval must be scoped to a well or a project")

    stmt = select(DocumentChunk).where(DocumentChunk.org_id == org_id)
    if query.well_id:
        stmt = stmt.where(DocumentChunk.well_id == query.well_id)
    if query.project_id:
        stmt = stmt.where(
            DocumentChunk.document_id.in_(
                select(Document.id).where(Document.org_id == org_id, Document.project_id == query.project_id)
            )
        )
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
    if query.depth_from_si is not None:
        stmt = stmt.where(
            or_(DocumentChunk.depth_to_si.is_(None), DocumentChunk.depth_to_si >= query.depth_from_si)
        )
    if query.depth_to_si is not None:
        stmt = stmt.where(
            or_(DocumentChunk.depth_from_si.is_(None), DocumentChunk.depth_from_si <= query.depth_to_si)
        )
    # Interval semantics with an explicit fallback: a chunk that only records one end of its
    # period is treated as a point in time, so a report from January is not returned for a
    # February question merely because its period_end was never filled in.
    if query.period_from is not None:
        stmt = stmt.where(
            or_(
                func.coalesce(DocumentChunk.period_end, DocumentChunk.period_start).is_(None),
                func.coalesce(DocumentChunk.period_end, DocumentChunk.period_start) >= query.period_from,
            )
        )
    if query.period_to is not None:
        stmt = stmt.where(
            or_(
                func.coalesce(DocumentChunk.period_start, DocumentChunk.period_end).is_(None),
                func.coalesce(DocumentChunk.period_start, DocumentChunk.period_end) <= query.period_to,
            )
        )

    stmt = stmt.order_by(DocumentChunk.created_at.desc()).limit(max(200, query.limit * 20))
    chunks = list((await session.execute(stmt)).scalars().all())
    filters = {
        key: value
        for key, value in {
            "well_id": query.well_id,
            "project_id": query.project_id,
            "section_id": query.section_id,
            "operation_id": query.operation_id,
            "doc_type": query.doc_type,
            "depth_from_si": query.depth_from_si,
            "depth_to_si": query.depth_to_si,
            "period_from": query.period_from,
            "period_to": query.period_to,
        }.items()
        if value is not None
    }
    notes: list[str] = []
    if not chunks:
        return RetrievalResult(hits=[], mode_used=str(mode), notes=["no chunks matched the filters"], filters_applied=filters)

    query_tokens = _tokens(query.text or "")
    lexical_scores = _bm25_scores(query_tokens, [_tokens(chunk.text) for chunk in chunks]) if query_tokens and mode in {RetrieveMode.LEXICAL, RetrieveMode.HYBRID} else []
    semantic_scores: list[float] = []
    vector_backend: str | None = None
    mode_used = str(mode)
    if mode is RetrieveMode.SEMANTIC or (mode is RetrieveMode.HYBRID and query.text):
        semantic_scores, vector_backend = await _semantic_scores_with_backend(session, chunks, query.text or "")
        if not semantic_scores:
            mode_used = "lexical"  # semantic was unavailable in either mode; degrade, never fake
            notes.append(
                "vector search unavailable (no embedding provider configured or no stored embeddings): "
                "fell back to lexical scoring"
            )
        elif vector_backend == "pgvector":
            notes.append("vector search executed via postgresql pgvector (<=> cosine distance)")
        elif vector_backend == "python_cosine":
            notes.append("vector search executed via portable cosine similarity")

    ranked: list[tuple[DocumentChunk, float, dict[str, Any]]] = []
    if mode_used == "metadata" or (not query_tokens and not semantic_scores):
        ranked = [(chunk, 1.0, {"reason": "metadata filter only, newest first"}) for chunk in chunks]
    elif mode_used == "semantic" and semantic_scores:
        ranked = [
            (chunk, score, {"semantic": round(score, 6), "vector_backend": vector_backend or "python_cosine"})
            for chunk, score in zip(chunks, semantic_scores, strict=True)
        ]
    elif query_tokens and semantic_scores:
        # Reciprocal rank fusion: robust, scale-free, no score normalisation guesswork.
        lexical_rank = _ranks(lexical_scores)
        semantic_rank = _ranks(semantic_scores)
        for index, chunk in enumerate(chunks):
            score = 1.0 / (60 + lexical_rank[index]) + 1.0 / (60 + semantic_rank[index])
            ranked.append(
                (
                    chunk,
                    score,
                    {
                        "rrf": round(score, 6),
                        "lexical_rank": lexical_rank[index],
                        "semantic_rank": semantic_rank[index],
                        "semantic_score": round(semantic_scores[index], 6),
                        "vector_backend": vector_backend or "python_cosine",
                    },
                )
            )
    else:
        ranked = [
            (chunk, score, {"bm25": round(score, 6)})
            for chunk, score in zip(chunks, lexical_scores, strict=True)
        ]

    ranked.sort(key=lambda item: (-item[1], item[0].chunk_index))
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
            citations=[chunk.document_id, chunk.id],
            explain=explain,
        )
        for chunk, score, explain in top
    ]
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
        logger.warning("pgvector sync failed", extra={"extra_fields": {"error": str(exc)}})
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
    texts = [c.text for c in chunks]
    vectors = await provider.embed(texts)
    model_name = getattr(provider, "model_name", None) or (
        provider.models()[0].model if provider.models() else provider.name
    )
    count = 0
    for chunk, vec in zip(chunks, vectors, strict=True):
        chunk.embedding = list(vec)
        chunk.embedding_dim = len(vec)
        chunk.embedding_model = model_name
        chunk.embedding_content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        count += 1
    await session.flush()
    await _sync_pgvector_rows(session, chunks)
    return count


async def _pgvector_semantic_scores(
    session: AsyncSession,
    chunks: list[DocumentChunk],
    query_vector: list[float],
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
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT chunk_id, 1.0 - (embedding <=> CAST(:q_lit AS vector)) AS cosine_sim
                    FROM document_chunk_vectors
                    WHERE org_id = :org_id
                      AND embedding_dim = :dim
                      AND chunk_id = ANY(:chunk_ids)
                    """
                ),
                {"q_lit": q_lit, "org_id": org_id, "dim": dim, "chunk_ids": chunk_ids},
            )
        ).all()
    except Exception as exc:
        logger.warning("pgvector query failed", extra={"extra_fields": {"error": str(exc)}})
        return None
    score_by_id = {str(r[0]): float(r[1]) for r in rows if r[1] is not None}
    if not score_by_id:
        return None
    return [score_by_id.get(c.id, 0.0) for c in chunks]


async def _semantic_scores_with_backend(
    session: AsyncSession,
    chunks: list[DocumentChunk],
    text: str,
) -> tuple[list[float], str | None]:
    """Embedding search returning `(scores, backend_name)` where `backend_name` is `'pgvector'` or `'python_cosine'`."""
    from drillai.ai.providers import LLMCapability, provider_registry

    registry = provider_registry()
    provider = next(
        (item for item in registry.providers().values() if item.supports(LLMCapability.EMBEDDING)),
        None,
    )
    with_vectors = [chunk for chunk in chunks if chunk.embedding is not None]
    if provider is None or not with_vectors:
        return [], None
    embed = getattr(provider, "embed", None)
    if embed is None:
        return [], None
    try:
        vector = await embed([text])
    except Exception as exc:
        logger.warning("embedding failed", extra={"extra_fields": {"provider": provider.name, "error": str(exc)}})
        return [], None
    if not vector:
        return [], None
    query_vector = vector[0]
    pg_scores = await _pgvector_semantic_scores(session, chunks, query_vector)
    if pg_scores is not None:
        return pg_scores, "pgvector"
    scores: list[float] = []
    for chunk in chunks:
        if chunk.embedding is None:
            scores.append(0.0)
            continue
        scores.append(_cosine(query_vector, chunk.embedding))
    return scores, "python_cosine"


async def _semantic_scores(session: AsyncSession, chunks: list[DocumentChunk], text: str) -> list[float]:
    scores, _ = await _semantic_scores_with_backend(session, chunks, text)
    return scores


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


async def expand_neighbours(session: AsyncSession, hits: list[RetrievalHit], radius: int = 1) -> list[RetrievalHit]:
    """Add the chunks immediately around each hit, in document order.

    Neighbours are taken by ``chunk_index`` across the whole document rather than within one page:
    a paragraph that continues onto the next page is exactly the context a reader needs, and the
    page boundary is an artefact of the PDF, not of the meaning. Neighbour hits are scored below
    their parent hit so they never outrank it, and each says which hit it belongs to.
    """
    if radius <= 0 or not hits:
        return hits
    document_ids = {hit.document_id for hit in hits}
    rows = (
        await session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id.in_(document_ids))
            .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        )
    ).scalars().all()
    by_document: dict[str, list[DocumentChunk]] = {}
    for row in rows:
        by_document.setdefault(row.document_id, []).append(row)

    extra: list[RetrievalHit] = []
    seen = {hit.chunk_id for hit in hits}
    for hit in hits:
        siblings = by_document.get(hit.document_id, [])
        position = next((index for index, row in enumerate(siblings) if row.id == hit.chunk_id), None)
        if position is None:
            continue
        window = siblings[max(0, position - radius) : position + radius + 1]
        for neighbour in window:
            if neighbour.id == hit.chunk_id or neighbour.id in seen:
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
                    citations=[neighbour.document_id, neighbour.id],
                    explain={"reason": f"neighbour of {hit.chunk_id}", "parent_score": hit.score},
                )
            )
    return [*hits, *extra]
