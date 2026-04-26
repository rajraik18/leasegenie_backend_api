"""Vector store service.

Backed by pgvector — embeddings live in the `clause_embeddings` table of
the same Postgres database the rest of the app uses. This replaces the
ChromaDB sidecar from v1.

For SQLite dev mode, the `_NoOpVectorStore` is returned: it accepts
upserts but returns empty search results. The real semantic search in
that mode happens via `app/agents/tools.py` `_vector_search` which builds
an in-memory matrix.

Public interface (unchanged from v1, callers don't need updates):
    VectorStore                 — protocol
    VectorHit(id, text, score, metadata)
    get_vector_store()          — singleton, picks backend based on DB type
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from app.config import settings
from app.services.embeddings import get_embedder
from app.services.ocr import Clause

logger = logging.getLogger(__name__)


@dataclass
class VectorHit:
    id: str
    text: str
    score: float
    metadata: dict[str, Any]


class VectorStore(Protocol):
    def upsert_clauses(
        self,
        *,
        tenant_id: str,
        document_id: str,
        document_label: str,
        clauses: Iterable[Clause],
    ) -> int: ...

    def search(
        self,
        *,
        query: str,
        tenant_id: str | None = None,
        document_id: str | None = None,
        top_k: int = 5,
    ) -> list[VectorHit]: ...

    def delete_document(self, document_id: str) -> int: ...


# ---------------------------------------------------------------------------
# pgvector-backed implementation
# ---------------------------------------------------------------------------

class PgVectorStore:
    """Stores embeddings in the `clause_embeddings` Postgres table.

    Uses raw SQL (not the ORM) for two reasons:
        1. pgvector's `<=>` cosine-distance operator isn't a first-class
           SQLAlchemy construct without the `pgvector.sqlalchemy` extras
        2. We want to use pgvector's HNSW index hint so search is O(log N)
           rather than a sequential scan
    """

    def __init__(self):
        self._embedder = get_embedder()
        # Lazy-imported so SQLite dev path doesn't trip on missing psycopg2
        from app.db.session import engine
        self._engine = engine

    # ------------------------------------------------------------------
    def upsert_clauses(
        self,
        *,
        tenant_id: str,
        document_id: str,
        document_label: str,
        clauses: Iterable[Clause],
    ) -> int:
        """Upsert a batch of clauses. Returns count inserted/updated."""
        from sqlalchemy import text

        clauses = list(clauses)
        if not clauses:
            return 0

        # Embed in batch (significantly faster than per-clause)
        texts = [c.text[:2000] for c in clauses]
        try:
            vectors = [self._embedder.embed_one(t) for t in texts]
        except Exception as exc:
            logger.warning("Batch embed failed: %s", exc)
            return 0

        # Upsert via ON CONFLICT
        sql = text("""
            INSERT INTO clause_embeddings (
                tenant_id, document_id, page_number, clause_number, heading,
                clause_text, embedding, char_start, char_end
            ) VALUES (
                :tenant_id, :document_id, :page, :clause_no, :heading,
                :text, (:embedding)::vector, :char_start, :char_end
            )
            ON CONFLICT (document_id, page_number, char_start)
            DO UPDATE SET
                clause_number = EXCLUDED.clause_number,
                heading = EXCLUDED.heading,
                clause_text = EXCLUDED.clause_text,
                embedding = EXCLUDED.embedding,
                char_end = EXCLUDED.char_end
        """)

        rows = []
        for c, v in zip(clauses, vectors):
            rows.append({
                "tenant_id": tenant_id,
                "document_id": document_id,
                "page": c.page_number,
                "clause_no": c.clause_number,
                "heading": c.heading[:512] if c.heading else None,
                "text": c.text,
                "embedding": _to_pgvector_literal(v),
                "char_start": c.start_offset,
                "char_end": c.end_offset,
            })

        with self._engine.begin() as conn:
            conn.execute(sql, rows)
        logger.info("upserted %d clauses for document %s", len(rows), document_id)
        return len(rows)

    # ------------------------------------------------------------------
    def search(
        self,
        *,
        query: str,
        tenant_id: str | None = None,
        document_id: str | None = None,
        top_k: int = 5,
    ) -> list[VectorHit]:
        """Cosine-similarity search via pgvector's `<=>` operator.

        `<=>` returns cosine DISTANCE (0=identical, 2=opposite). We convert
        to similarity (1 - distance) so callers get the conventional 0..1
        range where higher = more similar.
        """
        from sqlalchemy import text

        try:
            q_vec = self._embedder.embed_one(query)
        except Exception as exc:
            logger.warning("query embed failed: %s", exc)
            return []

        # Build WHERE clause based on filters
        where_clauses = []
        params: dict[str, Any] = {
            "embedding": _to_pgvector_literal(q_vec),
            "top_k": top_k,
        }
        if tenant_id:
            where_clauses.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id
        if document_id:
            where_clauses.append("document_id = :document_id")
            params["document_id"] = document_id
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        sql = text(f"""
            SELECT
                id::text AS id,
                clause_text,
                tenant_id::text AS tenant_id,
                document_id::text AS document_id,
                page_number,
                clause_number,
                heading,
                embedding <=> (:embedding)::vector AS distance
            FROM clause_embeddings
            {where_sql}
            ORDER BY embedding <=> (:embedding)::vector
            LIMIT :top_k
        """)

        with self._engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            VectorHit(
                id=str(row.id),
                text=row.clause_text,
                score=float(1.0 - row.distance),  # similarity in [0, 1]
                metadata={
                    "tenant_id": row.tenant_id,
                    "document_id": row.document_id,
                    "page_number": row.page_number,
                    "clause_number": row.clause_number,
                    "heading": row.heading,
                },
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    def delete_document(self, document_id: str) -> int:
        from sqlalchemy import text

        with self._engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM clause_embeddings WHERE document_id = :d"),
                {"d": document_id},
            )
        rowcount = result.rowcount or 0
        logger.info("deleted %d clauses for document %s", rowcount, document_id)
        return rowcount


# ---------------------------------------------------------------------------
# No-op store for SQLite dev mode
# ---------------------------------------------------------------------------

class _NoOpVectorStore:
    """Returned when DATABASE_URL is SQLite. Accepts upserts (silent no-op)
    and returns empty search results. The real semantic search in dev mode
    happens via app.agents.tools._vector_search's in-memory matrix path.
    """

    def upsert_clauses(self, *, tenant_id, document_id, document_label, clauses) -> int:
        return 0

    def search(self, *, query, tenant_id=None, document_id=None, top_k=5):
        return []

    def delete_document(self, document_id: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_singleton: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _singleton
    if _singleton is not None:
        return _singleton

    if settings.is_postgres:
        try:
            _singleton = PgVectorStore()
            logger.info("Vector store: pgvector (Postgres)")
        except Exception as exc:
            logger.error("PgVectorStore init failed: %s — using NoOp store", exc)
            _singleton = _NoOpVectorStore()
    else:
        logger.info("Vector store: NoOp (SQLite dev mode — uses in-memory cosine)")
        _singleton = _NoOpVectorStore()

    return _singleton


# ---------------------------------------------------------------------------
# pgvector serialization helper
# ---------------------------------------------------------------------------

def _to_pgvector_literal(vec: list[float]) -> str:
    """Convert a Python list of floats to the pgvector literal string format.

    pgvector accepts the same '[1,2,3]' literal as a string parameter when
    cast to ::vector. We avoid the `pgvector` Python adapter for psycopg2
    here so we don't take that as a hard dependency on the SQLite path.
    """
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
