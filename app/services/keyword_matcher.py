"""Keyword-based clause matcher.

For each field, the BRD supplies 1+ anchor keywords (e.g. CAM → "Operating
Expenses"). This service scans the segmented clauses for those anchors —
exact + fuzzy — and returns a ranked list of candidate clauses per field,
which the LLM extractor then drills into.
"""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from app.core.reference_data import FieldConfig
from app.services.ocr import Clause


# Minimum fuzzy score to accept a keyword hit (0-100)
FUZZY_THRESHOLD = 85
# How many top candidates to return per field
TOP_K = 5


@dataclass
class ClauseMatch:
    clause: Clause
    score: float
    matched_keyword: str


def find_candidate_clauses(field: FieldConfig, clauses: list[Clause]) -> list[ClauseMatch]:
    """Return the top-K clauses most likely to contain data for `field`."""
    if not field.keywords:
        return []

    matches: list[ClauseMatch] = []
    # Lower-cased keywords for efficient exact search
    keywords_lc = [(kw, kw.lower()) for kw in field.keywords]

    for clause in clauses:
        text_lc = clause.text.lower()
        heading_lc = (clause.heading or "").lower()

        best_score = 0.0
        best_kw = ""

        for kw, kw_lc in keywords_lc:
            # 1) Exact substring in heading is the strongest signal
            if kw_lc in heading_lc:
                score = 100.0 + 20.0  # boost: heading hit
            elif kw_lc in text_lc:
                # 2) Exact substring in body
                score = 100.0
            else:
                # 3) Fuzzy on heading, else fuzzy on first 500 chars of body
                target = heading_lc or text_lc[:500]
                score = fuzz.partial_ratio(kw_lc, target)
                if score < FUZZY_THRESHOLD:
                    continue

            if score > best_score:
                best_score = score
                best_kw = kw

        if best_score > 0:
            matches.append(ClauseMatch(clause=clause, score=best_score, matched_keyword=best_kw))

    # Sort by score desc, keep top K
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches[:TOP_K]
