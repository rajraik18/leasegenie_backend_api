"""Agent tools.

These are the functions the field-extraction agent can call while reasoning
about a lease. Each tool is a pure function over a `DocumentContext`, with a
JSON-Schema description for the LLM.

Design principles:
    * Every tool is cheap and side-effect-free (the only side-effecting tool
      is `finalize_answer`, which emits the terminal result).
    * Outputs are short, structured strings — the agent re-reads them as
      observations, so brevity matters for context budget.
    * Tool errors are returned as strings, not exceptions, so the agent can
      recover.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rank_bm25 import BM25Okapi

from app.services.ocr import Clause


# ---------------------------------------------------------------------------
# Document context — shared state for all tool calls during one extraction
# ---------------------------------------------------------------------------

@dataclass
class DocumentContext:
    """Bundles a base lease + its amendments into a searchable corpus.

    One context object is created per tenant and reused across all 72 field
    agents for that tenant.
    """
    # per-document clauses, keyed by a human label the agent sees
    #   "base_lease"  -> list[Clause]
    #   "amendment_1" -> list[Clause]
    per_document_clauses: dict[str, list[Clause]]

    # tenant_id for scoping vector-store queries
    tenant_id: str | None = None

    # flat corpus view for BM25 search
    _flat: list[tuple[str, Clause]] = field(default_factory=list)  # (doc_label, clause)
    _bm25: BM25Okapi | None = None

    def build_index(self) -> None:
        """Tokenize + build BM25 across every clause in every document."""
        self._flat = [
            (label, c)
            for label, clauses in self.per_document_clauses.items()
            for c in clauses
        ]
        if not self._flat:
            self._bm25 = None
            return
        tokenized = [_tokenize(c.text) for _, c in self._flat]
        self._bm25 = BM25Okapi(tokenized)
        # Vector embeddings are built lazily on first hybrid_search() call
        self._clause_vectors = None
        self._embedder = None

    # ------------------------------------------------------------------
    # Vector support (lazy)
    # ------------------------------------------------------------------

    def _ensure_vector_index(self) -> bool:
        """Build the in-memory clause-vector matrix on first use.

        Returns True on success, False if the embedder is unavailable.
        If embedding fails for some clauses, they get a zero-vector and
        will simply not match in cosine similarity.
        """
        if getattr(self, "_clause_vectors", None) is not None:
            return True
        try:
            from app.services.embeddings import get_embedder  # lazy import
            self._embedder = get_embedder()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).info(
                "Vector search unavailable (embedder init failed: %s) — "
                "falling back to BM25-only", exc,
            )
            self._clause_vectors = []
            self._embedder = None
            return False

        import logging
        logger = logging.getLogger(__name__)
        vectors: list[list[float]] = []
        for i, (_, c) in enumerate(self._flat):
            # Truncate very long clauses — embedding model has a token limit
            snippet = c.text[:2000]
            try:
                v = self._embedder.embed_one(snippet)
            except Exception as exc:
                logger.debug("embed clause %d failed: %s", i, exc)
                v = [0.0] * (self._embedder.dim if self._embedder else 768)
            vectors.append(v)
        self._clause_vectors = vectors
        return True

    def _vector_search(self, query: str, top_k: int = 20,
                       doc_filter: str | None = None) -> list[tuple[int, float]]:
        """Return [(flat_index, cosine_similarity)] for top_k most-similar clauses.

        Uses the in-memory clause-vector matrix scoped to the documents in
        this DocumentContext (typically 1-8 PDFs for one extraction). For
        these tiny corpora (<5K clauses), in-memory cosine is faster than
        round-tripping to pgvector — avoids a network hop per question.

        Cross-document semantic cache (across all tenants over time) lives in
        the pgvector store at app/services/vector_store.py and is queried
        via `VectorStore.search()`. That path uses the HNSW index.

        Empty list on failure.
        """
        if not self._ensure_vector_index():
            return []
        if not self._clause_vectors:
            return []
        try:
            q_vec = self._embedder.embed_one(query)
        except Exception:
            return []

        # Manual cosine similarity to avoid numpy dependency
        def _dot(a: list[float], b: list[float]) -> float:
            n = min(len(a), len(b))
            return sum(a[i] * b[i] for i in range(n))

        def _norm(a: list[float]) -> float:
            return (sum(x * x for x in a)) ** 0.5

        q_norm = _norm(q_vec)
        if q_norm == 0.0:
            return []

        sims: list[tuple[int, float]] = []
        for i, cv in enumerate(self._clause_vectors):
            if doc_filter and self._flat[i][0] != doc_filter:
                continue
            cn = _norm(cv)
            if cn == 0.0:
                continue
            sims.append((i, _dot(q_vec, cv) / (q_norm * cn)))

        sims.sort(key=lambda t: t[1], reverse=True)
        return sims[:top_k]

    # ------------------------------------------------------------------
    # Hybrid search (BM25 + vector fused with RRF)
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        top_k: int = 5,
        doc_filter: str | None = None,
        rrf_k: int = 60,
        pool_size: int = 30,
    ) -> list[dict[str, Any]]:
        """Hybrid retrieval: BM25 + vector fused with Reciprocal Rank Fusion.

        RRF formula (Cormack, Clarke & Buettcher 2009):
            score(d) = Σ over retrievers r  :  1 / (k + rank_r(d))
        k=60 is the widely-used default.

        Why hybrid: BM25 handles literal keywords like "Commencement Date"
        perfectly but misses paraphrases like "Lease began on". Vectors
        handle paraphrase but miss rare terms (e.g. case-specific party
        names). Fusing both dominates either alone on leases.
        """
        if self._bm25 is None or not self._flat:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        # --- BM25 ranking (over entire pool) ---
        bm25_scores = self._bm25.get_scores(tokens)
        bm25_ranked: list[tuple[int, float]] = [
            (i, float(bm25_scores[i])) for i in range(len(self._flat))
            if bm25_scores[i] > 0
        ]
        bm25_ranked.sort(key=lambda t: t[1], reverse=True)
        bm25_ranked = bm25_ranked[:pool_size]

        # --- Vector ranking (top pool_size) ---
        vec_ranked = self._vector_search(query, top_k=pool_size, doc_filter=doc_filter)

        # If vectors aren't available, just return BM25 results
        if not vec_ranked:
            return self.search(query, top_k=top_k, doc_filter=doc_filter)

        # --- RRF fusion ---
        rrf_scores: dict[int, float] = {}
        rank_info: dict[int, dict[str, Any]] = {}

        for rank, (i, _score) in enumerate(bm25_ranked):
            rrf_scores[i] = rrf_scores.get(i, 0.0) + 1.0 / (rrf_k + rank + 1)
            rank_info.setdefault(i, {})["bm25_rank"] = rank + 1

        for rank, (i, sim) in enumerate(vec_ranked):
            rrf_scores[i] = rrf_scores.get(i, 0.0) + 1.0 / (rrf_k + rank + 1)
            rank_info.setdefault(i, {})["vec_rank"] = rank + 1
            rank_info[i]["vec_sim"] = round(sim, 3)

        # Sort by fused score
        fused = sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)

        results: list[dict[str, Any]] = []
        for i, fused_score in fused:
            label, c = self._flat[i]
            if doc_filter and label != doc_filter:
                continue
            info = rank_info.get(i, {})
            results.append({
                "doc": label,
                "page": c.page_number,
                "clause_number": c.clause_number,
                "heading": c.heading,
                "snippet": _shorten(c.text, 400),
                "score": round(fused_score, 4),
                "bm25_rank": info.get("bm25_rank"),
                "vec_rank": info.get("vec_rank"),
                "vec_sim": info.get("vec_sim"),
                "_clause_ref": f"{label}:{c.page_number}:{c.clause_number or 'n/a'}:{c.start_offset}",
            })
            if len(results) >= top_k:
                break
        return results

    def search(self, query: str, top_k: int = 5, doc_filter: str | None = None) -> list[dict[str, Any]]:
        """BM25 search. Returns [{doc, page, clause_number, heading, snippet, score}, ...]."""
        if self._bm25 is None or not self._flat:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)

        # Rank all indices
        ranked = sorted(range(len(self._flat)), key=lambda i: scores[i], reverse=True)

        results = []
        for idx in ranked:
            if scores[idx] <= 0:
                break
            label, c = self._flat[idx]
            if doc_filter and label != doc_filter:
                continue
            results.append({
                "doc": label,
                "page": c.page_number,
                "clause_number": c.clause_number,
                "heading": c.heading,
                "snippet": _shorten(c.text, 400),
                "score": round(float(scores[idx]), 2),
                "_clause_ref": f"{label}:{c.page_number}:{c.clause_number or 'n/a'}:{c.start_offset}",
            })
            if len(results) >= top_k:
                break
        return results

    def get_page(self, doc_label: str, page_number: int) -> str | None:
        """Full text of a specific page of a specific document."""
        clauses = self.per_document_clauses.get(doc_label, [])
        parts = [c.text for c in clauses if c.page_number == page_number]
        if not parts:
            return None
        return "\n\n".join(parts)

    def get_clause(self, clause_ref: str) -> Clause | None:
        """Look up by the `_clause_ref` token we returned in search."""
        for label, c in self._flat:
            key = f"{label}:{c.page_number}:{c.clause_number or 'n/a'}:{c.start_offset}"
            if key == clause_ref:
                return c
        return None

    def list_amendments(self) -> list[str]:
        return sorted(
            [k for k in self.per_document_clauses if k.startswith("amendment_")],
            key=lambda s: int(s.split("_")[1]),
        )


# ---------------------------------------------------------------------------
# Tool schemas advertised to the model (OpenAI/Ollama function-call format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_document",
            "description": (
                "Full-text BM25 search across the lease + all amendments. Use this "
                "FIRST to find clauses relevant to the field. Returns up to top_k "
                "results with doc label, page, clause number, heading, and snippet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text query — use domain terms like 'base rent', 'percentage rent breakpoint', 'operating expenses exclusion'.",
                    },
                    "top_k": {"type": "integer", "description": "Max results (1-10).", "default": 5},
                    "doc_filter": {
                        "type": "string",
                        "description": "Restrict to one document, e.g. 'base_lease' or 'amendment_3'. Optional.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Semantic (vector) search across indexed lease clauses. Use this "
                "when keywords won't match — e.g. looking for rent-concept language "
                "phrased unusually, or finding the most similar clauses to a defined "
                "term. Complementary to search_document (BM25)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query."},
                    "top_k": {"type": "integer", "default": 5},
                    "doc_filter": {
                        "type": "string",
                        "description": "Restrict to one document, e.g. 'base_lease'. Optional.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Fetch the full text of a single page of a specific document. Use after search when you need more surrounding context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "Document label, e.g. 'base_lease', 'amendment_2'."},
                    "page_number": {"type": "integer"},
                },
                "required": ["doc", "page_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_clause",
            "description": "Fetch the full text of a clause using the _clause_ref returned by search_document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "clause_ref": {"type": "string"},
                },
                "required": ["clause_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_definitions",
            "description": "Look up a defined term (e.g. 'Base Rent', 'Operating Expenses') in the lease's definitions / interpretation section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_amendment_override",
            "description": "List amendment clauses that reference the current field, in chronological order. Use this to see if any amendment modifies the base lease value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_name": {"type": "string"},
                },
                "required": ["field_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_answer",
            "description": (
                "EMIT the final extracted value and STOP. Only call this once you are confident. "
                "value: the normalized answer string (for Number fields use digits only, no $ or commas; "
                "for Text fields use a concise clean string). "
                "If the document truly has no information, set value='None' but still supply clause_text "
                "with the most topically relevant paragraph."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "Normalized answer or 'None'."},
                    "raw_value": {"type": "string", "description": "Verbatim snippet from the document, if any."},
                    "confidence": {"type": "number", "description": "0.0 - 1.0 grounded-ness score."},
                    "source_doc": {"type": "string", "description": "Document label the answer came from."},
                    "page_number": {"type": "integer"},
                    "clause_number": {"type": "string"},
                    "clause_text": {"type": "string", "description": "Fallback full clause paragraph."},
                    "reasoning": {"type": "string", "description": "Brief rationale for the answer and confidence."},
                },
                "required": ["value", "confidence"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    ok: bool
    content: str                         # string the agent will see
    finalized: dict[str, Any] | None = None  # set only by finalize_answer


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    ctx: DocumentContext,
) -> ToolResult:
    try:
        if name == "search_document":
            return _tool_search(arguments, ctx)
        if name == "semantic_search":
            return _tool_semantic_search(arguments, ctx)
        if name == "read_page":
            return _tool_read_page(arguments, ctx)
        if name == "read_clause":
            return _tool_read_clause(arguments, ctx)
        if name == "check_definitions":
            return _tool_definitions(arguments, ctx)
        if name == "check_amendment_override":
            return _tool_amendment_override(arguments, ctx)
        if name == "finalize_answer":
            return _tool_finalize(arguments)
        return ToolResult(ok=False, content=f"Unknown tool: {name}")
    except Exception as exc:  # defensive — never throw into the agent loop
        return ToolResult(ok=False, content=f"Tool '{name}' error: {exc}")


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------

def _tool_search(args: dict[str, Any], ctx: DocumentContext) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, content="Error: 'query' is required.")
    top_k = int(args.get("top_k") or 5)
    top_k = max(1, min(10, top_k))
    doc_filter = args.get("doc_filter") or None

    hits = ctx.search(query, top_k=top_k, doc_filter=doc_filter)
    if not hits:
        return ToolResult(ok=True, content="No results.")
    lines = [f"Found {len(hits)} results:"]
    for i, h in enumerate(hits, start=1):
        lines.append(
            f"[{i}] doc={h['doc']} page={h['page']} clause={h['clause_number'] or '-'} "
            f"heading={h['heading'] or '-'} score={h['score']}\n"
            f"    ref: {h['_clause_ref']}\n"
            f"    snippet: {h['snippet']}"
        )
    return ToolResult(ok=True, content="\n".join(lines))


def _tool_semantic_search(args: dict[str, Any], ctx: DocumentContext) -> ToolResult:
    """Vector-store semantic search. Falls back to BM25 if no tenant_id is set
    (e.g. when the context was built for a standalone test)."""
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, content="Error: 'query' is required.")
    if ctx.tenant_id is None:
        # Silently fall back to lexical search when vector filtering isn't available
        return _tool_search(args, ctx)

    top_k = int(args.get("top_k") or 5)
    top_k = max(1, min(10, top_k))
    doc_filter = args.get("doc_filter") or None

    try:
        from app.services.vector_store import get_vector_store
        hits = get_vector_store().semantic_search(
            query=query, tenant_id=ctx.tenant_id,
            document_label=doc_filter, top_k=top_k,
        )
    except Exception as exc:
        return ToolResult(ok=True, content=f"(semantic search unavailable: {exc} — falling back to lexical)\n" +
                         _tool_search(args, ctx).content)

    if not hits:
        return ToolResult(ok=True, content="No semantic matches.")
    lines = [f"Semantic search returned {len(hits)} results (higher score = more similar):"]
    for i, h in enumerate(hits, start=1):
        md = h.metadata or {}
        lines.append(
            f"[{i}] doc={md.get('document_label','?')} page={md.get('page_number','?')} "
            f"clause={md.get('clause_number','-')} heading={md.get('heading','-')} "
            f"similarity={h.score:.2f}\n"
            f"    text: {_shorten(h.text, 400)}"
        )
    return ToolResult(ok=True, content="\n".join(lines))


def _tool_read_page(args: dict[str, Any], ctx: DocumentContext) -> ToolResult:
    doc = str(args.get("doc") or "")
    try:
        page = int(args.get("page_number"))
    except (TypeError, ValueError):
        return ToolResult(ok=False, content="Error: 'page_number' must be an integer.")
    text = ctx.get_page(doc, page)
    if text is None:
        return ToolResult(ok=True, content=f"No page {page} in {doc}.")
    # Cap to 4000 chars to preserve context budget
    return ToolResult(ok=True, content=f"[{doc} page {page}]\n{_shorten(text, 4000)}")


def _tool_read_clause(args: dict[str, Any], ctx: DocumentContext) -> ToolResult:
    ref = str(args.get("clause_ref") or "")
    c = ctx.get_clause(ref)
    if c is None:
        return ToolResult(ok=True, content=f"No clause found for ref '{ref}'.")
    return ToolResult(
        ok=True,
        content=(
            f"[page={c.page_number} clause={c.clause_number or '-'} heading={c.heading or '-'}]\n"
            f"{_shorten(c.text, 3000)}"
        ),
    )


def _tool_definitions(args: dict[str, Any], ctx: DocumentContext) -> ToolResult:
    term = str(args.get("term") or "").strip()
    if not term:
        return ToolResult(ok=False, content="Error: 'term' is required.")
    # Heuristic: defined terms appear as "'Term' means ..." or "Term shall mean ..."
    pattern_strs = [
        rf'["\u201c]{re.escape(term)}["\u201d]\s+(?:means|shall\s+mean)\b',
        rf'\b{re.escape(term)}\b\s+(?:means|shall\s+mean)\b',
    ]
    patterns = [re.compile(p, re.IGNORECASE) for p in pattern_strs]

    hits: list[str] = []
    for label, clauses in ctx.per_document_clauses.items():
        for c in clauses:
            for pat in patterns:
                m = pat.search(c.text)
                if m:
                    # Grab ~500 chars around the hit
                    start = max(0, m.start() - 50)
                    end = min(len(c.text), m.start() + 500)
                    hits.append(
                        f"[{label} p.{c.page_number} {c.clause_number or '-'}] "
                        f"{c.text[start:end].strip()}"
                    )
                    if len(hits) >= 5:
                        break
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break

    if not hits:
        return ToolResult(ok=True, content=f"No definition found for '{term}'.")
    return ToolResult(ok=True, content="\n\n---\n\n".join(hits))


def _tool_amendment_override(args: dict[str, Any], ctx: DocumentContext) -> ToolResult:
    field_name = str(args.get("field_name") or "").strip()
    if not field_name:
        return ToolResult(ok=False, content="Error: 'field_name' is required.")
    amendments = ctx.list_amendments()
    if not amendments:
        return ToolResult(ok=True, content="No amendments present. Use base_lease value.")

    tokens = _tokenize(field_name)
    if not tokens:
        return ToolResult(ok=True, content=f"No amendments discuss '{field_name}'.")

    blocks = []
    for label in amendments:
        hits = ctx.search(field_name, top_k=3, doc_filter=label)
        if hits:
            top = hits[0]
            blocks.append(
                f"{label} (page {top['page']}, clause {top['clause_number'] or '-'}): "
                f"{top['snippet']}"
            )
    if not blocks:
        return ToolResult(ok=True, content=f"No amendments discuss '{field_name}'.")
    return ToolResult(
        ok=True,
        content=(
            "Amendments that reference this field (in chronological order — apply latest):\n\n"
            + "\n\n".join(blocks)
        ),
    )


def _tool_finalize(args: dict[str, Any]) -> ToolResult:
    value = args.get("value")
    if value is None:
        return ToolResult(ok=False, content="Error: 'value' is required in finalize_answer.")
    try:
        confidence = float(args.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    out = {
        "value": str(value),
        "raw_value": args.get("raw_value"),
        "confidence": confidence,
        "source_doc": args.get("source_doc"),
        "page_number": _maybe_int(args.get("page_number")),
        "clause_number": _maybe_str(args.get("clause_number")),
        "clause_text": args.get("clause_text"),
        "reasoning": args.get("reasoning"),
    }
    return ToolResult(ok=True, content="ANSWER FINALIZED.", finalized=out)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(s: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(s or "")]


def _shorten(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0] + " …"


def _maybe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _maybe_str(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)
