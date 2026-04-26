"""Background task: on document upload, OCR the PDF and index every clause
into the vector store so agents can semantic-search it later.

Runs asynchronously so the upload endpoint returns immediately. If OCR or
indexing fails, we log but don't block — the extraction pipeline will OCR
again as a fallback.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.orm import Document
from app.services.ocr import extract_document_text, segment_clauses
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


def index_document_sync(document_id: str) -> dict:
    """OCR one document and index its clauses in the vector store.
    Called from the Celery task wrapper. Returns a small status dict."""
    db: Session = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if doc is None:
            return {"status": "not_found", "document_id": document_id}

        tenant_id = doc.tenant_id

        # Skip if already indexed (idempotent)
        vs = get_vector_store()
        if vs.has_document(tenant_id, document_id):
            logger.info("document %s already indexed — skipping", document_id)
            return {"status": "already_indexed", "document_id": document_id}

        # OCR
        try:
            doc.ocr_status = "ocr_in_progress"
            db.commit()
            doc_text = extract_document_text(Path(doc.storage_path))
            clauses = segment_clauses(doc_text)
        except Exception as exc:
            logger.exception("OCR failed for %s", document_id)
            doc.ocr_status = "failed"
            db.commit()
            return {"status": "ocr_failed", "document_id": document_id, "error": str(exc)}

        # Vector index
        doc_label = "base_lease" if doc.document_type == "base_lease" else f"amendment_{doc.document_order}"
        try:
            count = vs.index_document(
                tenant_id=tenant_id,
                document_id=document_id,
                document_label=doc_label,
                document_type=doc.document_type,
                clauses=clauses,
            )
        except Exception as exc:
            logger.exception("vector indexing failed for %s", document_id)
            doc.ocr_status = "failed"
            db.commit()
            return {"status": "index_failed", "document_id": document_id, "error": str(exc)}

        doc.ocr_status = "indexed"
        db.commit()
        return {"status": "indexed", "document_id": document_id, "clauses": count}
    finally:
        db.close()
