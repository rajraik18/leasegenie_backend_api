"""Evaluation harness — measure extraction correctness against hand-labeled
ground truth.

Usage:
    1. Create ground-truth YAML files under tests/eval/ground_truth/
       (one per document, named after the document)
    2. Run: python -m tests.eval.harness --corpus tests/eval/documents/
    3. Read the generated report at tests/eval/reports/

The harness measures five things:
    (a) Exact Match rate        — canonical-form equality
    (b) Citation accuracy       — did we cite the right page + clause?
    (c) Expected Calibration    — does confidence predict correctness?
    (d) Reviewer hours saved    — time-weighted ROI metric
    (e) Per-category accuracy   — which specialist is weakest?

Design principles:
    - Runs against a mocked LLM when LLM_MOCK=1 (for CI)
    - Runs against live Ollama otherwise (for measuring real accuracy)
    - Scores are deterministic: same inputs → same report
    - Failures are actionable: every miss includes the reason
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ground-truth schema
# ---------------------------------------------------------------------------

@dataclass
class GroundTruthField:
    """One field's expected value for one document."""
    field_id: str
    expected_value: str | None              # "None" means field correctly absent
    expected_page: int | None = None
    expected_clause_snippet: str | None = None    # ≥15 chars of verbatim text
    # Tolerances
    date_days_tolerance: int = 0            # e.g., 1 = ±1 day OK
    currency_pct_tolerance: float = 0.0     # e.g., 0.01 = ±1% OK
    partial_match_acceptable: bool = True
    # Meta
    difficulty: int = 2                     # 1-5 per README
    notes: str = ""


@dataclass
class GroundTruthDocument:
    """All ground-truth fields for one document."""
    document_path: str                       # e.g., "Sample 6.pdf"
    property_type: str                       # "Industrial", "Retail", ...
    document_type: str = "base_lease"
    fields: list[GroundTruthField] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring types
# ---------------------------------------------------------------------------

@dataclass
class FieldScore:
    field_id: str
    expected: str | None
    actual: str | None
    exact_match: bool
    partial_match: bool
    citation_correct: bool
    page_correct: bool
    clause_correct: bool
    confidence: float
    is_none_correct: bool        # True if both expected and actual are "None"
    failure_reason: str | None = None


@dataclass
class DocumentReport:
    document_path: str
    total_fields: int
    exact_matches: int
    partial_matches: int
    false_nones: int              # extracted None but value existed
    correct_nones: int            # correctly extracted None
    wrong_values: int
    citation_correct: int
    overall_em_rate: float
    overall_partial_rate: float
    weighted_score: float         # EM=1.0, PM=0.7
    per_field_scores: list[FieldScore]
    red_flags_fired: list[str] = field(default_factory=list)
    reviewer_minutes_saved: float = 0.0


@dataclass
class AggregateReport:
    timestamp: str
    corpus_size: int
    total_fields_evaluated: int
    overall_em_rate: float
    overall_partial_rate: float
    overall_weighted_score: float
    per_document: list[DocumentReport]
    per_category: dict[str, dict[str, float]]
    per_difficulty: dict[int, dict[str, float]]
    expected_calibration_error: float
    total_reviewer_hours_saved: float


# ---------------------------------------------------------------------------
# Canonicalization (must match playbook_executor._canonicalize_value)
# ---------------------------------------------------------------------------

_CURRENCY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d{1,2})?)")
_DATE_NAME_RE = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(?P<d>\d{1,2}),?\s+(?P<y>\d{4})",
    re.IGNORECASE,
)
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")
_PERCENT_RE = re.compile(r"([\d.]+)\s*%?")
_NUMBER_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\b")

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _canonicalize(v: Any) -> str:
    """Normalize a value for comparison."""
    if v is None:
        return ""
    s = str(v).strip().lower()
    if not s or s in {"none", "n/a", "not found", "unknown"}:
        return ""
    return re.sub(r"\s+", " ", s)


def _canonical_currency(v: str) -> str | None:
    m = _CURRENCY_RE.search(v)
    if m:
        num_str = m.group(1).replace(",", "")
        try:
            num = float(num_str)
            return f"{num:.2f}"
        except ValueError:
            pass
    return None


def _canonical_date(v: str) -> str | None:
    m = _DATE_NAME_RE.search(v)
    if m:
        mon = _MONTHS.get(m.group("mon").lower()[:3], "01")
        return f"{m.group('y')}-{mon}-{int(m.group('d')):02d}"
    m = _DATE_NUM_RE.search(v)
    if m:
        mo, d, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = ("20" if int(y) < 50 else "19") + y
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


# ---------------------------------------------------------------------------
# Per-field scoring
# ---------------------------------------------------------------------------

def score_field(
    result: Any,             # AgentFieldResult-like (has value, confidence, page, clause_text)
    gt: GroundTruthField,
) -> FieldScore:
    """Score one extracted field vs. its ground truth.

    Returns a FieldScore including the reason on failure.
    """
    actual_value = getattr(result, "value", None) if result else None
    actual_conf = getattr(result, "confidence", 0.0) if result else 0.0
    actual_page = getattr(result, "page_number", None) if result else None
    actual_clause = getattr(result, "clause_text", None) if result else None

    expected = _canonicalize(gt.expected_value)
    actual = _canonicalize(actual_value)

    # Correct-None case
    if expected == "" and actual == "":
        return FieldScore(
            field_id=gt.field_id,
            expected=gt.expected_value, actual=actual_value,
            exact_match=True, partial_match=True,
            is_none_correct=True,
            citation_correct=True,
            page_correct=True,
            clause_correct=True,
            confidence=actual_conf,
        )

    # False-None: expected something, got nothing
    if expected != "" and actual == "":
        return FieldScore(
            field_id=gt.field_id,
            expected=gt.expected_value, actual=actual_value,
            exact_match=False, partial_match=False,
            is_none_correct=False,
            citation_correct=False, page_correct=False, clause_correct=False,
            confidence=actual_conf,
            failure_reason=f"FALSE_NONE: expected {gt.expected_value!r}, got {actual_value!r}",
        )

    # Hallucination: expected nothing, got something
    if expected == "" and actual != "":
        return FieldScore(
            field_id=gt.field_id,
            expected=gt.expected_value, actual=actual_value,
            exact_match=False, partial_match=False,
            is_none_correct=False,
            citation_correct=False, page_correct=False, clause_correct=False,
            confidence=actual_conf,
            failure_reason=f"HALLUCINATION: expected None, got {actual_value!r}",
        )

    # Type-specific exact match
    exact, partial = _match_values(
        expected_raw=gt.expected_value,
        actual_raw=actual_value,
        date_days_tolerance=gt.date_days_tolerance,
        currency_pct_tolerance=gt.currency_pct_tolerance,
    )

    # Citation check
    page_ok = False
    if gt.expected_page is not None and actual_page is not None:
        page_ok = abs(actual_page - gt.expected_page) <= 1
    elif gt.expected_page is None:
        page_ok = True

    clause_ok = False
    if gt.expected_clause_snippet and actual_clause:
        snippet = _canonicalize(gt.expected_clause_snippet)[:60]
        clause_ok = snippet in _canonicalize(actual_clause)
    elif not gt.expected_clause_snippet:
        clause_ok = True

    citation_ok = page_ok and clause_ok

    reason = None
    if not exact and not partial:
        reason = f"WRONG_VALUE: expected {gt.expected_value!r}, got {actual_value!r}"
    elif exact and not citation_ok:
        reason = "VALUE_OK_CITATION_WRONG"

    return FieldScore(
        field_id=gt.field_id,
        expected=gt.expected_value, actual=actual_value,
        exact_match=exact, partial_match=partial,
        is_none_correct=False,
        citation_correct=citation_ok,
        page_correct=page_ok, clause_correct=clause_ok,
        confidence=actual_conf,
        failure_reason=reason,
    )


def _match_values(
    expected_raw: str | None,
    actual_raw: str | None,
    date_days_tolerance: int = 0,
    currency_pct_tolerance: float = 0.0,
) -> tuple[bool, bool]:
    """Return (exact_match, partial_match)."""
    if expected_raw is None or actual_raw is None:
        return False, False

    expected = str(expected_raw).strip()
    actual = str(actual_raw).strip()

    # Currency compare
    exp_cur = _canonical_currency(expected)
    act_cur = _canonical_currency(actual)
    if exp_cur and act_cur:
        if exp_cur == act_cur:
            return True, True
        if currency_pct_tolerance > 0:
            try:
                exp_f, act_f = float(exp_cur), float(act_cur)
                if exp_f > 0 and abs(act_f - exp_f) / exp_f <= currency_pct_tolerance:
                    return False, True
            except Exception:
                pass

    # Date compare
    exp_d = _canonical_date(expected)
    act_d = _canonical_date(actual)
    if exp_d and act_d:
        if exp_d == act_d:
            return True, True
        if date_days_tolerance > 0:
            try:
                from datetime import date
                dy, dm, dd = map(int, exp_d.split("-"))
                ay, am, ad = map(int, act_d.split("-"))
                diff = abs((date(dy, dm, dd) - date(ay, am, ad)).days)
                if diff <= date_days_tolerance:
                    return False, True
            except Exception:
                pass

    # Canonical string
    cexp, cact = _canonicalize(expected), _canonicalize(actual)
    if cexp == cact:
        return True, True

    # Partial: actual contains expected (or vice versa), minimum 5 chars
    if len(cexp) >= 5 and (cexp in cact or cact in cexp):
        return False, True

    # Partial: significant token overlap either direction.
    # Uses precision (how many of actual's tokens are in expected) OR
    # recall (how many of expected's tokens are in actual) — whichever is higher.
    # This matches a human abstractor's judgment: if the short form hits
    # the key tokens, it's a partial match.
    STOP = {"the", "and", "for", "with", "not", "that", "this", "from", "any",
            "all", "per", "are", "has", "have", "been", "may", "must", "will",
            "shall", "such", "other", "each", "than", "between", "within", "into"}
    expected_tokens = {t for t in re.findall(r"\w{3,}", cexp) if t not in STOP}
    actual_tokens = {t for t in re.findall(r"\w{3,}", cact) if t not in STOP}
    if expected_tokens and actual_tokens:
        overlap = expected_tokens & actual_tokens
        recall = len(overlap) / len(expected_tokens)
        precision = len(overlap) / len(actual_tokens)
        # Either direction: 60% overlap is a partial match
        if max(recall, precision) >= 0.6:
            return False, True
        if recall >= 0.4 and precision >= 0.4:
            return False, True

    return False, False


# ---------------------------------------------------------------------------
# Document-level aggregation
# ---------------------------------------------------------------------------

def evaluate_document(
    gt_doc: GroundTruthDocument,
    extracted_fields: list[Any],
    red_flags: list[str] | None = None,
) -> DocumentReport:
    """Score one document's extraction against its ground truth."""
    extracted_by_id = {
        getattr(r, "field_id", None): r
        for r in extracted_fields
    }

    per_field = []
    em = pm = false_none = correct_none = wrong = cite_ok = 0

    for gt in gt_doc.fields:
        result = extracted_by_id.get(gt.field_id)
        score = score_field(result, gt)
        per_field.append(score)

        if score.exact_match:
            em += 1
        elif score.partial_match:
            pm += 1
        else:
            if score.failure_reason and score.failure_reason.startswith("FALSE_NONE"):
                false_none += 1
            elif score.expected and not score.actual:
                false_none += 1
            else:
                wrong += 1
        if score.is_none_correct:
            correct_none += 1
        if score.citation_correct:
            cite_ok += 1

    total = len(per_field) or 1
    em_rate = em / total
    # Partial = em + pm both count toward partial
    partial_rate = (em + pm) / total
    weighted = (em + 0.7 * pm) / total

    # Reviewer-hours saved: 2 min saved per high-confidence correct field;
    # 0.5 min saved per correct field flagged for review (quick verify);
    # -5 min penalty per silently wrong field (rework cost).
    rev_minutes = 0.0
    for fs in per_field:
        if fs.exact_match and fs.confidence >= 0.9:
            rev_minutes += 2.0
        elif fs.exact_match or fs.partial_match:
            rev_minutes += 0.5
        elif not fs.exact_match and fs.confidence >= 0.7:
            # Silently wrong — most expensive
            rev_minutes -= 5.0

    return DocumentReport(
        document_path=gt_doc.document_path,
        total_fields=total,
        exact_matches=em,
        partial_matches=pm,
        false_nones=false_none,
        correct_nones=correct_none,
        wrong_values=wrong,
        citation_correct=cite_ok,
        overall_em_rate=round(em_rate, 3),
        overall_partial_rate=round(partial_rate, 3),
        weighted_score=round(weighted, 3),
        per_field_scores=per_field,
        red_flags_fired=red_flags or [],
        reviewer_minutes_saved=round(rev_minutes, 1),
    )


# ---------------------------------------------------------------------------
# Calibration (ECE)
# ---------------------------------------------------------------------------

def expected_calibration_error(
    scores: list[FieldScore],
    n_bins: int = 10,
) -> float:
    """Compute Expected Calibration Error across all scored fields.

    ECE = Σ (|B_m| / N) × |acc(B_m) - conf(B_m)|
    where B_m is the set of predictions in bin m.
    """
    if not scores:
        return 0.0
    # Bin by confidence
    buckets: dict[int, list[FieldScore]] = {}
    for s in scores:
        b = min(n_bins - 1, int(s.confidence * n_bins))
        buckets.setdefault(b, []).append(s)

    total = len(scores)
    ece = 0.0
    for bucket in buckets.values():
        n = len(bucket)
        if n == 0:
            continue
        accuracy = sum(1 for s in bucket if s.exact_match) / n
        avg_conf = sum(s.confidence for s in bucket) / n
        ece += (n / total) * abs(accuracy - avg_conf)
    return round(ece, 4)


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------

# Map field_id → category (mirror the 5 specialists).
# This would normally be loaded from playbook JSONs but hard-coding for speed.
FIELD_CATEGORIES = {
    # Basic Information
    "tenant_name": "Basic Information", "landlord_name": "Basic Information",
    "lease_date": "Basic Information", "lease_expiration_date": "Basic Information",
    "lease_term_yrs": "Basic Information", "original_lease_commencement_date": "Basic Information",
    "term_commencement_date": "Basic Information", "rent_commencement_date": "Basic Information",
    "most_recent_lease_start": "Basic Information", "leased_rsf": "Basic Information",
    "suite": "Basic Information", "street_address": "Basic Information",
    "city": "Basic Information", "state": "Basic Information",
    "building": "Basic Information", "property_name": "Basic Information",
    "lease_guarantor": "Basic Information",
    # Financial
    "annual_base_rent": "Financial Clauses", "future_rent_steps": "Financial Clauses",
    "security_deposit": "Financial Clauses", "late_payment": "Financial Clauses",
    "percentage_rent": "Financial Clauses", "breakpoint": "Financial Clauses",
    # Reimbursements
    "cam": "Reimbursements", "cam_inclusion": "Reimbursements",
    "cam_exclusion": "Reimbursements", "caps_on_cam": "Reimbursements",
    "pro_rata": "Reimbursements", "re_taxes": "Reimbursements",
    "landlord_insurance": "Reimbursements", "tenant_insurance_requirements": "Reimbursements",
    "utilities": "Reimbursements", "base_year": "Reimbursements",
    "base_year_amount": "Reimbursements", "mgmt_fee": "Reimbursements",
    "admin_fee": "Reimbursements", "gross_up": "Reimbursements",
    "other_income_exterior_signage_storage": "Reimbursements",
    "advertisement": "Reimbursements", "marketing": "Reimbursements",
    # Critical
    "permitted_use": "Critical Clauses", "renewal_options": "Critical Clauses",
    "tenant_termination": "Critical Clauses", "landlord_termination": "Critical Clauses",
    "holdover": "Critical Clauses", "rofo": "Critical Clauses",
    "rofr": "Critical Clauses", "right_of_expansion": "Critical Clauses",
    "contraction_option": "Critical Clauses", "purchase_option": "Critical Clauses",
    "co_tenancy": "Critical Clauses", "sales_kick_out": "Critical Clauses",
    "exclusive_use": "Critical Clauses", "continuous_operation": "Critical Clauses",
    "go_dark": "Critical Clauses", "relocation": "Critical Clauses",
    "landlord_s_recapture_rights": "Critical Clauses",
    "landlord_restriction": "Critical Clauses",
    # Other
    "allowance": "Other Lease Clauses", "alteration": "Other Lease Clauses",
    "assignment_and_subletting": "Other Lease Clauses",
    "sublease_provision": "Other Lease Clauses", "parking": "Other Lease Clauses",
    "subordination": "Other Lease Clauses", "estoppel_certificate": "Other Lease Clauses",
    "hazardous_materials": "Other Lease Clauses", "casualty": "Other Lease Clauses",
    "condemnation": "Other Lease Clauses", "monetary_default": "Other Lease Clauses",
    "non_monetary_default": "Other Lease Clauses",
    "repair_and_maintenance": "Other Lease Clauses",
    "reporting_of_financial_information": "Other Lease Clauses",
    "reporting_of_gross_sales": "Other Lease Clauses",
    "notices": "Other Lease Clauses", "indemnification": "Other Lease Clauses",
    "rules_and_regulations": "Other Lease Clauses", "force_majeure": "Other Lease Clauses",
    "brokers": "Other Lease Clauses", "move_out_conditions": "Other Lease Clauses",
}


def build_aggregate_report(
    per_doc: list[DocumentReport],
    ground_truth_docs: list[GroundTruthDocument],
) -> AggregateReport:
    """Combine per-document reports into the corpus-level aggregate."""
    all_scores: list[FieldScore] = []
    for d in per_doc:
        all_scores.extend(d.per_field_scores)

    gt_by_field: dict[str, GroundTruthField] = {}
    for gtd in ground_truth_docs:
        for gtf in gtd.fields:
            gt_by_field[gtf.field_id] = gtf  # last-wins; fine for difficulty lookup

    total = len(all_scores) or 1
    em = sum(1 for s in all_scores if s.exact_match)
    pm = sum(1 for s in all_scores if s.partial_match and not s.exact_match)

    # Per-category aggregation
    per_category: dict[str, dict[str, float]] = {}
    for s in all_scores:
        cat = FIELD_CATEGORIES.get(s.field_id, "Unknown")
        d = per_category.setdefault(cat, {"count": 0, "em": 0, "pm": 0})
        d["count"] += 1
        if s.exact_match:
            d["em"] += 1
        elif s.partial_match:
            d["pm"] += 1
    for cat, d in per_category.items():
        c = d["count"] or 1
        d["em_rate"] = round(d["em"] / c, 3)
        d["partial_rate"] = round((d["em"] + d["pm"]) / c, 3)
        d["weighted"] = round((d["em"] + 0.7 * d["pm"]) / c, 3)

    # Per-difficulty aggregation
    per_diff: dict[int, dict[str, float]] = {}
    for s in all_scores:
        gt = gt_by_field.get(s.field_id)
        diff = gt.difficulty if gt else 2
        d = per_diff.setdefault(diff, {"count": 0, "em": 0})
        d["count"] += 1
        if s.exact_match:
            d["em"] += 1
    for diff, d in per_diff.items():
        c = d["count"] or 1
        d["em_rate"] = round(d["em"] / c, 3)

    ece = expected_calibration_error(all_scores)
    total_hours = sum(d.reviewer_minutes_saved for d in per_doc) / 60.0

    return AggregateReport(
        timestamp=datetime.utcnow().isoformat(),
        corpus_size=len(per_doc),
        total_fields_evaluated=total,
        overall_em_rate=round(em / total, 3),
        overall_partial_rate=round((em + pm) / total, 3),
        overall_weighted_score=round((em + 0.7 * pm) / total, 3),
        per_document=per_doc,
        per_category=per_category,
        per_difficulty=per_diff,
        expected_calibration_error=ece,
        total_reviewer_hours_saved=round(total_hours, 2),
    )


def report_to_markdown(report: AggregateReport) -> str:
    """Render the aggregate report as a human-readable Markdown summary."""
    lines = [
        "# LeaseGenie Evaluation Report",
        "",
        f"**Timestamp:** {report.timestamp}",
        f"**Documents evaluated:** {report.corpus_size}",
        f"**Total fields scored:** {report.total_fields_evaluated}",
        "",
        "## Headline metrics",
        "",
        f"- **Exact match rate:** {report.overall_em_rate:.1%}",
        f"- **Partial match rate:** {report.overall_partial_rate:.1%}",
        f"- **Weighted score:** {report.overall_weighted_score:.1%}  (EM=1.0, PM=0.7)",
        f"- **Expected Calibration Error:** {report.expected_calibration_error:.4f}  (target: <0.05)",
        f"- **Reviewer hours saved:** {report.total_reviewer_hours_saved:.1f}",
        "",
        "## Per-category accuracy",
        "",
        "| Category | N | Exact | Partial | Weighted |",
        "|---|---|---|---|---|",
    ]
    for cat, d in sorted(report.per_category.items()):
        lines.append(
            f"| {cat} | {int(d['count'])} | {d['em_rate']:.1%} | "
            f"{d['partial_rate']:.1%} | {d['weighted']:.1%} |"
        )
    lines += [
        "",
        "## Per-difficulty accuracy",
        "",
        "| Difficulty | N | Exact Match |",
        "|---|---|---|",
    ]
    for diff in sorted(report.per_difficulty.keys()):
        d = report.per_difficulty[diff]
        lines.append(f"| {diff} | {int(d['count'])} | {d['em_rate']:.1%} |")
    lines += ["", "## Per-document summary", "", "| Document | Fields | EM | Weighted | Hours Saved |", "|---|---|---|---|---|"]
    for d in report.per_document:
        lines.append(
            f"| {d.document_path} | {d.total_fields} | "
            f"{d.overall_em_rate:.1%} | {d.weighted_score:.1%} | "
            f"{d.reviewer_minutes_saved/60:.2f} |"
        )

    # Top failures for debugging
    lines += ["", "## Top 10 failures across corpus", ""]
    all_failures = []
    for d in report.per_document:
        for fs in d.per_field_scores:
            if fs.failure_reason:
                all_failures.append((d.document_path, fs))
    all_failures.sort(key=lambda t: -t[1].confidence)  # highest-confidence failures first
    for doc, fs in all_failures[:10]:
        lines.append(f"- **{doc}** / `{fs.field_id}` (conf={fs.confidence:.2f}): {fs.failure_reason}")

    return "\n".join(lines)
