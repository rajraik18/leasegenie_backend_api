"""Abstraction API.

The main deliverable endpoint: returns the lease abstraction grid for a
tenant — each in-scope field × (base lease | amendments) + override +
concluded value. Also exposes override PATCH, audit log, and LeaseLens
red flags.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.orm import AuditLog, FieldOverride, Tenant
from app.schemas.models import (
    AuditLogOut, FieldOverrideIn, RedFlag, TenantAbstractionOut,
)
from app.services.abstraction import build_abstraction, detect_red_flags

router = APIRouter(prefix="/tenants", tags=["abstraction"])


@router.get("/{tenant_id}/abstraction", response_model=TenantAbstractionOut)
def get_abstraction(tenant_id: str, db: Session = Depends(get_db)) -> TenantAbstractionOut:
    try:
        return build_abstraction(db, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{tenant_id}/fields/{field_id}", response_model=TenantAbstractionOut)
def set_override(
    tenant_id: str,
    field_id: str,
    payload: FieldOverrideIn,
    db: Session = Depends(get_db),
) -> TenantAbstractionOut:
    """Set or clear a manual override for a field. Writes audit log."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    existing = (
        db.query(FieldOverride)
        .filter(FieldOverride.tenant_id == tenant_id, FieldOverride.field_id == field_id)
        .one_or_none()
    )
    old_value = existing.value if existing else None

    if payload.value is None:
        # Clear override
        if existing is not None:
            db.delete(existing)
            db.add(AuditLog(
                tenant_id=tenant_id,
                field_id=field_id,
                action="revert",
                old_value=old_value,
                new_value=None,
                actor=payload.actor or "user",
            ))
    else:
        if existing is None:
            existing = FieldOverride(
                tenant_id=tenant_id,
                field_id=field_id,
                value=payload.value,
                comment=payload.comment,
                updated_by=payload.actor,
            )
            db.add(existing)
        else:
            existing.value = payload.value
            existing.comment = payload.comment
            existing.updated_by = payload.actor
        db.add(AuditLog(
            tenant_id=tenant_id,
            field_id=field_id,
            action="override",
            old_value=old_value,
            new_value=payload.value,
            actor=payload.actor or "user",
        ))

    db.commit()
    return build_abstraction(db, tenant_id)


@router.get("/{tenant_id}/audit", response_model=list[AuditLogOut])
def get_audit(
    tenant_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[AuditLogOut]:
    """Audit trail — BRD rule: keep original + last 2 edits visible."""
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [AuditLogOut.model_validate(r) for r in rows]


@router.get("/{tenant_id}/red-flags", response_model=list[RedFlag])
def get_red_flags(tenant_id: str, db: Session = Depends(get_db)) -> list[RedFlag]:
    """LeaseLens — inconsistencies across base lease + amendments."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return detect_red_flags(db, tenant_id)
