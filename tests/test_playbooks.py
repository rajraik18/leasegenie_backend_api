"""Test that the playbook compiler produces usable playbooks from the real
.docx guides + Questions.xlsx."""
from pathlib import Path

from app.agents.playbooks.compiler import compile_all
from app.agents.playbooks import get_playbooks
from app.agents.playbooks.schema import ActionType


def test_compile_produces_playbooks(tmp_path):
    """Compiler should emit at least 50 playbooks from the real refs."""
    source = Path("data/playbooks_source")
    out = tmp_path / "compiled"
    result = compile_all(source, out)
    assert result["count"] >= 50, f"got {result['count']}"
    # Index file should exist
    assert (out / "_index.json").exists()


def test_annual_base_rent_playbook_has_decision_tree():
    """Annual Base Rent is the canonical example — should have multi-step flow."""
    # Force a fresh compile into a temp spot for determinism
    pbs = get_playbooks()
    pb = pbs.get("annual_base_rent")
    assert pb is not None, "annual_base_rent playbook missing"
    assert len(pb.questions) >= 2
    assert pb.output_type == "Number"
    # Q1 should be about Summary page
    q1 = pb.questions[0]
    assert q1.id == "Q1"
    assert "summary" in q1.question_text.lower()
    # Q1's YES branch should go to another question (not terminate)
    assert q1.yes_branch is not None
    assert q1.yes_branch.type in (ActionType.GOTO, ActionType.EXTRACT)


def test_cam_playbook_has_no_recovery_literal():
    """CAM playbook should contain the 'No Recovery' literal action somewhere."""
    pbs = get_playbooks()
    pb = pbs.get("cam")
    assert pb is not None
    found = False
    for q in pb.questions:
        for branch in (q.yes_branch, q.no_branch):
            if branch and branch.type == ActionType.RECORD_LITERAL and branch.literal:
                if "no recovery" in branch.literal.lower():
                    found = True
    assert found, "CAM playbook should include 'No Recovery' literal action"


def test_co_tenancy_is_retail_only():
    """Co-Tenancy should be gated to retail per docx heading."""
    pbs = get_playbooks()
    pb = pbs.get("co_tenancy")
    assert pb is not None
    # Either property_applicability restricts it, or the playbook source is the
    # retail-only docx heading.
    assert (
        pb.property_applicability.get("Retail") is True
        and pb.property_applicability.get("Office") is False
    ), f"co_tenancy applicability: {pb.property_applicability}"


def test_all_playbooks_have_at_least_one_question():
    pbs = get_playbooks()
    assert len(pbs) > 0
    for fid, pb in pbs.items():
        assert len(pb.questions) >= 1, f"{fid} has no questions"


def test_allowance_depends_on_lcd():
    """Allowance playbook should declare its dependency on LCD per docx."""
    pbs = get_playbooks()
    pb = pbs.get("allowance")
    assert pb is not None
    assert "original_lease_commencement_date" in pb.depends_on
