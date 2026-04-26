"""Celery tasks for async extraction + document indexing."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from celery import shared_task

from app.db.session import SessionLocal
from app.models.orm import ExtractionJob
from app.services.doc_indexer import index_document_sync
from app.services.pipeline import run_extraction_for_tenant

logger = logging.getLogger(__name__)


@shared_task(name="leasegenie.extract_tenant", bind=True)
def extract_tenant_task(
    self,
    tenant_id: str,
    job_id: str,
    schema_id: str | None = None,
) -> dict:
    db = SessionLocal()
    try:
        run_extraction_for_tenant(
            db,
            tenant_id=tenant_id,
            job_id=job_id,
            schema_id=schema_id,
        )
        return {"tenant_id": tenant_id, "job_id": job_id, "status": "complete"}
    except Exception as exc:
        logger.exception("extraction task failed")
        job = db.get(ExtractionJob, job_id)
        if job:
            job.status = "failed"
            job.error = str(exc)[:4000]
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise
    finally:
        db.close()


@shared_task(name="leasegenie.index_document", bind=True)
def index_document_task(self, document_id: str) -> dict:
    """Fired off on document upload. OCRs the PDF and populates the
    vector store so the extraction agents can semantic-search it."""
    try:
        return index_document_sync(document_id)
    except Exception:
        logger.exception("index_document task failed")
        raise

