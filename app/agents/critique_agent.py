"""Critique agent — hallucination defense.

After the playbook executor produces a (value, clause_text) pair, the
critique agent makes a narrow second LLM call asking:

    Given this extracted value and this clause text, does the clause
    actually support the value?

If the critique says NO, the executor demotes the result:
    - confidence /= 2
    - needs_review = True
    - red_flag appended explaining the mismatch
    - if a corrected value was supplied AND the critique supports it, we
      optionally swap to the corrected value (behind a conservatism flag)

The critique runs at temperature=0 on a minimal prompt — designed to be
cheap (~1-2k prompt tokens, <100 tokens out) and to catch three specific
failure modes observed in LLM extraction:

    1. Number hallucination: the LLM picks a number from a nearby clause
       that was retrieved but belongs to a different field.
    2. Direction inversion: "Landlord pays" extracted when the clause
       actually says "Tenant pays" (common confusion).
    3. Date drift: extracted date differs by a day/month from the clause.

This is NOT a full re-extraction — it's a cheap binary check. It is
meant to be run only on high-stakes fields (currency, dates, party
names) where a wrong answer downstream is costly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


CRITIQUE_SYSTEM = """You are LeaseGenie's verification auditor.

You are given (a) an extracted VALUE, (b) the lease CLAUSE TEXT it was supposedly extracted from, and (c) the FIELD being extracted. Your job is to verify whether the clause ACTUALLY supports the extracted value — or whether the extraction hallucinated, picked the wrong number, confused parties, or otherwise drifted.

Reply ONLY in this JSON shape, no prose:
{
  "supports": true | false,
  "corrected_value": "<if supports=false and the clause CLEARLY contains a different correct value for this field, give it here; otherwise null>",
  "failure_mode": "none" | "hallucinated" | "wrong_number" | "wrong_party" | "wrong_date" | "partial" | "ambiguous",
  "reasoning": "<one short sentence>"
}

Rules:
- Be strict. If the clause does not explicitly state the value (even with small rewording), reply supports=false.
- Numbers: the exact number must appear in the clause. $100 extracted for $1,000 = wrong_number.
- Parties: if the clause says "Tenant shall pay" and the extraction attributes payment to Landlord = wrong_party.
- Only supply corrected_value when the clause UNAMBIGUOUSLY contains the right value for this field. Otherwise null.
- If the clause is tangentially relevant but does not directly answer the field, supports=false / failure_mode=ambiguous."""


CRITIQUE_USER = """FIELD: {field_name}  (category: {category})
FIELD DESCRIPTION: {overview}

EXTRACTED VALUE: {value}

CLAUSE TEXT (from {source_doc}, page {page}):
{clause}

Verify whether the clause supports the extracted value. Reply in the specified JSON shape."""


# Fields that benefit most from critique (high-stakes, concrete answers).
# Prose/description fields are skipped — critique can't reliably judge them.
_CRITIQUE_WORTHY_OUTPUT_TYPES = {
    "currency", "number", "numeric", "amount", "money",
    "date", "percentage", "percent", "integer",
}
_CRITIQUE_WORTHY_FIELDS = {
    "tenant_name", "landlord_name", "lease_guarantor",
    "original_lease_commencement_date", "term_commencement_date",
    "rent_commencement_date", "lease_expiration_date",
    "leased_rsf", "security_deposit", "pro_rata",
    "annual_base_rent", "allowance",
    "breakpoint", "percentage_rent", "late_payment",
}


@dataclass
class CritiqueResult:
    supports: bool
    corrected_value: str | None
    failure_mode: str           # e.g. "none", "wrong_number", ...
    reasoning: str
    ran: bool = True            # False when we skipped or the LLM failed


def should_critique(field_id: str, output_type: str) -> bool:
    """Decide whether this (field_id, output_type) warrants a critique pass."""
    if field_id in _CRITIQUE_WORTHY_FIELDS:
        return True
    ot = (output_type or "").strip().lower()
    if any(t in ot for t in _CRITIQUE_WORTHY_OUTPUT_TYPES):
        return True
    return False


def critique(
    client,                         # OllamaAgentClient-like, with chat_json()
    *,
    field_id: str,
    field_name: str,
    category: str,
    overview: str,
    value: str,
    clause_text: str,
    source_doc: str | None,
    page: int | None,
) -> CritiqueResult:
    """Run one critique pass. Never raises — returns a "ran=False" result
    on any failure so the caller can continue."""
    if not value or value.lower() == "none":
        return CritiqueResult(
            supports=True, corrected_value=None, failure_mode="none",
            reasoning="No value to critique", ran=False,
        )
    if not clause_text:
        return CritiqueResult(
            supports=False, corrected_value=None, failure_mode="ambiguous",
            reasoning="No clause text was cited", ran=False,
        )

    user = CRITIQUE_USER.format(
        field_name=field_name or field_id,
        category=category or "?",
        overview=(overview or "?")[:500],
        value=value[:400],
        source_doc=source_doc or "?",
        page=page if page is not None else "?",
        clause=clause_text[:2500],
    )

    try:
        data = client.chat_json(
            messages=[
                {"role": "system", "content": CRITIQUE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("critique LLM call failed for %s: %s", field_id, exc)
        return CritiqueResult(
            supports=True, corrected_value=None, failure_mode="none",
            reasoning=f"Critique LLM error: {exc}", ran=False,
        )

    if not data:
        return CritiqueResult(
            supports=True, corrected_value=None, failure_mode="none",
            reasoning="Empty critique response", ran=False,
        )

    supports = bool(data.get("supports", True))
    corrected = data.get("corrected_value")
    if corrected is not None and not str(corrected).strip():
        corrected = None
    failure_mode = str(data.get("failure_mode") or "none")
    reasoning = str(data.get("reasoning") or "")

    return CritiqueResult(
        supports=supports,
        corrected_value=corrected,
        failure_mode=failure_mode,
        reasoning=reasoning,
        ran=True,
    )


def apply_critique_to_result(
    result,                          # PlaybookResult-like (has value, confidence, needs_review, red_flags)
    critique_result: CritiqueResult,
    *,
    apply_corrections: bool = False,
) -> None:
    """Mutate `result` in-place based on the critique outcome.

    Policy:
        - supports=True:     no-op. Optionally a small confidence boost
          (capped at 0.95) to reflect that a second model agreed.
        - supports=False:    halve confidence, set needs_review=True,
          append a red_flag describing the failure_mode. If
          `apply_corrections=True` AND the critique supplied a
          corrected_value, swap to it (with a note).
    """
    if not critique_result.ran:
        return

    if critique_result.supports:
        # Small boost for corroborated extractions
        try:
            result.confidence = min(0.95, (result.confidence or 0.0) + 0.05)
        except Exception:
            pass
        return

    # Not supported — demote
    prev_conf = result.confidence or 0.0
    result.confidence = max(0.1, prev_conf * 0.5)
    result.needs_review = True

    flag_text = (
        f"Critique disagrees ({critique_result.failure_mode}): "
        f"{critique_result.reasoning[:200]}"
    )
    try:
        if flag_text not in (result.red_flags or []):
            result.red_flags = list(result.red_flags or []) + [flag_text]
    except Exception:
        pass

    if apply_corrections and critique_result.corrected_value:
        orig = result.value
        result.value = critique_result.corrected_value
        correction_note = f"Value auto-corrected by critique: {orig!r} → {critique_result.corrected_value!r}"
        try:
            result.red_flags = list(result.red_flags or []) + [correction_note]
        except Exception:
            pass
