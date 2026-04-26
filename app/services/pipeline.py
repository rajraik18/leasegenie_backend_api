"""Extraction pipeline — glue between the DB/API layer and the multi-agent
Coordinator.

Loads a tenant's documents from the DB, hands them to the Coordinator, and
persists per-field outcomes + reconciliation red flags back through the ORM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.agents.coordinator import AgentFieldResult, Coordinator, DocumentInput
from app.models.orm import (
    AuditLog, ExtractionJob, FieldValue, Tenant,
)

logger = logging.getLogger(__name__)


def run_extraction_for_tenant(
    db: Session,
    tenant_id: str,
    job_id: str | None = None,
    schema_id: str | None = None,
) -> None:
    """Extract every in-scope field for every document of `tenant` using the
    multi-agent Coordinator.

    If `schema_id` is provided, only the playbooks selected by that schema
    are run. Otherwise the full BRD playbook set is used.
    """
    tenant: Tenant | None = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"tenant not found: {tenant_id}")

    property_ = tenant.property
    docs = sorted(tenant.documents, key=lambda d: d.document_order)
    if not docs:
        logger.info("tenant %s has no documents", tenant_id)
        _complete_job(db, job_id, total=0, done=0)
        return

    # Resolve schema document (if any)
    schema_doc: dict | None = None
    if schema_id:
        from app.services.schema_store import SchemaNotFound, get_schema
        try:
            schema_row = get_schema(db, schema_id)
            schema_doc = schema_row.schema_json
            logger.info(
                "extracting against schema '%s' v%s (%d fields requested)",
                schema_row.schema_id, schema_row.version,
                len(schema_doc.get("fields", []) or []),
            )
        except SchemaNotFound:
            logger.warning(
                "schema_id '%s' not found at extraction time — falling back to BRD default",
                schema_id,
            )
            schema_doc = None

    for d in docs:
        d.ocr_status = "ocr_in_progress"

    # Wipe the per-document field_value slate so a re-run that extracts
    # fewer fields than a prior run doesn't leave stale rows behind.
    # Per-field upserts in _persist_field_value() will refill them.
    doc_ids = [d.id for d in docs]
    db.execute(
        delete(FieldValue).where(
            FieldValue.tenant_id == tenant_id,
            FieldValue.document_id.in_(doc_ids),
        )
    )
    db.commit()

    doc_inputs = [
        DocumentInput(
            document_id=d.id,
            document_type=d.document_type,
            document_order=d.document_order,
            storage_path=Path(d.storage_path),
        )
        for d in docs
    ]

    _start_job(db, job_id, total=0)

    coordinator = Coordinator()

    def on_progress(done: int, total: int, current_field_id: str) -> None:
        _bump_progress(db, job_id, done, total)

    def on_result(res: AgentFieldResult) -> None:
        _persist_field_value(db, tenant_id=tenant_id, result=res)

    try:
        final_results, report = coordinator.run(
            tenant_id=tenant_id,
            abstract_type=tenant.abstract_type,
            property_type=property_.property_type,
            documents=doc_inputs,
            schema_doc=schema_doc,
            on_progress=on_progress,
            on_result=on_result,
        )
    except Exception as exc:
        logger.exception("coordinator failed for tenant %s", tenant_id)
        _fail_job(db, job_id, error=str(exc))
        for d in docs:
            d.ocr_status = "failed"
        db.commit()
        raise

    for d in docs:
        d.ocr_status = "complete"
    db.commit()

    # Log reconciliation summary
    if report is not None:
        logger.info(
            "Reconciliation: %d fields, %d low-confidence, %d need review, %d red flags",
            report.total_fields,
            report.low_confidence_count,
            report.needs_review_count,
            len(report.red_flags),
        )
        # Persist red flags to audit log so LeaseLens endpoint can read them
        for rf in report.red_flags:
            db.add(AuditLog(
                tenant_id=tenant_id,
                field_id=",".join(rf.field_ids) if rf.field_ids else "_global",
                action="red_flag",
                old_value=None,
                new_value=f"[{rf.code}/{rf.severity}] {rf.message}",
                actor="reconciliation_agent",
            ))
        db.commit()

    _complete_job(db, job_id, total=None, done=None)
    logger.info("extraction complete: tenant=%s docs=%d fields=%d",
                tenant_id, len(docs), len({r.field_id for r in final_results}))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist_field_value(
    db: Session,
    tenant_id: str,
    result: AgentFieldResult,
) -> None:
    db.execute(
        delete(FieldValue).where(
            FieldValue.tenant_id == tenant_id,
            FieldValue.document_id == result.document_id,
            FieldValue.field_id == result.field_id,
        )
    )
    fv = FieldValue(
        tenant_id=tenant_id,
        document_id=result.document_id,
        field_id=result.field_id,
        value=result.value,
        raw_value=result.raw_value,
        confidence=result.confidence,
        page_number=result.page_number,
        clause_number=result.clause_number,
        clause_text=result.clause_text,
        question_answers={
            "category": result.category,
            "condition_type_taken": result.condition_type_taken,
            "output_type": result.output_type,
            "source_doc": result.source_doc,
            "red_flags": result.red_flags,
            "needs_review": result.needs_review,
            "cross_field_notes": result.cross_field_notes,
            "trace": result.trace_summary,
        },
    )
    db.add(fv)
    db.add(AuditLog(
        tenant_id=tenant_id,
        field_id=result.field_id,
        action="extract",
        old_value=None,
        new_value=result.value,
        actor=f"specialist:{result.category}",
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Job status helpers
# ---------------------------------------------------------------------------

def _get_job(db: Session, job_id: str | None) -> ExtractionJob | None:
    if not job_id:
        return None
    return db.get(ExtractionJob, job_id)


def _start_job(db: Session, job_id: str | None, total: int) -> None:
    job = _get_job(db, job_id)
    if job is None:
        return
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.total_fields = total
    job.progress = 0
    job.completed_fields = 0
    db.commit()


def _bump_progress(db: Session, job_id: str | None, done: int, total: int) -> None:
    job = _get_job(db, job_id)
    if job is None:
        return
    job.completed_fields = done
    job.total_fields = total
    job.progress = int(100 * done / total) if total else 0
    db.commit()


def _complete_job(db: Session, job_id: str | None, total: int | None, done: int | None) -> None:
    job = _get_job(db, job_id)
    if job is None:
        return
    job.status = "complete"
    job.finished_at = datetime.now(timezone.utc)
    if total is not None:
        job.total_fields = total
    if done is not None:
        job.completed_fields = done
    job.progress = 100
    db.commit()


def _fail_job(db: Session, job_id: str | None, error: str) -> None:
    job = _get_job(db, job_id)
    if job is None:
        return
    job.status = "failed"
    job.error = error
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
