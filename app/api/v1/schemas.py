"""Extraction Schemas API.

Schemas are user-uploaded JSON documents that select / define which fields
the extraction pipeline should process. They override the built-in 79 BRD
playbooks for any extraction that references them via ?schema_id=...

Endpoints:
    POST   /api/v1/schemas              upload a new schema (file or JSON body)
    GET    /api/v1/schemas              list all schemas (latest version of each slug)
    GET    /api/v1/schemas/{id}         get one schema (latest, or ?version=...)
    GET    /api/v1/schemas/{id}/versions  list every stored version of one slug
    PUT    /api/v1/schemas/{id}         replace the schema (auto-bumps version)
    DELETE /api/v1/schemas/{id}         delete every version of a schema
    POST   /api/v1/schemas/{id}/activate   mark this schema as the default
    POST   /api/v1/schemas/validate     dry-run validation only (does not persist)
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.agents.playbooks.loader import get_playbooks
from app.db.session import get_db
from app.schemas.models import (
    SchemaListOut,
    SchemaOut,
    SchemaSummaryOut,
    SchemaUploadIn,
    SchemaValidationErrorItem,
    SchemaValidationErrorOut,
)
from app.services.schema_store import (
    SchemaNotFound,
    delete_schema,
    get_active_schema,
    get_schema,
    get_schema_versions,
    list_schemas,
    set_active_schema,
    upsert_schema,
)
from app.services.schema_validator import (
    ValidationResult,
    validate_extraction_schema,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schemas", tags=["schemas"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _builtin_playbook_ids() -> set[str]:
    return set(get_playbooks().keys())


async def _read_schema_payload(
    file: UploadFile | None,
    body: SchemaUploadIn | None,
) -> dict:
    """Accept either multipart upload or a JSON body, return the parsed dict.

    Multipart wins if both are provided.
    """
    if file is not None:
        try:
            raw = await file.read()
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Uploaded file is not valid JSON: {exc}",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not read uploaded file: {exc}",
            )
    if body is not None:
        return body.model_dump(exclude_none=False)
    raise HTTPException(
        status_code=400,
        detail="Provide either a 'file' multipart upload or a JSON body",
    )


def _to_summary(row, schema_json: dict | None = None) -> SchemaSummaryOut:
    """Convert ORM row to SchemaSummaryOut, computing field_count on the fly."""
    s = SchemaSummaryOut.model_validate(row)
    j = schema_json if schema_json is not None else (row.schema_json or {})
    fields = j.get("fields") or []
    s.field_count = len(fields) if isinstance(fields, list) else 0
    return s


def _validate_or_raise(
    schema_doc: dict,
    *,
    return_warnings: bool = False,
) -> ValidationResult:
    """Run validator, raise 400 with structured payload on failure."""
    result = validate_extraction_schema(
        schema_doc, available_playbook_ids=_builtin_playbook_ids(),
    )
    if not result.ok:
        # Translate to the API error shape
        err_payload = SchemaValidationErrorOut(
            errors=[
                SchemaValidationErrorItem(
                    path=e.path, code=e.code, message=e.message,
                )
                for e in result.errors
            ],
            warnings=[
                SchemaValidationErrorItem(
                    path=w.path, code=w.code, message=w.message,
                )
                for w in result.warnings
            ],
        )
        raise HTTPException(
            status_code=400,
            detail=err_payload.model_dump(),
        )
    return result


# ---------------------------------------------------------------------------
# 1. POST /schemas — upload
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=SchemaOut,
    status_code=status.HTTP_201_CREATED,
    summary="1. Upload a schema (file or JSON body)",
    responses={
        201: {"description": "Schema saved (returns persisted record)"},
        400: {
            "description": "Validation failed — see errors[] in body",
            "model": SchemaValidationErrorOut,
        },
    },
)
async def upload_schema(
    file: UploadFile | None = File(
        None,
        description=(
            "Optional .json file upload. If omitted, send the schema "
            "as the JSON request body instead."
        ),
    ),
    body: SchemaUploadIn | None = None,
    created_by: str | None = Query(
        None,
        description="Optional user/email of the uploader (for audit log)",
    ),
    db: Session = Depends(get_db),
):
    """Upload a schema. Accepts multipart-file or a JSON body.

    If a schema with the same `schema_id` already exists, this creates a
    new version (auto-bumps patch number if the requested version is taken).
    """
    schema_doc = await _read_schema_payload(file, body)
    _validate_or_raise(schema_doc)

    row = upsert_schema(db, schema_doc=schema_doc, created_by=created_by)
    db.commit()
    db.refresh(row)
    return SchemaOut.model_validate(row)


# ---------------------------------------------------------------------------
# 2. POST /schemas/validate — dry-run validation only
# ---------------------------------------------------------------------------

@router.post(
    "/validate",
    summary="1a. Dry-run validation",
    responses={
        200: {"description": "Schema is valid"},
        400: {"description": "Validation failed", "model": SchemaValidationErrorOut},
    },
)
async def validate_schema_endpoint(
    file: UploadFile | None = File(None),
    body: SchemaUploadIn | None = None,
):
    """Dry-run schema validation. Useful before committing an upload."""
    schema_doc = await _read_schema_payload(file, body)
    result = _validate_or_raise(schema_doc, return_warnings=True)
    return {
        "ok": True,
        "field_count": result.field_count,
        "references_count": result.references_count,
        "inline_count": result.inline_count,
        "warnings": [
            {"path": w.path, "code": w.code, "message": w.message}
            for w in result.warnings
        ],
    }


# ---------------------------------------------------------------------------
# 3. GET /schemas — list
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=SchemaListOut,
    summary="List all schemas",
)
def list_schemas_endpoint(db: Session = Depends(get_db)) -> SchemaListOut:
    rows = list_schemas(db)
    active = get_active_schema(db)
    return SchemaListOut(
        schemas=[_to_summary(r) for r in rows],
        count=len(rows),
        active_schema_id=active.schema_id if active else None,
    )


# ---------------------------------------------------------------------------
# 4. GET /schemas/{schema_id} — get one
# ---------------------------------------------------------------------------

@router.get(
    "/{schema_id}",
    response_model=SchemaOut,
    summary="Get one schema (optional ?version=)",
    responses={404: {"description": "Schema not found"}},
)
def get_schema_endpoint(
    schema_id: str,
    version: str | None = Query(
        None, description="Specific version to fetch (e.g. '1.0.0'). Omit for latest."
    ),
    db: Session = Depends(get_db),
) -> SchemaOut:
    try:
        row = get_schema(db, schema_id, version=version)
    except SchemaNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return SchemaOut.model_validate(row)


# ---------------------------------------------------------------------------
# 5. GET /schemas/{id}/versions — version history
# ---------------------------------------------------------------------------

@router.get(
    "/{schema_id}/versions",
    summary="List version history of a schema",
    responses={404: {"description": "Schema not found"}},
)
def get_schema_versions_endpoint(
    schema_id: str,
    db: Session = Depends(get_db),
):
    rows = get_schema_versions(db, schema_id)
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"schema_id={schema_id} not found",
        )
    return {
        "schema_id": schema_id,
        "versions": [
            {
                "version": r.version,
                "is_active": r.is_active,
                "created_at": r.created_at.isoformat(),
                "created_by": r.created_by,
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# 6. PUT /schemas/{id} — replace (re-upload)
# ---------------------------------------------------------------------------

@router.put(
    "/{schema_id}",
    response_model=SchemaOut,
    summary="Re-upload (replace) a schema",
    responses={
        200: {"description": "Schema replaced (new version created)"},
        400: {"description": "Validation failed", "model": SchemaValidationErrorOut},
    },
)
async def replace_schema_endpoint(
    schema_id: str,
    file: UploadFile | None = File(None),
    body: SchemaUploadIn | None = None,
    created_by: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Replace a schema by uploading a new version. The schema_id in the
    URL takes precedence over schema_id in the body — if they differ, the
    URL value wins and the body is rewritten before storing."""
    schema_doc = await _read_schema_payload(file, body)
    schema_doc["schema_id"] = schema_id      # URL is canonical
    _validate_or_raise(schema_doc)

    row = upsert_schema(db, schema_doc=schema_doc, created_by=created_by)
    db.commit()
    db.refresh(row)
    return SchemaOut.model_validate(row)


# ---------------------------------------------------------------------------
# 7. DELETE /schemas/{id}
# ---------------------------------------------------------------------------

@router.delete(
    "/{schema_id}",
    summary="Delete a schema",
    responses={
        200: {"description": "Schema versions deleted"},
        404: {"description": "Schema not found"},
    },
)
def delete_schema_endpoint(
    schema_id: str,
    db: Session = Depends(get_db),
):
    try:
        n = delete_schema(db, schema_id)
    except SchemaNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return {"deleted": n, "schema_id": schema_id}


# ---------------------------------------------------------------------------
# 8. POST /schemas/{id}/activate — mark as default
# ---------------------------------------------------------------------------

@router.post(
    "/{schema_id}/activate",
    response_model=SchemaOut,
    summary="Activate a schema (set as default)",
    responses={
        200: {"description": "Schema activated"},
        404: {"description": "Schema not found"},
    },
)
def activate_schema_endpoint(
    schema_id: str,
    db: Session = Depends(get_db),
):
    """Mark `schema_id` as the active default. New extractions that omit
    `?schema_id=` will use this one. Clears any previously-active flag."""
    try:
        row = set_active_schema(db, schema_id)
    except SchemaNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    db.refresh(row)
    return SchemaOut.model_validate(row)
