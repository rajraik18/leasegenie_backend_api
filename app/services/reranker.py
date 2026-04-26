"""BGE Cross-Encoder Reranker.

Third stage of retrieval after the BM25+vector RRF pool has been assembled.

Why a reranker:
    BM25 ranks by term frequency and inverse document frequency. Vector
    search ranks by bi-encoder cosine similarity — both encode the query
    and each candidate SEPARATELY, then compare vectors. A cross-encoder
    looks at the [query, candidate] PAIR jointly and produces a single
    relevance score. This is slower per pair but dramatically more accurate
    on the top ~20 candidates, which is why "retrieve with bi-encoder +
    rerank with cross-encoder" is the standard high-precision pipeline.

Model:
    `BAAI/bge-reranker-base` — 278MB, BERT-base class, English+Chinese,
    runs on CPU at ~50 pairs/sec. Good balance for on-prem deployment.
    Alternatives: `BAAI/bge-reranker-large` (1.3GB, +3-5% accuracy).

Graceful degradation:
    If sentence-transformers or the model aren't installed, `rerank()`
    returns the candidates in the original order — never raises.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---- Configuration ----
DEFAULT_MODEL = "BAAI/bge-reranker-base"
TOP_K_AFTER_RERANK = 5        # keep best 5 after reranking
MAX_PAIRS = 50                # never rerank more than this many — protects latency
MIN_CANDIDATES = 3            # skip reranking entirely below this (not worth load cost)


# Lazy singleton — model is heavy (~280 MB), load once
_reranker_model = None
_reranker_available: bool | None = None


def _try_load_reranker(model_name: str = DEFAULT_MODEL):
    """Attempt to load the cross-encoder. Returns None on failure."""
    global _reranker_model, _reranker_available
    if _reranker_available is False:
        return None
    if _reranker_model is not None:
        return _reranker_model

    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except ImportError:
        logger.info(
            "sentence-transformers not installed — reranker disabled. "
            "To enable: pip install sentence-transformers"
        )
        _reranker_available = False
        return None

    try:
        # max_length=512 tokens — leases can have long clauses
        _reranker_model = CrossEncoder(model_name, max_length=512, device="cpu")
        _reranker_available = True
        logger.info("Loaded cross-encoder reranker: %s", model_name)
    except Exception as exc:
        logger.warning("Failed to load reranker %s: %s", model_name, exc)
        _reranker_available = False
        _reranker_model = None
    return _reranker_model


def is_available() -> bool:
    """Cheap check — does not actually load the model."""
    if _reranker_available is not None:
        return _reranker_available
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    top_k: int = TOP_K_AFTER_RERANK,
    snippet_key: str = "snippet",
    score_key: str = "rerank_score",
) -> list[dict[str, Any]]:
    """Rerank a list of candidate clauses by cross-encoder relevance.

    Parameters
    ----------
    query: str
        The search query.
    candidates: list of dict
        Output of `DocumentContext.hybrid_search()` — each dict must have
        a `snippet` (or configurable `snippet_key`) field.
    top_k: int
        How many top-reranked candidates to return.
    snippet_key: str
        Which dict key contains the text to compare against the query.
    score_key: str
        Which dict key to write the reranker score into.

    Returns
    -------
    A new list of dicts, each a shallow copy of the corresponding input
    annotated with:
        - `rerank_score`   float, higher is more relevant
        - `orig_rank`      int, position in the input list (1-indexed)
    Sorted by rerank_score descending. Truncated to top_k.

    If the reranker is unavailable, returns candidates[:top_k] unchanged.
    """
    if not candidates:
        return []
    if len(candidates) < MIN_CANDIDATES:
        return candidates[:top_k]

    model = _try_load_reranker()
    if model is None:
        # Graceful fallback: preserve input order
        return [
            {**c, "orig_rank": i + 1, score_key: None}
            for i, c in enumerate(candidates[:top_k])
        ]

    # Prepare [query, candidate_text] pairs. Cap at MAX_PAIRS to bound latency.
    pool = candidates[:MAX_PAIRS]
    pairs = [[query, c.get(snippet_key) or ""] for c in pool]

    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as exc:
        logger.warning("Reranker predict failed: %s — preserving original order", exc)
        return [
            {**c, "orig_rank": i + 1, score_key: None}
            for i, c in enumerate(candidates[:top_k])
        ]

    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for i, (c, s) in enumerate(zip(pool, scores)):
        ranked.append((float(s), i, {**c, "orig_rank": i + 1, score_key: round(float(s), 4)}))

    # Sort by rerank score desc, stable tiebreak on original position
    ranked.sort(key=lambda t: (-t[0], t[1]))
    return [item[2] for item in ranked[:top_k]]
