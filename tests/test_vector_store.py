"""Test vector store + semantic_search tool with mocked backend.

We exercise the tool-dispatch layer (which falls back to BM25 when the
vector store is unavailable) and the tool schema registration. The
storage backend (ChromaDB in v1, pgvector in v2) is mocked in both cases
since this test focuses on the tool wiring, not storage.
"""
import sys
import types

from app.agents.tools import DocumentContext, TOOL_SCHEMAS, execute_tool
from app.services.ocr import Clause


def _ctx(tenant_id: str | None = None):
    base = [
        Clause(clause_number="1", heading="BASIC", text="Tenant: TechCo Inc.",
               page_number=1, start_offset=0, end_offset=100),
        Clause(clause_number="3.1", heading="BASE RENT",
               text="Annual Base Rent of $120,000 per year",
               page_number=3, start_offset=0, end_offset=100),
    ]
    ctx = DocumentContext(per_document_clauses={"base_lease": base}, tenant_id=tenant_id)
    ctx.build_index()
    return ctx


def test_semantic_search_tool_is_advertised():
    """The tool should be visible to the LLM in the tool schemas."""
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    assert "semantic_search" in names
    assert "search_document" in names


def test_semantic_search_falls_back_to_bm25_without_tenant_id():
    """When ctx.tenant_id is None, semantic_search silently falls back to BM25."""
    ctx = _ctx(tenant_id=None)
    r = execute_tool("semantic_search", {"query": "base rent"}, ctx)
    assert r.ok
    # Should find the BASE RENT clause via BM25 fallback
    assert "Base Rent" in r.content or "BASE RENT" in r.content


def test_semantic_search_falls_back_when_vector_store_unavailable(monkeypatch):
    """If the vector store raises on import/init, semantic_search still responds."""
    ctx = _ctx(tenant_id="t0")

    # Shim a vector store module that raises when get_vector_store is called
    fake_vs = types.ModuleType("app.services.vector_store")
    def _raise():
        raise RuntimeError("vector store unavailable in test env")
    fake_vs.get_vector_store = _raise
    monkeypatch.setitem(sys.modules, "app.services.vector_store", fake_vs)

    r = execute_tool("semantic_search", {"query": "rent"}, ctx)
    assert r.ok
    # The fallback message plus lexical results should appear
    assert "unavailable" in r.content.lower() or "Base Rent" in r.content or "fallback" in r.content.lower()


def test_semantic_search_with_mocked_vector_store(monkeypatch):
    """When the vector store returns hits, they should be formatted into the result."""
    ctx = _ctx(tenant_id="t0")

    # Build a fake VectorHit-shaped object
    class _FakeHit:
        def __init__(self, text, score, meta):
            self.text = text
            self.score = score
            self.metadata = meta

    class _FakeVS:
        def semantic_search(self, query, tenant_id, document_label, top_k):
            return [
                _FakeHit(
                    text="Annual Base Rent of $120,000 per year",
                    score=0.91,
                    meta={"document_label": "base_lease", "page_number": 3,
                          "clause_number": "3.1", "heading": "BASE RENT"},
                ),
            ]

    fake_vs = types.ModuleType("app.services.vector_store")
    fake_vs.get_vector_store = lambda: _FakeVS()
    monkeypatch.setitem(sys.modules, "app.services.vector_store", fake_vs)

    r = execute_tool("semantic_search", {"query": "rent", "top_k": 3}, ctx)
    assert r.ok
    assert "Semantic search returned" in r.content
    assert "0.91" in r.content
    assert "$120,000" in r.content
    assert "BASE RENT" in r.content


def test_bm25_search_still_works_independently():
    """BM25 search_document remains orthogonal to vector store."""
    ctx = _ctx(tenant_id="t0")
    r = execute_tool("search_document", {"query": "annual base rent"}, ctx)
    assert r.ok
    assert "BASE RENT" in r.content or "Base Rent" in r.content
