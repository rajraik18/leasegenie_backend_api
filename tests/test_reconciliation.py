"""Test the reconciliation agent."""
from app.agents.playbook_executor import PlaybookResult
from app.agents.reconciliation_agent import ReconciliationAgent
from app.agents.specialists.base import FieldOutcome


def _outcome(field_id: str, field_name: str, doc_label: str, value: str,
             confidence: float = 0.9, needs_review: bool = False) -> FieldOutcome:
    pr = PlaybookResult(
        field_id=field_id,
        value=value,
        raw_value=value,
        confidence=confidence,
        source_doc=doc_label,
        page_number=1,
        clause_number=None,
        clause_text=None,
        output_type="Text",
        needs_review=needs_review,
    )
    return FieldOutcome(
        field_id=field_id, field_name=field_name,
        category="Basic Information", doc_label=doc_label,
        playbook_result=pr,
    )


def test_rsf_mismatch_detected():
    outcomes = [
        _outcome("leased_rsf", "Leased RSF", "base_lease", "5000"),
        _outcome("leased_rsf", "Leased RSF", "amendment_1", "4900"),
    ]
    report = ReconciliationAgent().run(
        outcomes=outcomes,
        documents=[("d0", "base_lease", "base_lease", 0),
                   ("d1", "amendment_1", "amendment", 1)],
    )
    codes = [rf.code for rf in report.red_flags]
    assert "RSF_MISMATCH" in codes


def test_led_before_lcd_is_critical():
    outcomes = [
        _outcome("original_lease_commencement_date", "LCD", "base_lease", "01/01/2025"),
        _outcome("lease_expiration_date", "LED", "base_lease", "01/01/2020"),
    ]
    report = ReconciliationAgent().run(
        outcomes=outcomes,
        documents=[("d0", "base_lease", "base_lease", 0)],
    )
    criticals = [rf for rf in report.red_flags if rf.severity == "critical"]
    assert any(rf.code == "DATE_INCONSISTENCY" for rf in criticals)


def test_lease_term_math_mismatch():
    """Stated term of 10 years, but LED−LCD = 5 years → should flag."""
    outcomes = [
        _outcome("original_lease_commencement_date", "LCD", "base_lease", "01/01/2020"),
        _outcome("lease_expiration_date", "LED", "base_lease", "01/01/2025"),
        _outcome("lease_term_yrs", "Lease Term (yrs.)", "base_lease", "10"),
    ]
    report = ReconciliationAgent().run(
        outcomes=outcomes,
        documents=[("d0", "base_lease", "base_lease", 0)],
    )
    assert any(rf.code == "LEASE_TERM_MATH_MISMATCH" for rf in report.red_flags)


def test_needs_review_rollup():
    outcomes = [
        _outcome("tenant_name", "Tenant Name", "base_lease", "None", 0.0, needs_review=True),
    ]
    report = ReconciliationAgent().run(
        outcomes=outcomes,
        documents=[("d0", "base_lease", "base_lease", 0)],
    )
    assert report.needs_review_count == 1
    assert any(rf.code == "MANUAL_REVIEW_REQUIRED" for rf in report.red_flags)


def test_clean_extraction_has_no_flags():
    outcomes = [
        _outcome("tenant_name", "Tenant Name", "base_lease", "TechCo Inc", 0.95),
        _outcome("leased_rsf", "Leased RSF", "base_lease", "5000", 0.95),
    ]
    report = ReconciliationAgent().run(
        outcomes=outcomes,
        documents=[("d0", "base_lease", "base_lease", 0)],
    )
    assert len(report.red_flags) == 0
