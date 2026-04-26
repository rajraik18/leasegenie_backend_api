"""Test specialist post-processing logic."""
from app.agents.playbook_executor import PlaybookResult
from app.agents.specialists.base import FieldOutcome
from app.agents.specialists.basic_info import _years_between, _normalize_date, BasicInfoAgent


def test_years_between():
    assert _years_between("01/01/2020", "01/01/2025") == 5.0
    assert _years_between("2020-01-01", "2025-01-01") == 5.0


def test_normalize_date_iso_to_us():
    assert _normalize_date("2024-03-15") == "03/15/2024"


def test_normalize_date_passthrough_us():
    assert _normalize_date("03/15/2024") == "03/15/2024"


def test_lease_date_becomes_undated_when_none():
    """If playbook produced None for lease_date, specialist should record 'undated'."""
    pr = PlaybookResult(
        field_id="lease_date",
        value="None",
        raw_value=None,
        confidence=0.0,
        source_doc=None,
        page_number=None,
        clause_number=None,
        clause_text=None,
        output_type="Date",
    )
    outcome = FieldOutcome(
        field_id="lease_date",
        field_name="Lease Date",
        category="Basic Information",
        doc_label="base_lease",
        playbook_result=pr,
    )
    # Directly call post_process (no client needed for this path)
    agent = object.__new__(BasicInfoAgent)  # skip __init__ which needs a client
    agent.post_process(outcome, shared_facts={})
    assert outcome.playbook_result.value == "undated"


def test_tenant_name_blank_flags_for_review():
    pr = PlaybookResult(
        field_id="tenant_name",
        value="None",
        raw_value=None,
        confidence=0.0,
        source_doc=None,
        page_number=None,
        clause_number=None,
        clause_text=None,
        output_type="Text",
    )
    outcome = FieldOutcome(
        field_id="tenant_name",
        field_name="Tenant Name",
        category="Basic Information",
        doc_label="base_lease",
        playbook_result=pr,
    )
    agent = object.__new__(BasicInfoAgent)
    agent.post_process(outcome, shared_facts={})
    assert outcome.playbook_result.needs_review is True
    assert any("blank" in rf.lower() for rf in outcome.playbook_result.red_flags)


def test_lease_term_derived_from_lcd_led():
    pr = PlaybookResult(
        field_id="lease_term_yrs",
        value="None",
        raw_value=None,
        confidence=0.0,
        source_doc=None,
        page_number=None,
        clause_number=None,
        clause_text=None,
        output_type="Number",
    )
    outcome = FieldOutcome(
        field_id="lease_term_yrs",
        field_name="Lease Term (yrs.)",
        category="Basic Information",
        doc_label="base_lease",
        playbook_result=pr,
    )
    shared = {
        "original_lease_commencement_date": {
            "base_lease": {"value": "01/01/2020", "output_type": "Date", "condition_type": None}
        },
        "lease_expiration_date": {
            "base_lease": {"value": "01/01/2025", "output_type": "Date", "condition_type": None}
        },
    }
    agent = object.__new__(BasicInfoAgent)
    agent.post_process(outcome, shared_facts=shared)
    assert outcome.playbook_result.value == "5.0"
    assert any("derived" in n.lower() for n in outcome.cross_field_notes)
