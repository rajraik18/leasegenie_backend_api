"""Celery tasks for async extraction + document indexing."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.db.session import SessionLocal
from app.models.orm import ExtractionJob
from app.observability import (
    CLEANUP_BYTES_RECLAIMED,
    CLEANUP_FILES_REMOVED,
    EXTRACTION_DURATION,
    EXTRACTIONS,
)
from app.services.doc_indexer import index_document_sync
from app.services.pipeline import run_extraction_for_tenant

logger = logging.getLogger(__name__)


@shared_task(
    name="leasegenie.extract_tenant",
    bind=True,
    autoretry_for=(ConnectionError, TimeoutError),
    max_retries=2,
    default_retry_delay=30,
    retry_backoff=True,
    retry_jitter=True,
)
def extract_tenant_task(
    self,
    tenant_id: str,
    job_id: str,
    schema_id: str | None = None,
) -> dict:
    started = time.perf_counter()
    db = SessionLocal()
    try:
        run_extraction_for_tenant(
            db,
            tenant_id=tenant_id,
            job_id=job_id,
            schema_id=schema_id,
        )
        EXTRACTION_DURATION.observe(time.perf_counter() - started)
        EXTRACTIONS.labels(outcome="complete").inc()
        return {"tenant_id": tenant_id, "job_id": job_id, "status": "complete"}
    except SoftTimeLimitExceeded:
        logger.warning("extraction task hit soft time limit -- aborting")
        EXTRACTIONS.labels(outcome="timeout").inc()
        job = db.get(ExtractionJob, job_id)
        if job:
            job.status = "failed"
            job.error = "task exceeded soft time limit"
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise
    except Exception as exc:
        logger.exception("extraction task failed")
        EXTRACTIONS.labels(outcome="failed").inc()
        job = db.get(ExtractionJob, job_id)
        if job:
            job.status = "failed"
            job.error = str(exc)[:4000]
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise
    finally:
        db.close()


@shared_task(
    name="leasegenie.index_document",
    bind=True,
    autoretry_for=(ConnectionError, TimeoutError),
    max_retries=3,
    default_retry_delay=20,
    retry_backoff=True,
    retry_jitter=True,
)
def index_document_task(self, document_id: str) -> dict:
    """Fired off on document upload. OCRs the PDF and populates the
    vector store so the extraction agents can semantic-search it."""
    try:
        return index_document_sync(document_id)
    except SoftTimeLimitExceeded:
        logger.warning("index_document task hit soft time limit")
        raise
    except Exception:
        logger.exception("index_document task failed")
        raise


# ---------------------------------------------------------------------------
# File-retention beat tasks
# ---------------------------------------------------------------------------

def _cleanup_dir(root: Path, *, max_age_days: int, kind: str) -> dict:
    """Walk `root` and delete files whose mtime is older than max_age_days.

    Empty directories are removed bottom-up. Returns a summary dict and
    increments the Prometheus counters.
    """
    if max_age_days <= 0 or not root.exists():
        return {"removed": 0, "bytes": 0}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp()
    removed = 0
    bytes_freed = 0

    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        try:
            if path.is_file():
                if path.stat().st_mtime < cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    bytes_freed += size
            elif path.is_dir() and path != root:
                # Remove if empty after file deletion.
                try:
                    path.rmdir()
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("retention: could not process %s: %s", path, exc)

    if removed:
        logger.info(
            "retention: removed %d %s files (%.1f MB) older than %d days",
            removed, kind, bytes_freed / (1024 * 1024), max_age_days,
        )
    CLEANUP_FILES_REMOVED.labels(kind=kind).inc(removed)
    CLEANUP_BYTES_RECLAIMED.labels(kind=kind).inc(bytes_freed)
    return {"removed": removed, "bytes": bytes_freed}


@shared_task(name="leasegenie.cleanup_old_uploads")
def cleanup_old_uploads_task() -> dict:
    """Delete uploaded PDFs whose mtime exceeds UPLOAD_RETENTION_DAYS."""
    return _cleanup_dir(
        Path(settings.upload_dir),
        max_age_days=settings.upload_retention_days,
        kind="uploads",
    )


@shared_task(name="leasegenie.cleanup_old_exports")
def cleanup_old_exports_task() -> dict:
    """Delete exported xlsx/json files whose mtime exceeds EXPORT_RETENTION_DAYS."""
    return _cleanup_dir(
        Path(settings.export_dir),
        max_age_days=settings.export_retention_days,
        kind="exports",
    )
