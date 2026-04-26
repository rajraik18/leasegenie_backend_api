"""Embeddings service.

Thin wrapper around Ollama's /api/embeddings endpoint. Uses the embedding
model configured in settings (default: nomic-embed-text, 768-dim, strong on
legal/business text).

Embeddings are written to the `clause_embeddings` table in Postgres
(pgvector) on document upload — see app/services/vector_store.py. The
agent's hybrid retrieval combines BM25 lexical search with these vector
embeddings via reciprocal rank fusion in app/agents/tools.py.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.config import settings

logger = logging.getLogger(__name__)


class Embedder:
    """Async-friendly thin wrapper around ollama.Client.embeddings."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
    ):
        try:
            import ollama  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("ollama package not installed. `pip install ollama`") from e
        self._ollama = ollama
        self._client = ollama.Client(
            host=base_url or settings.ollama_base_url,
            timeout=settings.ollama_timeout_seconds,
        )
        self.model = model or settings.ollama_embed_model
        self.dim = settings.ollama_embed_dim

    def embed_one(self, text: str) -> list[float]:
        """Embed a single string. Empty strings return a zero-vector."""
        text = (text or "").strip()
        if not text:
            return [0.0] * self.dim
        try:
            # Ollama's python client exposes `embeddings(model, prompt)`
            resp = self._client.embeddings(model=self.model, prompt=text)
        except Exception as exc:
            logger.warning("Embedding call failed: %s", exc)
            return [0.0] * self.dim
        vec = resp.get("embedding") or []
        if not vec:
            return [0.0] * self.dim
        return list(vec)

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed a batch. Ollama has no native batch API — we loop sequentially."""
        return [self.embed_one(t) for t in texts]


_singleton: Embedder | None = None


def get_embedder() -> Embedder:
    global _singleton
    if _singleton is None:
        _singleton = Embedder()
    return _singleton
