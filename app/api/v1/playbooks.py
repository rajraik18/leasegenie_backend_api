"""Playbooks API — exposes the compiled decision trees for introspection.

Useful for debugging, for the UI to display "why did the agent do this?", and
for product owners to audit the flow derived from the .docx guides.
"""
from fastapi import APIRouter, HTTPException

from app.agents.playbooks import get_playbook, get_playbooks

router = APIRouter(prefix="/playbooks", tags=["playbooks"])


@router.get("", response_model=list[dict])
def list_playbooks() -> list[dict]:
    """Summary of every compiled playbook."""
    out = []
    for pb in sorted(get_playbooks().values(), key=lambda p: (p.category, p.field_name)):
        out.append({
            "field_id": pb.field_id,
            "field_name": pb.field_name,
            "category": pb.category,
            "output_type": pb.output_type,
            "question_count": len(pb.questions),
            "keyword_count": len(pb.keywords),
            "property_applicability": pb.property_applicability,
            "source_docx": pb.source_docx,
            "depends_on": pb.depends_on,
        })
    return out


@router.get("/{field_id}", response_model=dict)
def get_playbook_detail(field_id: str) -> dict:
    """Full decision tree for a field."""
    pb = get_playbook(field_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    return {
        "field_id": pb.field_id,
        "field_name": pb.field_name,
        "category": pb.category,
        "output_type": pb.output_type,
        "overview": pb.overview,
        "preliminary": pb.preliminary,
        "property_applicability": pb.property_applicability,
        "depends_on": pb.depends_on,
        "keywords": pb.keywords,
        "questions": [
            {
                "id": q.id,
                "priority": q.priority,
                "condition_type": q.condition_type,
                "question_text": q.question_text,
                "extraction_hint": q.extraction_hint,
                "output_type": q.output_type,
                "search_scope": q.search_scope.value,
                "yes_branch": _action_to_dict(q.yes_branch),
                "no_branch": _action_to_dict(q.no_branch),
                "red_flag": q.red_flag,
                "notes": q.notes,
            }
            for q in pb.questions
        ],
    }


def _action_to_dict(a) -> dict | None:
    if a is None:
        return None
    return {
        "type": a.type.value,
        "goto": a.goto,
        "literal": a.literal,
        "also_extract": a.also_extract,
    }
