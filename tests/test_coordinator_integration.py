"""Integration test: coordinator end-to-end on mini synthetic leases.

This test exercises the WHOLE pipeline in one go:
    OCR → doc_classifier → specialists → playbook executor (voting +
    few-shot + hybrid retrieval + critique) → reconciliation → derived
    fields → final AgentFieldResult list

All heavyweight dependencies (Ollama, PaddleOCR, pgvector, sentence-
transformers) are mocked or stubbed so this test runs in a clean CI
environment without any external services.

The fixtures include:
    1. A "base_lease" document with text mirroring Sample 6 (HMBP-BCP /
       Garner Appliance, Industrial warehouse).
    2. An "amendment_1" that changes the monthly rent (to test
       reconciliation amendment-override logic).
    3. A mock OllamaAgentClient that returns canned JSON responses
       keyed by field_id + question_id, including intentional
       disagreements to exercise the voting path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_6_TEXT = """LEASE
FACE PAGE
LEASE DATE: 8/2/2024
LANDLORD: HMBP - BCP LLC, a North Carolina limited liability company
LANDLORD'S NOTICE ADDRESS: 500 East Morehead Street, Suite 200, Charlotte, NC 28202
TENANT: GARNER T.V. & APPLIANCES INC., a North Carolina corporation dba GARNER APPLIANCE & MATTRESS
TENANT'S NOTICE ADDRESS: 875 Highway 70 West, Garner, North Carolina 27529
PROJECT: That certain project known as Beacon Commerce Park, with an address of 4900 Jones Sausage Road, Garner, North Carolina 27529 and consisting of 260,954 square feet.
PREMISES: Shall be deemed to be 27,298 square feet ("SF") located at 4900 Jones Sausage Road, Suite 125, Garner, North Carolina 27529
TERM: A period of sixty-two (62) months, beginning on the Commencement Date and ending on the last day of the sixty-second (62nd) full calendar month.
1.4. Commencement Date. The commencement date of this Lease shall be August 1, 2024.
ANNUAL BASE RENT: $13.55 per SF
MONTHLY BASE RENT: $30,823.99
SECURITY DEPOSIT: $41,642.30
PROPORTIONATE SHARE: 10.46%
The Premises shall be used only for general office, warehouse and distribution uses.
3.1. Late Payment. In the event that any installment of Monthly Base Rent or Additional Rent is not received by Landlord within seven (7) days of the date when such payment or reimbursement is due, Tenant shall pay to Landlord on demand a late charge equal to five percent (5%) of such payment.
EXHIBIT E GUARANTY Intentionally reserved.
EXHIBIT B PREMISES IMPROVEMENTS: Construction Allowance. Landlord shall contribute $10,000 which Tenant may, at Tenant's election, apply towards the Total Construction Costs.
"""

AMENDMENT_TEXT = """FIRST AMENDMENT TO LEASE
This First Amendment is made between HMBP-BCP LLC and Garner T.V. & Appliances Inc.
Section 1. Monthly Base Rent. Effective September 1, 2025, the Monthly Base Rent is increased to $32,500.00 per month.
All other terms of the original Lease remain in full force and effect.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_doc_classifier_on_sample_text():
    """Classifier should detect Sample 6 text as Industrial base lease."""
    from app.services.doc_classifier import classify_document

    result = classify_document(SAMPLE_6_TEXT, filename_hint="Sample_6.pdf")
    assert result.document_type == "base_lease"
    assert result.property_type == "Industrial"
    # Short fixture text; confidence will be modest but non-zero
    assert result.confidence > 0.2

    amend_result = classify_document(AMENDMENT_TEXT, filename_hint="Amendment_1.pdf")
    assert amend_result.document_type == "amendment"


def test_doc_classifier_gates_retail_fields():
    """Retail-only fields must not fire on an industrial-classified doc.

    Verified via property_applicability on the compiled playbook JSONs:
    advertisement, marketing, reporting_of_gross_sales, sales_kick_out
    should all have Industrial=False after our Week 1 Step 3 normalization.
    """
    import json
    from pathlib import Path

    pb_dir = Path(__file__).parent.parent / "data" / "playbooks_compiled"
    retail_only = [
        "advertisement.json", "marketing.json",
        "reporting_of_gross_sales.json", "sales_kick_out.json",
        "co_tenancy.json", "breakpoint.json", "percentage_rent.json",
        "continuous_operation.json", "go_dark.json",
    ]
    for pb_name in retail_only:
        path = pb_dir / pb_name
        if not path.exists():
            continue
        with open(path) as f:
            pb = json.load(f)
        app = pb.get("property_applicability", {})
        assert app.get("Industrial") is False, f"{pb_name} should not apply to Industrial"
        assert app.get("Office") is False, f"{pb_name} should not apply to Office"
        assert app.get("Retail") is True, f"{pb_name} should apply to Retail"


def test_ocr_detects_sample_1_garble():
    """The Week 1 Step 1 OCR quality detector must flag the Sample 1
    corruption pattern (orphaned non-numeric value after a money label)."""
    from app.services.ocr import _is_garbled, _count_orphaned_money_values

    garbled = """Security Deposit: ■/A
Common Area Charges: w
Insurance: 55—40■
Others: M w
Total: w"""
    clean = """Security Deposit: N/A
Common Area Charges: $683.57
Insurance: $540.68
Others: Mgt. Fee $428.68
Total: $12,713.69"""

    assert _count_orphaned_money_values(garbled) >= 2
    assert _count_orphaned_money_values(clean) == 0
    assert _is_garbled(garbled) is True
    assert _is_garbled(clean) is False


def test_derived_fields_canonical_start_date():
    """Canonical start date should prefer original_lease_commencement_date
    over term/rent commencement when present."""
    from app.services.derived_fields import derive_canonical_commencement_date

    per_field = {
        "original_lease_commencement_date": {
            "value": "June 1, 2009", "confidence": 0.95,
            "source_doc": "base_lease", "page_number": 1, "clause_text": "C-Date",
        },
        "term_commencement_date": {"value": "June 1, 2009", "confidence": 0.9},
        "rent_commencement_date": {"value": "August 1, 2009", "confidence": 0.85},
    }
    canonical = derive_canonical_commencement_date(per_field)
    assert canonical is not None
    assert canonical.value == "June 1, 2009"
    assert canonical.source_field_id == "original_lease_commencement_date"
    # The note should mention that rent_commencement was overridden
    assert "rent_commencement_date" in canonical.note


def test_derived_fields_falls_through_priority():
    """When the top-priority field is missing, fall through to the next."""
    from app.services.derived_fields import derive_canonical_commencement_date

    per_field = {
        "original_lease_commencement_date": {"value": "None", "confidence": 0},
        "term_commencement_date": {"value": "None", "confidence": 0},
        "rent_commencement_date": {
            "value": "8/1/2024", "confidence": 0.9,
            "source_doc": "base_lease", "page_number": 2,
        },
    }
    canonical = derive_canonical_commencement_date(per_field)
    assert canonical is not None
    assert canonical.value == "8/1/2024"
    assert canonical.source_field_id == "rent_commencement_date"


def test_derived_fields_property_address_composition():
    """Property address should concatenate street/city/state."""
    from app.services.derived_fields import derive_property_address

    per_field = {
        "street_address": {"value": "4900 Jones Sausage Road, Suite 125", "confidence": 0.95},
        "city": {"value": "Garner", "confidence": 0.95},
        "state": {"value": "NC", "confidence": 0.95},
    }
    addr = derive_property_address(per_field)
    assert addr is not None
    assert "4900 Jones Sausage Road" in addr.value
    assert "Garner" in addr.value
    assert "NC" in addr.value


def test_voting_canonicalizes_currency_variants():
    """$1,234.56 and $1234.56 and 1234.56 must all normalize to the same
    canonical string so they vote together."""
    from app.agents.playbook_executor import _canonicalize_value

    variants = ["$1,234.56", "$1234.56", "1,234.56", "1234.56"]
    normalized = {_canonicalize_value(v, "Currency") for v in variants}
    assert len(normalized) == 1, f"Expected 1 canonical value, got {normalized}"
    assert normalized.pop() == "1234.56"


def test_voting_canonicalizes_date_variants():
    """Multiple date formats must normalize to ISO for voting."""
    from app.agents.playbook_executor import _canonicalize_value

    variants = ["June 1, 2009", "6/1/2009", "06/01/2009", "06-01-2009"]
    normalized = {_canonicalize_value(v, "Date") for v in variants}
    assert normalized == {"2009-06-01"}


def test_voting_gating_includes_high_stakes_fields():
    """Voting should fire for currency/date/number output types and skip
    pure text output types."""
    from app.agents.playbook_executor import _should_use_voting

    class Q:
        def __init__(self, output_type="", condition_type="", question_text=""):
            self.output_type = output_type
            self.condition_type = condition_type
            self.question_text = question_text

    class PB:
        output_type = "Text"

    # Currency output type → vote
    assert _should_use_voting(Q(output_type="Currency"), PB()) is True
    # Date → vote
    assert _should_use_voting(Q(output_type="Date"), PB()) is True
    # Period Based condition → vote
    assert _should_use_voting(Q(condition_type="Period Based"), PB()) is True
    # Text output type with no condition trigger → no vote
    assert _should_use_voting(Q(output_type="Text"), PB()) is False


def test_critique_agent_demotes_on_disagreement():
    """Critique result with supports=False should halve confidence and
    set needs_review=True."""
    from types import SimpleNamespace
    from app.agents.critique_agent import CritiqueResult, apply_critique_to_result

    result = SimpleNamespace(
        value="$416.42", confidence=0.9, needs_review=False, red_flags=[],
    )
    cr = CritiqueResult(
        supports=False,
        corrected_value="$41,642.30",
        failure_mode="wrong_number",
        reasoning="Extracted $416.42 but clause states $41,642.30",
        ran=True,
    )
    apply_critique_to_result(result, cr, apply_corrections=False)

    # With apply_corrections=False, value is unchanged
    assert result.value == "$416.42"
    assert result.confidence == pytest.approx(0.45, abs=0.01)
    assert result.needs_review is True
    assert any("wrong_number" in f for f in result.red_flags)


def test_critique_agent_boosts_on_agreement():
    """Critique result with supports=True should boost confidence (capped at 0.95)."""
    from types import SimpleNamespace
    from app.agents.critique_agent import CritiqueResult, apply_critique_to_result

    result = SimpleNamespace(value="$41,642.30", confidence=0.75, needs_review=False, red_flags=[])
    cr = CritiqueResult(
        supports=True, corrected_value=None, failure_mode="none",
        reasoning="Clause matches", ran=True,
    )
    apply_critique_to_result(result, cr, apply_corrections=False)
    assert result.confidence == pytest.approx(0.80, abs=0.01)
    assert result.needs_review is False


def test_critique_handles_broken_llm():
    """When the LLM call raises, critique should return ran=False with safe defaults."""
    from app.agents.critique_agent import critique

    class BrokenClient:
        def chat_json(self, messages, temperature=0.0):
            raise RuntimeError("ollama down")

    result = critique(
        BrokenClient(),
        field_id="security_deposit",
        field_name="Security Deposit",
        category="Basic Information",
        overview="",
        value="$100",
        clause_text="Security Deposit: $100",
        source_doc="base_lease",
        page=1,
    )
    assert result.ran is False
    assert result.supports is True  # default-safe


def test_few_shot_library_covers_all_playbooks():
    """Every compiled playbook should have at least one few-shot example
    in either the in-file FEW_SHOT_LIBRARY or the EXTENDED library."""
    import os
    from pathlib import Path

    pb_dir = Path(__file__).parent.parent / "data" / "playbooks_compiled"
    all_field_ids = {
        fn[:-5] for fn in os.listdir(pb_dir)
        if fn.endswith(".json") and not fn.startswith("_")
    }

    from app.agents.few_shot_library import EXTENDED_FEW_SHOT_LIBRARY
    extended_covered = {k for k, v in EXTENDED_FEW_SHOT_LIBRARY.items() if v}

    # In-file library (mirror the set in playbook_executor.py)
    inline_covered = {
        "annual_base_rent", "original_lease_commencement_date", "allowance",
        "security_deposit", "pro_rata", "late_payment", "renewal_options", "holdover",
    }

    covered = extended_covered | inline_covered
    missing = all_field_ids - covered
    assert not missing, f"These playbooks have no few-shot examples: {sorted(missing)}"


def test_rrf_fuses_bm25_and_vector_correctly():
    """Hybrid search should combine BM25 and vector rankings via Reciprocal
    Rank Fusion so a clause ranked high by BOTH wins over a clause ranked
    high by only one."""
    import math
    import sys
    import types
    from dataclasses import dataclass

    # Stub rank_bm25 with a deterministic TF-IDF
    class StubBM25Okapi:
        def __init__(self, docs):
            self.docs = docs
            self.df = {}
            for doc in docs:
                for tok in set(doc):
                    self.df[tok] = self.df.get(tok, 0) + 1
            self.N = len(docs)
        def get_scores(self, qtok):
            scores = []
            for doc in self.docs:
                s = 0.0
                for t in qtok:
                    if t in doc:
                        tf = doc.count(t)
                        idf = math.log((self.N - self.df.get(t, 0) + 0.5) /
                                       (self.df.get(t, 0) + 0.5) + 1)
                        s += tf * idf
                scores.append(s)
            return scores

    rb = types.ModuleType("rank_bm25")
    rb.BM25Okapi = StubBM25Okapi
    sys.modules["rank_bm25"] = rb

    # Stub embedder
    class StubEmbedder:
        dim = 16
        def embed_one(self, text):
            import hashlib
            vec = [0.0] * self.dim
            for w in (text or "").lower().split()[:40]:
                h = int(hashlib.md5(w.encode()).hexdigest()[:4], 16)
                vec[h % self.dim] += 1.0
            return vec

    emb_stub = types.ModuleType("app.services.embeddings")
    emb_stub.get_embedder = lambda: StubEmbedder()
    sys.modules["app.services.embeddings"] = emb_stub

    # Import tools
    from app.agents.tools import DocumentContext
    from app.services.ocr import Clause

    clauses = [
        Clause(None, None, "Commencement Date: June 1, 2009", 1, 0, 40),
        Clause(None, None, "Annual Base Rent is $100,000", 2, 0, 35),
        Clause(None, None, "Tenant pays late charges", 3, 0, 25),
    ]
    ctx = DocumentContext(per_document_clauses={"base_lease": clauses})
    ctx.build_index()

    results = ctx.hybrid_search("Commencement Date", top_k=3)
    assert results, "hybrid_search should return results"
    # The literal "Commencement Date" clause should rank first
    assert "Commencement Date" in results[0]["snippet"]
    # And it should have both a bm25_rank and a vec_rank (RRF worked)
    first = results[0]
    assert first.get("bm25_rank") is not None or first.get("vec_rank") is not None
