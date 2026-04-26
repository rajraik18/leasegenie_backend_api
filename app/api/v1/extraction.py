"""Extraction API — trigger agentic extraction and poll for status."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.orm import ExtractionJob, Tenant
from app.schemas.models import JobOut
from app.workers.tasks import extract_tenant_task

router = APIRouter(tags=["extraction"])


@router.post(
    "/tenants/{tenant_id}/extract",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_extraction(tenant_id: str, db: Session = Depends(get_db)) -> JobOut:
    """Kick off the agentic extraction for a tenant.

    Creates an ExtractionJob row, enqueues a Celery task (or runs it eagerly
    when CELERY_TASK_ALWAYS_EAGER=true), and returns the job id for polling.
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    if not tenant.documents:
        raise HTTPException(status_code=409, detail="tenant has no documents uploaded")

    job = ExtractionJob(tenant_id=tenant_id, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)

    extract_tenant_task.delay(tenant_id=tenant_id, job_id=job.id)

    # When eager mode is on, the task already completed — refresh.
    db.refresh(job)
    return JobOut.model_validate(job)


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    summary="3. Check extraction job progress",
    description=(
        "Poll this endpoint until `status` is `complete` or `failed`. "
        "Use `progress` (0-100) for a UI progress bar.\n\n"
        "**Status values:** `queued` → `running` → `complete` (or `failed`).\n\n"
        "Once complete, fetch the result via "
        "`GET /api/v1/extract/jobs/{job_id}/result?format=json|xlsx`."
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "id": "8e1d4a3c-7c2f-4b8a-9e3d-2f1a8b7c6d5e",
                        "tenant_id": "1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f",
                        "schema_id": None,
                        "schema_version": None,
                        "status": "running",
                        "progress": 47,
                        "total_fields": 79,
                        "completed_fields": 37,
                        "error": None,
                        "started_at": "2026-04-26T05:30:05Z",
                        "finished_at": None,
                        "created_at": "2026-04-26T05:30:00Z",
                    }
                }
            }
        },
        404: {"description": "Job not found"},
    },
)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    job = db.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobOut.model_validate(job)
