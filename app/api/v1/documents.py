"""Documents API — upload base lease + amendments per tenant."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.orm import Document, Tenant
from app.schemas.models import DocumentOut

router = APIRouter(prefix="/tenants", tags=["documents"])

MAX_AMENDMENTS = 7  # BRD User Story #4


@router.post(
    "/{tenant_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_document(
    tenant_id: str,
    document_type: str = Form(..., description="'base_lease' or 'amendment'"),
    effective_date: str | None = Form(None, description="ISO date (YYYY-MM-DD)"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> DocumentOut:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    if document_type not in ("base_lease", "amendment"):
        raise HTTPException(status_code=400, detail="document_type must be 'base_lease' or 'amendment'")

    # Determine document_order
    existing = sorted(tenant.documents, key=lambda d: d.document_order)
    if document_type == "base_lease":
        if any(d.document_type == "base_lease" for d in existing):
            raise HTTPException(status_code=409, detail="base lease already uploaded — delete it first")
        document_order = 0
    else:
        amendment_count = sum(1 for d in existing if d.document_type == "amendment")
        if amendment_count >= MAX_AMENDMENTS:
            raise HTTPException(
                status_code=409,
                detail=f"maximum {MAX_AMENDMENTS} amendments reached",
            )
        document_order = amendment_count + 1

    # Parse effective date if given
    eff: datetime | None = None
    if effective_date:
        try:
            eff = datetime.fromisoformat(effective_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="effective_date must be ISO format YYYY-MM-DD")

    # Store file under uploads/<tenant_id>/<order>_<filename>
    filename = file.filename or f"upload-{document_order}.pdf"
    dest_dir: Path = settings.upload_dir / tenant_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = filename.replace("/", "_").replace("\\", "_")
    dest = dest_dir / f"{document_order:02d}_{safe_name}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    doc = Document(
        tenant_id=tenant_id,
        filename=filename,
        storage_path=str(dest),
        document_type=document_type,
        document_order=document_order,
        effective_date=eff,
        ocr_status="pending",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Fire off background OCR + vector-indexing so the document is ready
    # when extraction runs. Non-blocking — upload returns immediately.
    from app.workers.tasks import index_document_task
    try:
        index_document_task.delay(document_id=doc.id)
    except Exception as exc:
        # If Celery is down, we still accept the upload — extraction
        # will OCR again as a fallback.
        import logging
        logging.getLogger(__name__).warning("index task dispatch failed: %s", exc)

    return DocumentOut.model_validate(doc)


@router.get("/{tenant_id}/documents", response_model=list[DocumentOut])
def list_documents(tenant_id: str, db: Session = Depends(get_db)) -> list[DocumentOut]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    docs = sorted(tenant.documents, key=lambda d: d.document_order)
    return [DocumentOut.model_validate(d) for d in docs]


@router.delete("/{tenant_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(tenant_id: str, document_id: str, db: Session = Depends(get_db)) -> None:
    doc = db.get(Document, document_id)
    if doc is None or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        Path(doc.storage_path).unlink(missing_ok=True)
    except Exception:
        pass
    # Remove from vector store as well
    try:
        from app.services.vector_store import get_vector_store
        get_vector_store().delete_document(tenant_id, document_id)
    except Exception:
        pass
    db.delete(doc)
    db.commit()
