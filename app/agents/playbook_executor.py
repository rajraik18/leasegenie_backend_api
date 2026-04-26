"""Playbook executor.

Strict decision-tree runner. For each PlaybookQuestion, asks the LLM to
answer YES/NO + extract a value if appropriate, then follows the branches
*in code* — never lets the LLM drive flow control.

The executor:
    1. Starts at Q1.
    2. Loads the relevant clause candidates via tools (Summary pages first,
       then body, then amendments, per the question's search_scope).
    3. Sends the LLM a narrow JSON-mode prompt: "answer YES/NO, extract if
       applicable". Single call per question, no tool loop.
    4. Picks yes_branch or no_branch per the declared ActionType.
    5. On EXTRACT actions, stashes the value (may aggregate across questions).
    6. Terminates on FINALIZE / RECORD_NONE / RECORD_LITERAL / FLAG_REVIEW, or
       when a branch goes somewhere that doesn't exist, or when a step budget
       is exhausted.

Deterministic post-processing:
    * Currency outputs get cleaned ($1,234.56 → 1234.56)
    * "Monthly × 12" arithmetic is applied if the extracted value is explicitly
      tagged as monthly by the LLM
    * Never-blank rule: if no value was extracted but an extraction-required
      branch was reached, record "None" with the best clause as fallback
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.ollama_client import OllamaAgentClient
from app.agents.playbooks.schema import (
    ActionType, Playbook, PlaybookAction, PlaybookQuestion, SearchScope,
)
from app.agents.tools import DocumentContext
from app.services.ocr import Clause

logger = logging.getLogger(__name__)


MAX_STEPS = 15                              # safety ceiling on playbook walk


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class StepTrace:
    """One step in the playbook execution trace — useful for debugging / audit."""
    qid: str
    condition_type: str
    question_text: str
    llm_answer: str                # "YES" | "NO" | "UNKNOWN"
    llm_value: str | None          # extracted value if any
    llm_reasoning: str | None
    branch_taken: str              # "yes" | "no" | "none"
    next_action: str               # serialized ActionType


@dataclass
class PlaybookResult:
    field_id: str
    value: str                                 # final normalized value ("None" if nothing)
    raw_value: str | None                      # verbatim snippet
    confidence: float
    source_doc: str | None
    page_number: int | None
    clause_number: str | None
    clause_text: str | None
    output_type: str
    condition_type_taken: str | None = None           # which branch's condition applied
    red_flags: list[str] = field(default_factory=list)
    needs_review: bool = False
    trace: list[StepTrace] = field(default_factory=list)
    aggregated_extracts: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-question LLM prompt — narrow, JSON-only
# ---------------------------------------------------------------------------

PER_QUESTION_SYSTEM = """You are LeaseGenie's extraction analyst. You answer ONE yes/no question about a lease clause, and if yes, you extract the specific value.

Reply ONLY in this JSON shape, no prose:
{
  "answer": "YES" | "NO" | "UNKNOWN",
  "value": "<extracted string, or null>",
  "raw_snippet": "<verbatim text from the clause supporting the answer, or null>",
  "is_monthly": <true|false — true ONLY for rent amounts explicitly described as per-month>,
  "page_number": <int or null>,
  "clause_number": "<string or null>",
  "reasoning": "<one short sentence>"
}

Rules:
- Answer STRICTLY about the one question asked. Do not answer neighbouring concerns.
- UNKNOWN only if the clauses are ambiguous or absent. Otherwise pick YES/NO.
- Always ground the value in the provided clauses. Do not invent amounts, dates, or names.
- For currency, extract the raw number with its unit (e.g. "$10,000/month" or "$120,000 per annum").
- Set is_monthly=true only if the clause explicitly calls the amount monthly."""


PER_QUESTION_USER = """FIELD: {field_name}  (category: {category})
QUESTION_ID: {qid}
QUESTION: {question}
CONDITION_TYPE: {condition_type}
EXTRACTION_HINT: {hint}
OUTPUT_TYPE: {output}
SEARCH_SCOPE: {scope}

{few_shot_block}CANDIDATE CLAUSES (retrieved by search_scope):
{clauses}

Previously-extracted facts for this field (may inform your answer):
{prior_extracts}

Now answer the ONE question above in the specified JSON shape."""


# Few-shot block wrapper (only included if the playbook defines examples)
FEW_SHOT_TEMPLATE = """EXAMPLES (for calibration — study these before answering):
{examples}

"""


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------

class PlaybookExecutor:
    """Runs a single playbook against a single document context and a target
    document label (e.g. 'base_lease' or 'amendment_3')."""

    def __init__(self, client: OllamaAgentClient):
        self.client = client

    def run(
        self,
        playbook: Playbook,
        ctx: DocumentContext,
        doc_label: str,
        shared_facts: dict[str, Any] | None = None,
    ) -> PlaybookResult:
        shared_facts = shared_facts or {}

        first = playbook.first_question()
        if first is None:
            return _empty_result(playbook.field_id, playbook.output_type)

        current_qid = first.id
        steps = 0

        aggregated_extracts: list[dict[str, Any]] = []
        red_flags: list[str] = []
        condition_type_taken: str | None = None
        needs_review = False

        trace: list[StepTrace] = []

        # We track the "best" extraction across the walk — the one with the
        # highest-priority YES answer + non-null value.
        best_value: str | None = None
        best_raw: str | None = None
        best_page: int | None = None
        best_clause: str | None = None
        best_clause_text: str | None = None
        best_source: str | None = None
        is_monthly: bool = False

        while current_qid and steps < MAX_STEPS:
            steps += 1
            q = playbook.question(current_qid)
            if q is None:
                logger.debug("playbook %s: question %s not found — terminating", playbook.field_id, current_qid)
                break

            # Load candidate clauses for this step using the question's search scope
            clauses_block, retrieved_hits = self._gather_clauses(playbook, q, ctx, doc_label)

            # Ask the LLM the single yes/no + extract question
            llm = self._ask_question(q, playbook, clauses_block, aggregated_extracts)
            answer = (llm.get("answer") or "UNKNOWN").upper()

            # Record the red flag if the question had one and the answer triggers it
            if q.red_flag and answer == "YES":
                red_flags.append(q.red_flag)

            # Fill in page_number / clause_number from the top retrieved clause
            # when the LLM left them blank — essential for citation completeness.
            llm_page = llm.get("page_number")
            llm_clause = llm.get("clause_number")
            if (llm_page is None or llm_clause is None) and retrieved_hits:
                top = retrieved_hits[0]
                if llm_page is None:
                    llm["page_number"] = top.get("page")
                if llm_clause is None:
                    llm["clause_number"] = top.get("clause_number")

            trace_step = StepTrace(
                qid=q.id,
                condition_type=q.condition_type,
                question_text=q.question_text,
                llm_answer=answer,
                llm_value=llm.get("value"),
                llm_reasoning=llm.get("reasoning"),
                branch_taken="",
                next_action="",
            )

            # If the LLM extracted a value, stash it (regardless of branch)
            if llm.get("value"):
                aggregated_extracts.append({
                    "qid": q.id,
                    "condition_type": q.condition_type,
                    "value": llm["value"],
                    "raw": llm.get("raw_snippet"),
                    "page": llm.get("page_number"),
                    "clause": llm.get("clause_number"),
                    "is_monthly": bool(llm.get("is_monthly")),
                    "output_type": q.output_type,
                })
                # Track best (first confirmed YES with a value wins, since
                # playbook order = priority = preferred branch)
                if answer == "YES" and best_value is None:
                    best_value = llm["value"]
                    best_raw = llm.get("raw_snippet")
                    best_page = llm.get("page_number")
                    best_clause = llm.get("clause_number")
                    best_source = doc_label
                    is_monthly = bool(llm.get("is_monthly"))
                    condition_type_taken = q.condition_type
                    # best clause_text: prefer LLM raw_snippet; if missing,
                    # fall back to the top retrieved clause so the API always
                    # has a citation context to show.
                    best_clause_text = (
                        llm.get("raw_snippet")
                        or (retrieved_hits[0].get("snippet") if retrieved_hits else None)
                    )

            # Decide next action based on branch
            action: PlaybookAction | None = None
            if answer == "YES":
                action = q.yes_branch
                trace_step.branch_taken = "yes"
            elif answer == "NO":
                action = q.no_branch
                trace_step.branch_taken = "no"
            else:  # UNKNOWN — try YES branch first, fall back to NO
                action = q.yes_branch or q.no_branch
                trace_step.branch_taken = "yes" if q.yes_branch else "no"

            if action is None:
                trace_step.next_action = "terminate(no_branch_defined)"
                trace.append(trace_step)
                break

            trace_step.next_action = action.type.value + (f"→{action.goto}" if action.goto else "")
            trace.append(trace_step)

            # Resolve action
            if action.type == ActionType.GOTO:
                current_qid = action.goto
            elif action.type == ActionType.EXTRACT:
                # Value should already be in aggregated_extracts if LLM provided one
                current_qid = action.goto  # may be None → terminate
            elif action.type == ActionType.FINALIZE:
                break
            elif action.type == ActionType.FLAG_REVIEW:
                needs_review = True
                break
            elif action.type == ActionType.RECORD_NONE:
                if best_value is None:
                    best_value = "None"
                break
            elif action.type == ActionType.RECORD_LITERAL:
                best_value = action.literal or "None"
                condition_type_taken = q.condition_type
                break
            else:
                break

        # Post-processing
        final_value = best_value if best_value is not None else "None"
        final_value = self._normalize_value(
            final_value, playbook.output_type, is_monthly
        )

        confidence = self._confidence(best_value, best_raw, best_clause_text, needs_review)

        # Never-blank rule: even when no value, try to return the best
        # candidate clause as fallback
        if final_value.lower() == "none" and best_clause_text is None:
            fallback = ctx.search(playbook.field_name, top_k=1, doc_filter=doc_label)
            if fallback:
                top = fallback[0]
                best_clause_text = top["snippet"]
                if best_page is None:
                    best_page = top["page"]
                if best_clause is None:
                    best_clause = top["clause_number"]
                best_source = best_source or top["doc"]

        result = PlaybookResult(
            field_id=playbook.field_id,
            value=final_value,
            raw_value=best_raw,
            confidence=confidence,
            source_doc=best_source,
            page_number=best_page,
            clause_number=best_clause,
            clause_text=best_clause_text,
            output_type=playbook.output_type,
            condition_type_taken=condition_type_taken,
            red_flags=red_flags,
            needs_review=needs_review,
            trace=trace,
            aggregated_extracts=aggregated_extracts,
        )

        # Critique pass — cheap hallucination defense for high-stakes fields.
        # Only fires when we actually extracted a value AND the field is worth
        # double-checking (currency/date/party-name per should_critique).
        try:
            from app.agents.critique_agent import (
                apply_critique_to_result, critique, should_critique,
            )
            if (
                result.value
                and result.value.lower() != "none"
                and result.clause_text
                and should_critique(playbook.field_id, playbook.output_type)
            ):
                cr = critique(
                    self.client,
                    field_id=playbook.field_id,
                    field_name=playbook.field_name,
                    category=playbook.category,
                    overview=playbook.overview or "",
                    value=result.value,
                    clause_text=result.clause_text,
                    source_doc=result.source_doc,
                    page=result.page_number,
                )
                apply_critique_to_result(result, cr, apply_corrections=False)
                if cr.ran:
                    logger.info(
                        "critique[%s/%s] supports=%s failure_mode=%s",
                        playbook.field_id, doc_label, cr.supports, cr.failure_mode,
                    )
        except Exception as exc:
            logger.debug("critique pass skipped for %s: %s", playbook.field_id, exc)

        return result

    # ------------------------------------------------------------------
    # Clause retrieval per search scope
    # ------------------------------------------------------------------

    def _gather_clauses(
        self,
        playbook: Playbook,
        q: PlaybookQuestion,
        ctx: DocumentContext,
        doc_label: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return (formatted_block, raw_hits). The formatted block is what
        we show the LLM; the raw hits are used for citation fallback when
        the LLM doesn't supply page/clause numbers itself."""
        queries = self._queries_for(playbook, q)

        # Search within the target document first, widen to full corpus if empty
        hits: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        # Prefer hybrid_search (BM25+vector+RRF); falls back to BM25 inside
        search_fn = getattr(ctx, "hybrid_search", None) or ctx.search
        for query in queries:
            # Scope-aware search: if SUMMARY scope, bias query with "summary"
            if q.search_scope == SearchScope.SUMMARY:
                query = f"summary {query}"
            elif q.search_scope == SearchScope.DEFINITIONS:
                query = f"definitions {query}"
            # Retrieve a deeper pool here (top_k=10) so the reranker has
            # enough candidates to meaningfully reorder. Final cap is below.
            for r in search_fn(query, top_k=10, doc_filter=doc_label):
                if r["_clause_ref"] in seen_refs:
                    continue
                seen_refs.add(r["_clause_ref"])
                hits.append(r)

        # Cross-encoder rerank (if available) — boosts precision @ top-5
        if hits:
            try:
                from app.services.reranker import rerank as _rerank
                # Use the first query string for the cross-encoder (most specific)
                rerank_query = queries[0] if queries else playbook.field_name
                hits = _rerank(rerank_query, hits, top_k=5)
            except Exception as exc:
                logger.debug("rerank skipped: %s", exc)
                hits = hits[:5]
        else:
            hits = []

        if not hits:
            return ("(no candidate clauses found for this search scope)", [])

        lines = []
        for i, h in enumerate(hits, start=1):
            lines.append(
                f"[#{i}] doc={h['doc']} page={h['page']} clause={h['clause_number'] or '-'} "
                f"heading={h['heading'] or '-'}\n  {h['snippet']}"
            )
        return ("\n\n".join(lines), hits)

    def _queries_for(self, playbook: Playbook, q: PlaybookQuestion) -> list[str]:
        """Produce a small set of search queries tailored to the question."""
        queries: list[str] = []

        # Start with the playbook's field-level keywords (most important)
        queries.append(playbook.field_name)
        for kw in playbook.keywords[:5]:
            queries.append(kw)
        # Question-specific keywords
        for kw in q.keywords[:3]:
            queries.append(kw)
        # Condition-type-derived query
        if q.condition_type:
            queries.append(f"{playbook.field_name} {q.condition_type}")
        return queries[:6]  # cap

    # ------------------------------------------------------------------
    # LLM per-question call
    # ------------------------------------------------------------------

    def _ask_question(
        self,
        q: PlaybookQuestion,
        playbook: Playbook,
        clauses_block: str,
        prior_extracts: list[dict],
    ) -> dict[str, Any]:
        prior_str = "(none)" if not prior_extracts else "\n".join(
            f"  - Q{p['qid']} ({p['condition_type']}): {str(p['value'])[:200]}"
            for p in prior_extracts[-5:]
        )
        few_shot_block = _build_few_shot_block(playbook, q)
        user = PER_QUESTION_USER.format(
            field_name=playbook.field_name,
            category=playbook.category,
            qid=q.id,
            question=q.question_text,
            condition_type=q.condition_type,
            hint=q.extraction_hint or "(none)",
            output=q.output_type,
            scope=q.search_scope.value,
            few_shot_block=few_shot_block,
            clauses=clauses_block[:6000],
            prior_extracts=prior_str,
        )

        # Self-consistency voting: for numeric/date/currency questions, run
        # the LLM N=3 times at small nonzero temperature and take majority vote.
        # For text/binary questions, a single deterministic call is sufficient.
        use_voting = _should_use_voting(q, playbook)

        if use_voting:
            return self._ask_with_voting(q, playbook, user, n_samples=3)

        # Single-shot path (default)
        try:
            data = self.client.chat_json(
                messages=[
                    {"role": "system", "content": PER_QUESTION_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning("per-question call failed for %s/%s: %s", playbook.field_id, q.id, exc)
            return {"answer": "UNKNOWN", "value": None, "reasoning": f"LLM error: {exc}"}
        return data or {"answer": "UNKNOWN"}

    def _ask_with_voting(
        self,
        q: PlaybookQuestion,
        playbook: Playbook,
        user_prompt: str,
        *,
        n_samples: int = 3,
    ) -> dict[str, Any]:
        """Run the same prompt N times at temp=0.3 and majority-vote.

        Voting rules:
            1. If the YES/NO answer disagrees across samples, take the
               majority. Ties default to NO.
            2. Among the samples that picked the winning answer, normalize
               extracted values (canonical currency / date) and take the mode.
            3. Overall confidence is boosted when 3/3 agree, unchanged for
               2/3, and `needs_review` is set when 2/3 disagree on value.
        """
        samples: list[dict] = []
        for i in range(n_samples):
            try:
                data = self.client.chat_json(
                    messages=[
                        {"role": "system", "content": PER_QUESTION_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                    # Small but nonzero — enough to explore alternates, not
                    # so much that we get wildly different numeric answers.
                    temperature=0.3,
                )
                if data:
                    samples.append(data)
            except Exception as exc:
                logger.warning("voting sample %d failed for %s/%s: %s",
                               i, playbook.field_id, q.id, exc)

        if not samples:
            return {"answer": "UNKNOWN", "value": None,
                    "reasoning": "All voting samples failed"}

        # Count YES/NO/UNKNOWN
        answer_counts: dict[str, int] = {}
        for s in samples:
            ans = str(s.get("answer", "UNKNOWN")).upper()
            answer_counts[ans] = answer_counts.get(ans, 0) + 1

        # Majority wins; tie → NO (conservative default)
        winning_answer = max(answer_counts.items(), key=lambda kv: (kv[1], -_ans_priority(kv[0])))[0]
        winning_votes = answer_counts[winning_answer]

        # Among winning samples, normalize and find the mode value
        winning_samples = [s for s in samples if str(s.get("answer", "")).upper() == winning_answer]
        canonical_values: list[str] = []
        for s in winning_samples:
            v = s.get("value")
            if v is None or str(v).strip() == "":
                continue
            cv = _canonicalize_value(str(v), q.output_type)
            if cv:
                canonical_values.append(cv)

        # Pick mode (most-common canonical value)
        value_counts: dict[str, int] = {}
        for cv in canonical_values:
            value_counts[cv] = value_counts.get(cv, 0) + 1

        chosen_value = None
        value_agreement = 0
        if value_counts:
            chosen_value, value_agreement = max(value_counts.items(), key=lambda kv: kv[1])

        # Pick a representative sample's reasoning + raw value
        rep_sample = winning_samples[0] if winning_samples else samples[0]

        # Needs review if samples disagreed on value (2 distinct values from 3 samples)
        needs_review = len(value_counts) > 1 and value_agreement < len(winning_samples)

        # Voting trace logs the chosen extracted value, which may be a
        # rent amount, tenant name, or other PII. Keep at DEBUG so it only
        # appears when an operator opts in via DEBUG=true in .env.
        logger.debug(
            "voting[%s/%s] answers=%s value_agreement=%d/%d chosen=%s%s",
            playbook.field_id, q.id, answer_counts, value_agreement,
            len(winning_samples), chosen_value,
            " NEEDS_REVIEW" if needs_review else "",
        )

        return {
            "answer": winning_answer,
            "value": chosen_value if chosen_value is not None else rep_sample.get("value"),
            "reasoning": rep_sample.get("reasoning"),
            "_voting": {
                "n_samples": len(samples),
                "answer_counts": answer_counts,
                "winning_votes": winning_votes,
                "value_agreement": value_agreement,
                "needs_review": needs_review,
                "all_values": canonical_values,
            },
        }

    # ------------------------------------------------------------------
    # Post-processing: normalize per output_type, apply monthly×12
    # ------------------------------------------------------------------

    def _normalize_value(self, value: str, output_type: str, is_monthly: bool) -> str:
        if value is None:
            return "None"
        v = str(value).strip()
        if not v or v.lower() == "none":
            return "None"

        ot = (output_type or "Text").lower()

        if "currency" in ot or ot == "number" or "numeric" in ot or "amount" in ot:
            num = _extract_number(v)
            if num is not None:
                if is_monthly and "currency" in ot:
                    num = num * 12
                # Return as plain numeric string — no $ or commas
                if num.is_integer():
                    return str(int(num))
                return f"{num:.2f}"
        return v

    def _confidence(self, value: str | None, raw: str | None, clause_text: str | None, needs_review: bool) -> float:
        if needs_review:
            return 0.4
        if value is None or value == "None":
            return 0.1 if clause_text else 0.0
        if raw:
            return 0.9
        if clause_text:
            return 0.7
        return 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[\d,]+\.?\d*")


def _extract_number(s: str) -> float | None:
    """Pull the first number out of '$1,234.56 per month' style strings."""
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _empty_result(field_id: str, output_type: str) -> PlaybookResult:
    return PlaybookResult(
        field_id=field_id,
        value="None",
        raw_value=None,
        confidence=0.0,
        source_doc=None,
        page_number=None,
        clause_number=None,
        clause_text=None,
        output_type=output_type,
    )


# ---------------------------------------------------------------------------
# Few-shot examples support
# ---------------------------------------------------------------------------

def _build_few_shot_block(playbook, question) -> str:
    """If the playbook declares `few_shot_examples`, format them for the prompt.

    Examples are pulled from:
        (a) playbook.few_shot_examples (per-playbook, question-scoped)
        (b) the EXTENDED_FEW_SHOT_LIBRARY (imported from few_shot_library.py)
        (c) the in-file FEW_SHOT_LIBRARY fallback for a handful of key fields
    """
    examples = getattr(playbook, "few_shot_examples", None) or []
    if not examples:
        examples = _merged_library().get(playbook.field_id, [])
    # Filter to examples scoped to this question_id, if any specify a qid
    scoped = [e for e in examples if not e.get("qid") or e["qid"] == question.id]
    if not scoped:
        return ""

    lines = []
    for i, ex in enumerate(scoped[:3], start=1):  # cap at 3 to save context
        lines.append(f"Example {i}:")
        if ex.get("clause"):
            # Trim long clauses
            clause = ex["clause"][:500] + ("…" if len(ex["clause"]) > 500 else "")
            lines.append(f"  CLAUSE: {clause}")
        if ex.get("expected"):
            exp = ex["expected"]
            lines.append(
                "  EXPECTED JSON: {"
                + ", ".join(f'"{k}": {v!r}' for k, v in exp.items())
                + "}"
            )
        if ex.get("note"):
            lines.append(f"  NOTE: {ex['note']}")
    return FEW_SHOT_TEMPLATE.format(examples="\n".join(lines))


# The external library is the authoritative source for most fields; the
# in-file FEW_SHOT_LIBRARY is a hand-tuned override for a few flagship fields.
# Precedence: in-file > external.

_MERGED_CACHE: dict[str, list[dict]] | None = None


def _merged_library() -> dict[str, list[dict]]:
    """Merge the external EXTENDED_FEW_SHOT_LIBRARY with the in-file
    FEW_SHOT_LIBRARY. Cached after first call.
    """
    global _MERGED_CACHE
    if _MERGED_CACHE is not None:
        return _MERGED_CACHE

    merged: dict[str, list[dict]] = {}
    try:
        from app.agents.few_shot_library import EXTENDED_FEW_SHOT_LIBRARY
        merged.update({k: v for k, v in EXTENDED_FEW_SHOT_LIBRARY.items() if v})
    except ImportError:
        pass

    # In-file library takes precedence for any key it defines
    for key, examples in FEW_SHOT_LIBRARY.items():
        if examples:
            merged[key] = examples

    _MERGED_CACHE = merged
    logger.info(
        "Few-shot library loaded: %d fields covered",
        sum(1 for v in merged.values() if v),
    )
    return merged
FEW_SHOT_LIBRARY: dict[str, list[dict]] = {
    # ---- Base-rent family ----
    "annual_base_rent": [
        {
            "qid": "Q1",
            "clause": "ANNUAL BASE RENT: $13.55 per SF\nMONTHLY BASE RENT: $30,823.99",
            "expected": {
                "answer": "YES",
                "value": "$13.55 per SF ($30,823.99/mo)",
                "is_monthly": False,
            },
            "note": "Face Page stated both annual rate per SF and monthly amount.",
        },
        {
            "qid": "Q1",
            "clause": "August 1, 2009 through July 31, 2012 $8,882.60\nAugust 1, 2012 through July 31, 2014 $9,655.00",
            "expected": {
                "answer": "YES",
                "value": "$8,882.60/mo (initial)",
                "is_monthly": True,
            },
            "note": "ProLogis Addendum 1 shows rent schedule in monthly amounts.",
        },
    ],
    # ---- Start dates ----
    "original_lease_commencement_date": [
        {
            "qid": "Q1",
            "clause": "Commencement Date: June 1, 2009",
            "expected": {"answer": "YES", "value": "June 1, 2009"},
            "note": "Face Page Commencement Date field. Always extract verbatim.",
        },
        {
            "qid": "Q1",
            "clause": "1.4. Commencement Date. The commencement date (\"Commencement Date\") of this Lease shall be August 1, 2024.",
            "expected": {"answer": "YES", "value": "August 1, 2024"},
            "note": "HMBP-BCP Section 1.4 style.",
        },
    ],
    # ---- TI allowance ----
    "allowance": [
        {
            "qid": "Q1",
            "clause": "Landlord shall contribute up to a maximum amount of $40,000.00 (the \"TI Allowance\"), toward the initial Tenant Improvements",
            "expected": {"answer": "YES", "value": "$40,000.00"},
            "note": "ProLogis-style Paragraph 12 TI Allowance.",
        },
        {
            "qid": "Q1",
            "clause": "Landlord shall contribute $10,000 which Tenant may, at Tenant's election, apply towards the Total Construction Costs",
            "expected": {"answer": "YES", "value": "$10,000"},
            "note": "HMBP-BCP Exhibit B §5 Construction Allowance.",
        },
    ],
    # ---- Security deposit ----
    "security_deposit": [
        {
            "qid": "Q1",
            "clause": "Security Deposit: N/A",
            "expected": {"answer": "NO", "value": "None"},
            "note": "ProLogis net-lease — no deposit. Return None, not $0.",
        },
        {
            "qid": "Q1",
            "clause": "SECURITY DEPOSIT: $41,642.30 (\"Security Deposit\")",
            "expected": {"answer": "YES", "value": "$41,642.30"},
            "note": "HMBP-BCP Face Page security deposit.",
        },
    ],
    # ---- Proportionate share ----
    "pro_rata": [
        {
            "qid": "Q1",
            "clause": "Tenant's Proportionate Share of Project: 12.59%",
            "expected": {"answer": "YES", "value": "12.59%"},
        },
        {
            "qid": "Q1",
            "clause": "PROPORTIONATE SHARE: 10.46%",
            "expected": {"answer": "YES", "value": "10.46%"},
        },
    ],
    # ---- Late payment (new playbook) ----
    "late_payment": [
        {
            "qid": "Q2",
            "clause": "Tenant shall pay to Landlord on demand a late charge equal to five percent (5%) of such delinquent sum.",
            "expected": {"answer": "YES", "value": "5%"},
            "note": "ProLogis Paragraph 4 late charge.",
        },
        {
            "qid": "Q3",
            "clause": "In the event that any installment of Monthly Base Rent or Additional Rent is not received by Landlord within seven (7) days of the date when such payment or reimbursement is due, Tenant shall pay to Landlord on demand a late charge",
            "expected": {"answer": "YES", "value": "7 days"},
            "note": "HMBP-BCP Section 3.1 grace period.",
        },
    ],
    # ---- Renewal options ----
    "renewal_options": [
        {
            "qid": "Q1",
            "clause": "Tenant shall have the right to extend the Lease Term for an additional term of 3 years (such additional term is hereinafter called the \"First Extension Term\")... Tenant shall give Landlord notice at least six (6) months, but not more than twelve (12) months, prior to the scheduled expiration date",
            "expected": {
                "answer": "YES",
                "value": "Two 3-year renewal options; notice 6-12 months before expiration; $10,427.40/mo then $10,813.60/mo"
            },
            "note": "ProLogis Addendum 6 — two renewal options with fixed rent.",
        },
    ],
    # ---- Holdover ----
    "holdover": [
        {
            "qid": "Q1",
            "clause": "Tenant shall pay Landlord...an amount equal to 150% of the Base Rent in effect on the termination date, computed on a monthly basis",
            "expected": {"answer": "YES", "value": "150% of Base Rent"},
            "note": "ProLogis standard 150% holdover.",
        },
        {
            "qid": "Q1",
            "clause": "Tenant shall pay one hundred fifty percent (150%) of the Monthly Base Rent...for the first three (3) calendar months...the Holdover Rate shall be two hundred percent (200%)",
            "expected": {"answer": "YES", "value": "Tiered: 150% first 3 months then 200% (if 90-day advance notice); 200% from day one otherwise"},
            "note": "HMBP-BCP tiered holdover — more complex structure.",
        },
    ],
}

# Output types that benefit from voting (numeric/date answers converge on a
# single "right" value; prose answers don't).
_VOTABLE_OUTPUT_TYPES = {
    "currency", "number", "numeric", "amount", "money",
    "date", "percentage", "percent", "integer",
}

# Condition types (from BRD Questions.xlsx) that imply the answer is a
# specific data point, not a narrative clause.
_VOTABLE_CONDITION_TYPES = {
    "Amount Based", "Period Based", "Date Based",
    "Number Based", "Percent Based", "Percentage Based",
}


def _should_use_voting(q, playbook) -> bool:
    """Return True iff this (question, playbook) pair should use N=3 voting.

    Voting is valuable when the target answer is a canonical value that
    multiple independent samples can converge on. For prose extraction the
    cost outweighs the benefit.
    """
    ot = (q.output_type or playbook.output_type or "").strip().lower()
    if any(t in ot for t in _VOTABLE_OUTPUT_TYPES):
        return True
    ct = (q.condition_type or "").strip()
    if ct in _VOTABLE_CONDITION_TYPES:
        return True
    # Heuristic: the question explicitly asks for a number / amount / date
    qtext = (q.question_text or "").lower()
    if any(kw in qtext for kw in (
        "what is the amount", "what is the rent", "how many days",
        "what is the percentage", "what is the date", "what is the grace",
        "how much", "what is the fee",
    )):
        return True
    return False


def _ans_priority(ans: str) -> int:
    """Tie-breaker for YES/NO/UNKNOWN majority vote. Lower = preferred on tie.

    On a 1/1/1 split we prefer NO (conservative), then UNKNOWN, then YES.
    This matches the general principle: only say YES when we have >=2 votes.
    """
    order = {"NO": 0, "UNKNOWN": 1, "YES": 2}
    return order.get(ans.upper(), 3)


# Canonicalization: map surface forms like "$1,234.56" and "$1234.56" and
# "1234.56" to the same string so the mode() picks them together.

_CURRENCY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d{1,2})?)")
_DATE_RE = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(?P<d>\d{1,2}),?\s+(?P<y>\d{4})",
    re.IGNORECASE,
)
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")
_PERCENT_RE = re.compile(r"([\d.]+)\s*%")
_DAYS_RE = re.compile(r"(\d+)\s*(?:day|days|business\s*days?|calendar\s*days?)", re.IGNORECASE)

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _canonicalize_value(v: str, output_type: str) -> str:
    """Normalize a surface value to a canonical form for voting mode().

    Examples:
        "$1,234.56"  →  "1234.56"
        "$1234.56"   →  "1234.56"
        "1,234.56"   →  "1234.56"
        "June 1, 2009"  →  "2009-06-01"
        "6/1/2009"   →  "2009-06-01"
        "10 days"    →  "10"
        "5%"         →  "5.0"
        "FIVE percent (5%)"  →  "5.0"
    Non-canonicalizable values are returned stripped + lowercased.
    """
    if v is None:
        return ""
    v = str(v).strip()
    if not v:
        return ""

    ot = (output_type or "").lower()

    if "currency" in ot or "amount" in ot or "money" in ot:
        m = _CURRENCY_RE.search(v)
        if m:
            num_str = m.group(1).replace(",", "")
            try:
                num = float(num_str)
                return f"{num:.2f}" if num % 1 else str(int(num))
            except ValueError:
                pass

    if "percent" in ot:
        m = _PERCENT_RE.search(v) or re.search(r"([\d.]+)", v)
        if m:
            try:
                return f"{float(m.group(1)):.2f}"
            except ValueError:
                pass

    if "date" in ot:
        m = _DATE_RE.search(v)
        if m:
            mon = _MONTHS.get(m.group("mon").lower()[:3], "01")
            return f"{m.group('y')}-{mon}-{int(m.group('d')):02d}"
        m = _DATE_NUM_RE.search(v)
        if m:
            mo, d, y = m.group(1), m.group(2), m.group(3)
            if len(y) == 2:
                y = ("20" if int(y) < 50 else "19") + y
            return f"{y}-{int(mo):02d}-{int(d):02d}"

    if "number" in ot or "integer" in ot or "numeric" in ot:
        m = _DAYS_RE.search(v) or re.search(r"(\d+)", v)
        if m:
            return m.group(1)

    # Default: lowercase, squash whitespace
    return re.sub(r"\s+", " ", v.lower()).strip()
