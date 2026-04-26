"""Concluded-value engine.

BRD rule (User Story #4):
    Concluded Value = manual override  (if present)
                    else latest amendment value (by effective date)
                    else base lease value
                    else "None"

The caller supplies per-document `DocumentValueInput` rows (each carrying the
value AND its confidence + citation metadata) plus any override, and this
module picks the winner and propagates the winning doc's citation so the
API can expose *where* the concluded value came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DocumentValueInput:
    document_id: str
    document_label: str                # "Base Lease" | "Amendment 1"
    document_type: str                 # "base_lease" | "amendment"
    document_order: int                # 0 = base, 1..7 = amendments
    effective_date: datetime | None
    value: str | None
    # Citation + confidence of this document's extraction
    confidence: float = 0.0
    page_number: int | None = None
    clause_number: str | None = None
    clause_text: str | None = None


@dataclass
class ConcludedResult:
    concluded_value: str | None
    concluded_source: str              # "override" | "base_lease" | "amendment_N" | "none"
    confidence: float = 0.0
    source_document_id: str | None = None
    source_document_label: str | None = None
    page_number: int | None = None
    clause_number: str | None = None
    clause_text: str | None = None


def compute_concluded(
    per_doc_values: list[DocumentValueInput],
    override_value: str | None,
    override_present: bool,
) -> ConcludedResult:
    # 1. Override wins outright if present
    if override_present:
        return ConcludedResult(
            concluded_value=override_value,
            concluded_source="override",
            confidence=1.0,                   # explicit user choice
            source_document_label="Override",
        )

    # 2. Most recent amendment with a real value
    amendments = [
        dv for dv in per_doc_values
        if dv.document_type == "amendment" and _has_value(dv.value)
    ]
    amendments.sort(
        key=lambda d: (d.effective_date or datetime.min, d.document_order),
        reverse=True,
    )
    if amendments:
        top = amendments[0]
        return ConcludedResult(
            concluded_value=top.value,
            concluded_source=f"amendment_{top.document_order}",
            confidence=top.confidence,
            source_document_id=top.document_id,
            source_document_label=top.document_label,
            page_number=top.page_number,
            clause_number=top.clause_number,
            clause_text=top.clause_text,
        )

    # 3. Base lease fallback
    base_docs = [
        dv for dv in per_doc_values
        if dv.document_type == "base_lease" and _has_value(dv.value)
    ]
    if base_docs:
        base_docs.sort(key=lambda d: d.document_order)
        top = base_docs[0]
        return ConcludedResult(
            concluded_value=top.value,
            concluded_source="base_lease",
            confidence=top.confidence,
            source_document_id=top.document_id,
            source_document_label=top.document_label,
            page_number=top.page_number,
            clause_number=top.clause_number,
            clause_text=top.clause_text,
        )

    # 4. Nothing usable. BRD: "None", never blank. Still try to surface a
    # citation from whichever document had the best fallback clause_text.
    best_fallback = None
    for dv in per_doc_values:
        if dv.clause_text:
            if best_fallback is None or dv.confidence > best_fallback.confidence:
                best_fallback = dv
    if best_fallback is not None:
        return ConcludedResult(
            concluded_value="None",
            concluded_source="none",
            confidence=0.0,
            source_document_id=best_fallback.document_id,
            source_document_label=best_fallback.document_label,
            page_number=best_fallback.page_number,
            clause_number=best_fallback.clause_number,
            clause_text=best_fallback.clause_text,
        )

    return ConcludedResult(
        concluded_value="None",
        concluded_source="none",
        confidence=0.0,
    )


def confidence_band(confidence: float) -> str:
    """Map a float confidence into a user-facing band."""
    if confidence <= 0:
        return "none"
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _has_value(v: str | None) -> bool:
    if v is None:
        return False
    s = v.strip()
    return bool(s) and s.lower() != "none"
