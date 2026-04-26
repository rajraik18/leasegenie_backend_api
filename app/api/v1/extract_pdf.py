"""End-to-end wrapper endpoints.

POST /extract/pdf
    Multipart PDF upload → creates a throwaway tenant → uploads the PDF
    as the base lease → triggers extraction → returns 202 + job_id.

    Also accepts multiple PDFs in a single call: the first is treated as
    the base lease, the rest as amendments in order. Ideal for demo + CLI
    use where the caller just wants to hand over PDFs and get results.

    Query params:
      property_type  — Retail | Industrial | Office | Mixed-Use
      abstract_type  — Basic Economic Abstract | Financial Terms |
                       Short Form Abstract | Economic | Full Abstract
      tenant_name    — optional display name (defaults to 'Anonymous Tenant')

GET /jobs/{job_id}/result?format=json|xlsx
    Returns the final abstraction once the job is complete. Format:
      json — the full TenantAbstractionOut (with confidence + citation)
      xlsx — a downloadable Excel file matching BRD Lease Abstraction
             sheet layout, with summary, grid, and red flags sheets.
"""
from __future__ import annotations

import logging
import re
import shutil
import urllib.parse
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.orm import Document, ExtractionJob, Project, Property, Tenant
from app.schemas.models import JobOut, TenantAbstractionOut
from app.services.abstraction import build_abstraction
from app.services.excel_export import to_bytes as excel_bytes
from app.workers.tasks import extract_tenant_task, index_document_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract", tags=["extract"])

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(raw: str | None, fallback: str) -> str:
    """Strip directory components and reduce to [A-Za-z0-9._-]."""
    base = Path(raw or fallback).name or fallback
    cleaned = _SAFE_FILENAME_RE.sub("_", base).strip("._") or fallback
    return cleaned[:200]


@router.post(
    "/pdf",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="2. Upload one or more PDFs and trigger extraction",
    description=(
        "Single-call wrapper that creates a project/property/tenant "
        "behind the scenes, writes uploaded PDFs to disk, and dispatches "
        "an async extraction job.\n\n"
        "**File ordering:** the first file is treated as the base lease; "
        "subsequent files are amendments in upload order (max 7).\n\n"
        "**Schema selection:** if you uploaded a schema via "
        "`POST /api/v1/schemas`, pass `?schema_id=<your_id>` to extract "
        "only the fields it specifies. Without it, falls back to the "
        "active default schema (if any), then to the full 79-field BRD set.\n\n"
        "**Returns:** `202 Accepted` with `{job_id, status: 'queued'}`. "
        "Poll `GET /api/v1/jobs/{job_id}` for progress; download results "
        "from `GET /api/v1/extract/jobs/{job_id}/result?format=json|xlsx`."
    ),
    responses={
        202: {
            "description": "Extraction job queued",
            "content": {
                "application/json": {
                    "example": {
                        "id": "8e1d4a3c-7c2f-4b8a-9e3d-2f1a8b7c6d5e",
                        "tenant_id": "1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f",
                        "schema_id": "acme_retail_v1",
                        "schema_version": "1.0.0",
                        "status": "queued",
                        "progress": 0,
                        "total_fields": 0,
                        "completed_fields": 0,
                        "error": None,
                        "started_at": None,
                        "finished_at": None,
                        "created_at": "2026-04-26T05:30:00Z",
                    }
                }
            },
        },
        400: {"description": "Invalid input (property_type, abstract_type, or file count)"},
        404: {"description": "schema_id specified but not found"},
        429: {"description": "Rate limit exceeded (see RATE_LIMIT_EXTRACT in .env)"},
    },
)
def extract_pdf(
    request: Request,
    property_type: str = Query(..., description="Retail | Industrial | Office | Mixed-Use"),
    abstract_type: str = Query("Full Abstract"),
    tenant_name: str = Query("Anonymous Tenant"),
    schema_id: str | None = Query(
        None,
        description=(
            "Optional. If supplied, runs only the fields defined in the "
            "specified user schema (uploaded via POST /schemas). When omitted, "
            "uses the active default schema if one is set, otherwise falls back "
            "to the full 79-field BRD playbook set."
        ),
    ),
    files: list[UploadFile] = File(..., description="Base lease first, then amendments"),
    db: Session = Depends(get_db),
) -> JobOut:
    """Single-call wrapper: PDFs → extraction job_id.

    Creates a project/property/tenant behind the scenes and kicks off
    async extraction. Client polls GET /jobs/{id} for progress and
    GET /jobs/{id}/result for the final output.
    """
    if property_type not in ("Retail", "Industrial", "Office", "Mixed-Use"):
        raise HTTPException(status_code=400, detail="invalid property_type")
    if abstract_type not in (
        "Basic Economic Abstract", "Financial Terms", "Short Form Abstract",
        "Economic", "Full Abstract",
    ):
        raise HTTPException(status_code=400, detail="invalid abstract_type")
    if not files:
        raise HTTPException(status_code=400, detail="at least one PDF is required")
    if len(files) > settings.max_pdfs_per_request:
        raise HTTPException(
            status_code=400,
            detail=(
                f"too many PDFs — limit is {settings.max_pdfs_per_request} "
                f"(1 base lease + {settings.max_pdfs_per_request - 1} amendments)"
            ),
        )

    # Per-file MIME and size validation. We check each file's content_type
    # AND extension because UploadFile.content_type is sometimes wrong on
    # Windows clients. Also accumulate total size to enforce the per-request
    # cap before writing anything to disk.
    per_file_limit = settings.max_upload_size_mb * 1024 * 1024
    total_limit = settings.max_upload_total_mb * 1024 * 1024
    total_size = 0
    for idx, upload in enumerate(files):
        # MIME / extension check
        ct = (upload.content_type or "").lower()
        fname = (upload.filename or "").lower()
        if not (ct == "application/pdf" or fname.endswith(".pdf")):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"file #{idx + 1} ('{upload.filename}'): only PDF files "
                    f"are accepted (got content_type='{upload.content_type}')"
                ),
            )

        # Size check — UploadFile.size is set by Starlette when the multipart
        # framework can determine it. If not, we'd need to read the stream;
        # we do that defensively below during write.
        size = getattr(upload, "size", None)
        if size is not None:
            if size > per_file_limit:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"file #{idx + 1} ('{upload.filename}') is "
                        f"{size / (1024*1024):.1f} MB — exceeds "
                        f"per-file limit of {settings.max_upload_size_mb} MB"
                    ),
                )
            total_size += size

    if total_size > total_limit:
        raise HTTPException(
            status_code=413,
            detail=(
                f"total upload size is {total_size / (1024*1024):.1f} MB — "
                f"exceeds per-request limit of {settings.max_upload_total_mb} MB"
            ),
        )

    # Resolve which schema to run (if any). schema_id from query takes
    # precedence; if none, fall back to the active default; if no default,
    # use the built-in BRD playbooks (schema_id stays None).
    resolved_schema_id: str | None = None
    resolved_schema_version: str | None = None
    if schema_id:
        from app.services.schema_store import SchemaNotFound, get_schema
        try:
            schema_row = get_schema(db, schema_id)
        except SchemaNotFound:
            raise HTTPException(
                status_code=404,
                detail=f"schema_id '{schema_id}' not found",
            )
        resolved_schema_id = schema_row.schema_id
        resolved_schema_version = schema_row.version
    else:
        from app.services.schema_store import get_active_schema
        active = get_active_schema(db)
        if active is not None:
            resolved_schema_id = active.schema_id
            resolved_schema_version = active.version

    # Auto-create project/property/tenant for this one-off extraction
    project = Project(name=f"Ad-hoc extract {uuid.uuid4().hex[:8]}")
    db.add(project)
    db.flush()

    prop = Property(
        project_id=project.id,
        name=tenant_name,
        property_type=property_type,
    )
    db.add(prop)
    db.flush()

    tenant = Tenant(
        property_id=prop.id,
        name=tenant_name,
        suite_number=None,
        abstract_type=abstract_type,
    )
    db.add(tenant)
    db.flush()

    # Write each PDF to disk + create Document rows
    dest_dir = settings.upload_dir / tenant.id
    dest_dir.mkdir(parents=True, exist_ok=True)

    for idx, upload in enumerate(files):
        doc_type = "base_lease" if idx == 0 else "amendment"
        document_order = 0 if idx == 0 else idx
        safe_name = _safe_filename(upload.filename, f"doc-{idx}.pdf")
        path = dest_dir / f"{document_order:02d}_{safe_name}"

        # Stream-write with size enforcement. If upload.size was None during
        # validation, this is the only place we'll catch oversized files.
        bytes_written = 0
        chunk_size = 1024 * 1024  # 1 MB chunks
        with path.open("wb") as fh:
            while True:
                chunk = upload.file.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > per_file_limit:
                    fh.close()
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"file #{idx + 1} ('{upload.filename}') exceeds "
                            f"per-file limit of {settings.max_upload_size_mb} MB "
                            f"(detected during upload streaming)"
                        ),
                    )
                fh.write(chunk)

        doc = Document(
            tenant_id=tenant.id,
            filename=upload.filename or f"doc-{idx}.pdf",
            storage_path=str(path),
            document_type=doc_type,
            document_order=document_order,
            effective_date=None,
            ocr_status="pending",
        )
        db.add(doc)
        db.flush()

        # Kick off vector indexing in background
        try:
            index_document_task.delay(document_id=doc.id)
        except Exception as exc:
            logger.warning("index task dispatch failed: %s", exc)

    # Create extraction job + enqueue task
    job = ExtractionJob(
        tenant_id=tenant.id,
        status="queued",
        schema_id=resolved_schema_id,
        schema_version=resolved_schema_version,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        extract_tenant_task.delay(
            tenant_id=tenant.id,
            job_id=job.id,
            schema_id=resolved_schema_id,
        )
    except Exception as exc:
        logger.exception("extract task dispatch failed for job %s", job.id)
        job.status = "failed"
        job.error = f"could not enqueue extraction task: {exc}"[:4000]
        db.commit()
        # Clean up the on-disk uploads -- the job will never run, so the
        # PDFs are dead weight. Documents rows stay (the caller can
        # see the failed job + tenant in the DB and delete via the
        # documents API if they want).
        import shutil as _shutil
        try:
            _shutil.rmtree(dest_dir, ignore_errors=True)
        except OSError:
            pass
        raise HTTPException(
            status_code=503,
            detail="extraction queue unavailable — try again shortly",
        )
    db.refresh(job)

    return JobOut.model_validate(job)


@router.get(
    "/jobs/{job_id}/result",
    summary="4. Download final extraction (JSON or Excel)",
    description=(
        "Once `GET /api/v1/jobs/{job_id}` returns `status: 'complete'`, "
        "fetch the abstraction from this endpoint.\n\n"
        "**Formats:**\n"
        "- `json` — full `TenantAbstractionOut` with confidence + citation "
        "per field, plus red flags from reconciliation\n"
        "- `xlsx` — downloadable Excel matching BRD Lease Abstraction "
        "sheet layout (summary, grid, red flags sheets)"
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "tenant_id": "1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f",
                        "tenant_name": "Acme Corp",
                        "property_type": "Industrial",
                        "fields": [
                            {
                                "field_id": "annual_base_rent",
                                "field_name": "Annual Base Rent",
                                "value": "$369,887.88",
                                "confidence": 0.95,
                                "page_number": 4,
                                "clause_number": "3.1",
                                "needs_review": False,
                            }
                        ],
                        "red_flags": [],
                    }
                },
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
            },
            "description": "Extraction result",
        },
        404: {"description": "Job or tenant not found"},
        409: {"description": "Job not complete yet (poll status first)"},
    },
)
def get_job_result(
    job_id: str,
    format: str = Query("json", description="'json' or 'xlsx'"),
    db: Session = Depends(get_db),
):
    """Fetch the extraction for a job as JSON or a downloadable Excel file."""
    if format not in ("json", "xlsx"):
        raise HTTPException(status_code=400, detail="format must be 'json' or 'xlsx'")

    job = db.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "complete":
        raise HTTPException(
            status_code=409,
            detail=f"job status is '{job.status}' — not ready. progress={job.progress}%",
        )

    abstraction = build_abstraction(db, job.tenant_id)

    if format == "json":
        return abstraction

    # xlsx
    try:
        blob = excel_bytes(abstraction)
    except Exception as exc:
        logger.exception("excel export failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"excel export failed: {exc}")

    raw_name = f"lease_abstraction_{abstraction.tenant_name}_{job_id[:8]}.xlsx"
    ascii_name = _safe_filename(raw_name, f"lease_abstraction_{job_id[:8]}.xlsx")
    utf8_name = urllib.parse.quote(raw_name, safe="")
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
        },
    )
