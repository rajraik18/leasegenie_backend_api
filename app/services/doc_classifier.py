"""Document-type and property-type classification.

Runs BEFORE the specialists to:
    (1) distinguish base leases from amendments and subleases;
    (2) infer property type (Retail/Industrial/Office/Mixed-Use) so the
        applicability gate can correctly filter retail-only playbooks like
        advertisement, marketing, reporting_of_gross_sales, sales_kick_out.

Cheap, deterministic, keyword-based. No LLM required. Runs on the first
~5 pages of the document since type signals typically appear in the
face page or recitals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DocType = Literal["base_lease", "amendment", "sublease", "guaranty", "unknown"]
PropertyType = Literal["Retail", "Industrial", "Office", "Mixed-Use", "Unknown"]


# ---------------------------------------------------------------------------
# Signal dictionaries — tuned against the 17-doc corpus
# ---------------------------------------------------------------------------

_DOC_TYPE_SIGNALS: dict[DocType, list[str]] = {
    "sublease": [
        r"\bsublease\s+agreement\b",
        r"\bsub-\s*lease\b",
        r"this\s+sublease",
        r"sublandlord",
        r"sublessor", r"sublessee",
    ],
    "amendment": [
        r"\b(?:first|second|third|fourth|fifth|\d+(?:st|nd|rd|th))\s+amendment\b",
        r"amendment\s+to\s+lease",
        r"amendment\s+of\s+lease",
        r"lease\s+amendment",
        r"this\s+amendment",
        r"\bextension\s+agreement\b",
        r"\brenewal\s+agreement\b",
    ],
    "guaranty": [
        r"\bguaranty\s+of\s+lease",
        r"\blease\s+guaranty",
        r"\bguarantor\b.{0,30}\bhereby\s+(?:absolutely|unconditionally)\s+guarantees",
    ],
    "base_lease": [
        r"lease\s+agreement",
        r"\[net\s+lease\]",
        r"gross\s+lease",
        r"this\s+lease\s+(?:agreement\s+)?is\s+made",
        r"landlord\s+(?:and|hereby)\s+(?:leases|demises)",
        r"face\s+page",
    ],
}


# Property-type signals — ordered by precedence. The most specific match wins.
_PROPERTY_SIGNALS: dict[PropertyType, list[str]] = {
    "Retail": [
        r"\bshopping\s+center\b",
        r"\bretail\s+(?:center|premises|store|space|lease)\b",
        r"\bpercentage\s+rent\b",
        r"\bgross\s+sales\b",
        r"\bmall\b",
        r"\bexclusive\s+use\b",
        r"\bkiosk\b",
        r"\banchor\s+tenant\b",
        r"\bco-?tenancy\b",
        r"\bfood\s+court\b",
        r"\boutlet\s+(?:center|mall)\b",
    ],
    "Industrial": [
        r"\bdistribution\s+(?:center|uses?|space|warehouse|facility)\b",
        r"\bwarehouse\b",                    # bare match — catches "warehouse and distribution"
        r"\bindustrial\s+(?:park|premises|building|space|uses?|lease)\b",
        r"\bdock\s+(?:doors?|levelers?|bumpers?|plates?|equipment)\b",
        r"\btruck\s+doors?\b",
        r"\btruck\s+courts?\b",
        r"\brail\s+spur\b",
        r"\bloading\s+(?:docks?|areas?|bay)\b",
        r"\bmanufacturing\s+(?:space|facility|plant|uses?)\b",
        r"\bcold\s+storage\b",
        r"\bflex\s+space\b",
        r"\bbulk\s+warehouse\b",
        r"\bracking\b",                       # strong industrial signal
        r"\bforklifts?\b",
    ],
    "Office": [
        r"\boffice\s+(?:building|park|premises|space|lease|tower|complex|suite)\b",
        r"\bexecutive\s+suite\b",
        r"\bclass\s+[abc]\s+office\b",
        r"\bmedical\s+office\b",
        r"\bcorporate\s+office\b",
        r"\bprofessional\s+office\b",
        r"\brentable\s+area.{0,50}office\s+space\b",
    ],
    "Mixed-Use": [
        r"\bmixed-?\s*use\b",
        r"\bmulti-?\s*(?:tenant|use)\s+(?:building|project|property)\b",
    ],
}


@dataclass
class ClassificationResult:
    document_type: DocType
    property_type: PropertyType
    confidence: float                       # 0.0–1.0
    signals: dict[str, list[str]]           # category → matched keyword list
    reasoning: str


def _count_matches(text: str, patterns: list[str]) -> tuple[int, list[str]]:
    """Return (total matches, matched pattern list)."""
    total = 0
    matched: list[str] = []
    for pat in patterns:
        found = re.findall(pat, text, flags=re.IGNORECASE)
        if found:
            total += len(found)
            matched.append(pat)
    return total, matched


def classify_document(
    text: str,
    *,
    sample_chars: int = 15000,
    filename_hint: str | None = None,
) -> ClassificationResult:
    """Classify a document's type and property type from its raw text.

    Parameters
    ----------
    text: str
        The full extracted text of the document.
    sample_chars: int
        Upper bound on how much text to scan (default: first ~15 pages).
    filename_hint: str
        Optional filename; "- Am." style filenames are a strong amendment signal.
    """
    signals: dict[str, list[str]] = {}
    sample = text[:sample_chars].lower() if len(text) > sample_chars else text.lower()

    # --------------- Document type ---------------
    # Score each type
    type_scores: dict[DocType, int] = {}
    for doc_type, patterns in _DOC_TYPE_SIGNALS.items():
        count, matched = _count_matches(sample, patterns)
        type_scores[doc_type] = count
        if matched:
            signals[f"doc_type:{doc_type}"] = matched

    # Filename hint: "Sample 15 - Am." → amendment
    if filename_hint:
        fn = filename_hint.lower()
        if re.search(r"\b(?:am\.?|amend|amendment)\b", fn):
            type_scores["amendment"] = type_scores.get("amendment", 0) + 3
            signals.setdefault("doc_type:amendment", []).append(f"filename:{filename_hint}")
        elif re.search(r"sublease", fn):
            type_scores["sublease"] = type_scores.get("sublease", 0) + 3

    # Pick the best type. Tie-break: amendment > sublease > base_lease.
    # But amendment and sublease need strong enough signal to win.
    best_type: DocType = "base_lease"
    best_score = type_scores.get("base_lease", 0)
    # Amendments / subleases / guaranty need score >=2 to override base lease
    for special in ("amendment", "sublease", "guaranty"):
        if type_scores.get(special, 0) >= 2:
            if type_scores[special] > best_score:
                best_type = special  # type: ignore
                best_score = type_scores[special]
    if best_score == 0:
        best_type = "unknown"

    # --------------- Property type ---------------
    prop_scores: dict[PropertyType, int] = {}
    for prop, patterns in _PROPERTY_SIGNALS.items():
        count, matched = _count_matches(sample, patterns)
        prop_scores[prop] = count
        if matched:
            signals[f"property:{prop}"] = matched

    # Mixed-Use wins only if (a) explicit mixed-use signal OR (b) 2+ other types all score high
    best_prop: PropertyType = "Unknown"
    best_prop_score = 0
    for prop, score in prop_scores.items():
        if score > best_prop_score:
            best_prop_score = score
            best_prop = prop

    # Mixed-use override: if both Retail and Office each score >=2, mark Mixed-Use
    high_scoring = [p for p, s in prop_scores.items() if s >= 2 and p != "Mixed-Use"]
    if len(high_scoring) >= 2 and prop_scores.get("Mixed-Use", 0) > 0:
        best_prop = "Mixed-Use"
    elif best_prop_score == 0:
        best_prop = "Unknown"

    # --------------- Confidence ---------------
    # Doc-type confidence: score / 5 capped at 1.0
    doc_conf = min(1.0, best_score / 5.0) if best_score > 0 else 0.1
    prop_conf = min(1.0, best_prop_score / 5.0) if best_prop_score > 0 else 0.1
    overall_conf = round((doc_conf + prop_conf) / 2.0, 3)

    reasoning_parts = [
        f"document_type={best_type} (score={best_score})",
        f"property_type={best_prop} (score={best_prop_score})",
    ]
    if signals:
        reasoning_parts.append(f"signals_found={len(signals)} categories")
    reasoning = "; ".join(reasoning_parts)

    return ClassificationResult(
        document_type=best_type,
        property_type=best_prop,
        confidence=overall_conf,
        signals=signals,
        reasoning=reasoning,
    )
