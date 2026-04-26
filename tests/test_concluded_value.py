"""Test the concluded-value precedence rules + citation/confidence propagation."""
from datetime import datetime

from app.services.concluded_value import (
    DocumentValueInput, compute_concluded, confidence_band,
)


def _di(doc_id, label, doc_type, order, date, value,
        confidence=0.9, page=1, clause="1.0", clause_text="clause context"):
    return DocumentValueInput(
        document_id=doc_id,
        document_label=label,
        document_type=doc_type,
        document_order=order,
        effective_date=datetime.fromisoformat(date) if date else None,
        value=value,
        confidence=confidence,
        page_number=page,
        clause_number=clause,
        clause_text=clause_text,
    )


def test_override_wins_with_confidence_1():
    docs = [
        _di("d0", "Base Lease", "base_lease", 0, "2021-01-01", "5000"),
        _di("d1", "Amendment 1", "amendment", 1, "2023-01-01", "6000"),
    ]
    r = compute_concluded(docs, override_value="9999", override_present=True)
    assert r.concluded_value == "9999"
    assert r.concluded_source == "override"
    assert r.confidence == 1.0
    assert r.source_document_label == "Override"


def test_latest_amendment_citation_travels_through():
    docs = [
        _di("d0", "Base Lease", "base_lease", 0, "2021-01-01", "5000", page=3, clause="3.1"),
        _di("d1", "Amendment 1", "amendment", 1, "2022-01-01", "6000", page=2, clause="2.a"),
        _di("d2", "Amendment 2", "amendment", 2, "2024-01-01", "8000",
            confidence=0.85, page=7, clause="5.2", clause_text="Rent is hereby amended to $8,000..."),
    ]
    r = compute_concluded(docs, override_value=None, override_present=False)
    assert r.concluded_value == "8000"
    assert r.concluded_source == "amendment_2"
    assert r.source_document_id == "d2"
    assert r.source_document_label == "Amendment 2"
    assert r.page_number == 7
    assert r.clause_number == "5.2"
    assert r.confidence == 0.85
    assert "$8,000" in r.clause_text


def test_base_lease_fallback_brings_citation():
    docs = [
        _di("d0", "Base Lease", "base_lease", 0, "2021-01-01", "5000",
            confidence=0.92, page=11, clause="4.1"),
        _di("d1", "Amendment 1", "amendment", 1, "2022-01-01", "None"),
    ]
    r = compute_concluded(docs, override_value=None, override_present=False)
    assert r.concluded_source == "base_lease"
    assert r.page_number == 11
    assert r.clause_number == "4.1"
    assert r.confidence == 0.92


def test_none_result_has_zero_confidence():
    docs = [
        _di("d0", "Base Lease", "base_lease", 0, "2021-01-01", None,
            confidence=0.0, clause_text=None),
    ]
    r = compute_concluded(docs, override_value=None, override_present=False)
    assert r.concluded_value == "None"
    assert r.concluded_source == "none"
    assert r.confidence == 0.0


def test_none_result_still_surfaces_best_fallback_clause():
    """When nothing is extracted but a doc has a clause_text, surface it."""
    docs = [
        _di("d0", "Base Lease", "base_lease", 0, "2021-01-01", None,
            confidence=0.2, page=9, clause_text="Possibly-related clause text"),
    ]
    r = compute_concluded(docs, override_value=None, override_present=False)
    assert r.concluded_value == "None"
    assert r.page_number == 9
    assert r.clause_text == "Possibly-related clause text"


def test_confidence_bands():
    assert confidence_band(0.95) == "high"
    assert confidence_band(0.80) == "high"
    assert confidence_band(0.65) == "medium"
    assert confidence_band(0.50) == "medium"
    assert confidence_band(0.30) == "low"
    assert confidence_band(0.0) == "none"
