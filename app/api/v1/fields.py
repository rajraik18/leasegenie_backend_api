"""Field configuration API — exposes the parsed BRD field list.

Clients use this to render the per-tenant grid header, show which fields
are in scope for an (abstract_type × property_type) combination, and to
inspect keywords / questions behind each field.
"""
from fastapi import APIRouter, HTTPException, Query

from app.core.reference_data import get_reference_data
from app.schemas.models import (
    AbstractType, FieldConfigOut, FieldDetailOut, FieldQuestionOut, PropertyType,
)

router = APIRouter(prefix="/fields", tags=["fields"])


@router.get("", response_model=list[FieldConfigOut])
def list_fields(
    abstract_type: AbstractType | None = Query(None),
    property_type: PropertyType | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[FieldConfigOut]:
    """List all 72 fields (or just those in scope for abstract×property)."""
    ref = get_reference_data()
    fields = ref.list_fields(abstract_type=abstract_type, property_type=property_type)
    page = fields[offset : offset + limit]
    return [_to_config_out(fc) for fc in page]


@router.get("/{field_id}", response_model=FieldDetailOut)
def get_field(field_id: str) -> FieldDetailOut:
    """Full detail for a field — keywords and questions."""
    ref = get_reference_data()
    fc = ref.get(field_id)
    if fc is None:
        raise HTTPException(status_code=404, detail="field not found")
    return FieldDetailOut(
        **_to_config_out(fc).model_dump(),
        keywords=fc.keywords,
        questions=[
            FieldQuestionOut(
                question=q.question,
                extract=q.extract,
                output=q.output,
                priority=q.priority,
            )
            for q in fc.questions
        ],
    )


def _to_config_out(fc) -> FieldConfigOut:
    return FieldConfigOut(
        field_id=fc.field_id,
        name=fc.name,
        category=fc.category,
        output_type=fc.output_type,
        keyword_count=len(fc.keywords),
        question_count=len(fc.questions),
        abstract_applicability=fc.abstract_applicability,
        property_applicability=fc.property_applicability,
    )
