"""Assemble the final lease abstraction output + LeaseLens red flags."""
from __future__ import annotations

from collections import defaultdict
from sqlalchemy.orm import Session

from app.core.reference_data import get_reference_data
from app.models.orm import AuditLog, Document, FieldOverride, FieldValue, Tenant
from app.schemas.models import (
    AbstractedField, AbstractionSummary, DocumentOut, FieldValueOut,
    RedFlag, TenantAbstractionOut,
)
from app.services.concluded_value import (
    DocumentValueInput, compute_concluded, confidence_band,
)


def build_abstraction(db: Session, tenant_id: str) -> TenantAbstractionOut:
    tenant: Tenant | None = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"tenant not found: {tenant_id}")

    property_ = tenant.property
    ref = get_reference_data()
    in_scope = ref.list_fields(
        abstract_type=tenant.abstract_type,
        property_type=property_.property_type,
    )

    docs = sorted(tenant.documents, key=lambda d: d.document_order)

    fv_index: dict[tuple[str, str], FieldValue] = {
        (fv.field_id, fv.document_id): fv for fv in tenant.field_values
    }

    override_index: dict[str, FieldOverride] = {
        ov.field_id: ov
        for ov in db.query(FieldOverride).filter(FieldOverride.tenant_id == tenant_id).all()
    }

    fields_out: list[AbstractedField] = []
    for fc in in_scope:
        per_doc: list[FieldValueOut] = []
        for d in docs:
            fv = fv_index.get((fc.field_id, d.id))
            qa = (fv.question_answers or {}) if fv else {}
            per_doc.append(FieldValueOut(
                document_id=d.id,
                document_label=_doc_label(d),
                value=(fv.value if fv else "None"),
                confidence=(fv.confidence if fv else 0.0),
                page_number=(fv.page_number if fv else None),
                clause_number=(fv.clause_number if fv else None),
                clause_text=(fv.clause_text if fv else None),
                condition_type_taken=qa.get("condition_type_taken"),
                red_flags=list(qa.get("red_flags", []) or []),
                needs_review=bool(qa.get("needs_review", False)),
                cross_field_notes=list(qa.get("cross_field_notes", []) or []),
                trace_summary=qa.get("trace"),
            ))

        ov = override_index.get(fc.field_id)
        override_value = ov.value if ov else None
        override_present = ov is not None

        # Feed citation + confidence into the concluded-value engine so the
        # winning document's page/clause travel through to the API response.
        inputs = [
            _to_input(d, fv_index.get((fc.field_id, d.id)))
            for d in docs
        ]
        concluded = compute_concluded(inputs, override_value, override_present)

        fields_out.append(AbstractedField(
            field_id=fc.field_id,
            name=fc.name,
            category=fc.category,
            output_type=fc.output_type,
            per_document=per_doc,
            override_value=override_value,
            concluded_value=concluded.concluded_value,
            concluded_source=concluded.concluded_source,
            concluded_confidence=round(concluded.confidence, 3),
            confidence_level=confidence_band(concluded.confidence),
            source_document_label=concluded.source_document_label,
            source_document_id=concluded.source_document_id,
            page_number=concluded.page_number,
            clause_number=concluded.clause_number,
            clause_text=concluded.clause_text,
        ))

    summary = _build_summary(fields_out)

    return TenantAbstractionOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        suite_number=tenant.suite_number,
        abstract_type=tenant.abstract_type,
        property_type=property_.property_type,
        documents=[DocumentOut.model_validate(d) for d in docs],
        fields=fields_out,
        summary=summary,
    )


def _to_input(d: Document, fv: FieldValue | None) -> DocumentValueInput:
    return DocumentValueInput(
        document_id=d.id,
        document_label=_doc_label(d),
        document_type=d.document_type,
        document_order=d.document_order,
        effective_date=d.effective_date,
        value=(fv.value if fv else None),
        confidence=(fv.confidence if fv else 0.0),
        page_number=(fv.page_number if fv else None),
        clause_number=(fv.clause_number if fv else None),
        clause_text=(fv.clause_text if fv else None),
    )


def _build_summary(fields: list[AbstractedField]) -> AbstractionSummary:
    total = len(fields)
    extracted = [f for f in fields
                 if f.concluded_value and f.concluded_value.lower() != "none"]
    none_count = total - len(extracted)
    overridden = sum(1 for f in fields if f.concluded_source == "override")
    flagged = sum(
        1 for f in fields
        for pd in f.per_document if pd.needs_review
    )

    confidences = [f.concluded_confidence for f in extracted]
    mean_conf = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

    high = sum(1 for f in extracted if f.concluded_confidence >= 0.8)
    medium = sum(1 for f in extracted if 0.5 <= f.concluded_confidence < 0.8)
    low = sum(1 for f in extracted if 0 < f.concluded_confidence < 0.5)

    return AbstractionSummary(
        total_fields=total,
        fields_extracted=len(extracted),
        fields_none=none_count,
        fields_overridden=overridden,
        fields_flagged_review=flagged,
        mean_confidence=mean_conf,
        high_confidence_count=high,
        medium_confidence_count=medium,
        low_confidence_count=low,
    )


def _doc_label(d: Document) -> str:
    if d.document_type == "base_lease":
        return "Base Lease"
    return f"Amendment {d.document_order}"


# ---------------------------------------------------------------------------
# LeaseLens red-flag detection
# ---------------------------------------------------------------------------

def detect_red_flags(db: Session, tenant_id: str) -> list[RedFlag]:
    """Detect BRD-specified inconsistencies across documents and versus the
    rent roll. Pulls:
      * Reconciliation agent red flags from the audit log (action='red_flag')
      * Playbook-declared red flags from stored field values
      * Legacy rule-based checks (RSF mismatch, suite conflict, etc.)
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return []

    flags: list[RedFlag] = []

    # 1. Reconciliation agent flags persisted to audit log
    recon_rows = (
        db.query(AuditLog)
        .filter(AuditLog.tenant_id == tenant_id, AuditLog.action == "red_flag")
        .order_by(AuditLog.timestamp.desc())
        .all()
    )
    for row in recon_rows:
        # Messages look like "[CODE/severity] message"
        msg = row.new_value or ""
        code = "RECONCILIATION"
        severity = "info"
        message = msg
        if msg.startswith("["):
            close = msg.find("]")
            if close > 0:
                token = msg[1:close]
                if "/" in token:
                    code, severity = token.split("/", 1)
                message = msg[close + 1:].strip()
        flags.append(RedFlag(
            code=code,
            severity=severity,
            message=message,
            field_ids=[fid for fid in (row.field_id or "").split(",") if fid and fid != "_global"],
        ))

    # 2. Playbook-declared per-field red flags stashed in FieldValue.question_answers
    for fv in tenant.field_values:
        if not fv.question_answers:
            continue
        for rf in (fv.question_answers or {}).get("red_flags", []) or []:
            flags.append(RedFlag(
                code="PLAYBOOK_RED_FLAG",
                severity="info",
                message=rf,
                field_ids=[fv.field_id],
            ))
        if (fv.question_answers or {}).get("needs_review"):
            flags.append(RedFlag(
                code="MANUAL_REVIEW",
                severity="warning",
                message=f"{fv.field_id} flagged by playbook for manual review",
                field_ids=[fv.field_id],
            ))

    # 3. Legacy rule-based checks (kept for defence-in-depth)
    by_field: dict[str, list[FieldValue]] = defaultdict(list)
    for fv in tenant.field_values:
        by_field[fv.field_id].append(fv)

    # RSF mismatch across documents
    for fid in ("leased_rsf", "rentable_square_footage_rsf"):
        values = _distinct_nonempty([fv.value for fv in by_field.get(fid, [])])
        if len(values) > 1:
            flags.append(RedFlag(
                code="RSF_MISMATCH",
                severity="warning",
                message=f"Rentable Square Footage differs across documents: {values}",
                field_ids=[fid],
                context={"values": values},
            ))

    # Suite conflicts
    suite_values = _distinct_nonempty([fv.value for fv in by_field.get("suite", [])])
    if len(suite_values) > 1:
        flags.append(RedFlag(
            code="SUITE_CONFLICT",
            severity="warning",
            message=f"Suite number varies across documents: {suite_values}",
            field_ids=["suite"],
            context={"values": suite_values},
        ))

    # Tenant name spelling variations
    name_values = _distinct_nonempty([fv.value for fv in by_field.get("tenant_name", [])])
    if len(name_values) > 1:
        flags.append(RedFlag(
            code="NAME_VARIATION",
            severity="info",
            message=f"Tenant name varies across documents: {name_values}",
            field_ids=["tenant_name"],
            context={"values": name_values},
        ))

    # Date inconsistencies — expiration before commencement
    led = _first_value(by_field.get("lease_expiration_date", []))
    lcd = _first_value(by_field.get("original_lease_commencement_date", []))
    if led and lcd and led < lcd:
        flags.append(RedFlag(
            code="DATE_INCONSISTENCY",
            severity="critical",
            message=f"Lease Expiration ({led}) precedes Lease Commencement ({lcd})",
            field_ids=["lease_expiration_date", "original_lease_commencement_date"],
        ))

    # Low confidence extractions
    low_conf = [fv for fv in tenant.field_values if 0 < fv.confidence < 0.5]
    if low_conf:
        flags.append(RedFlag(
            code="LOW_CONFIDENCE",
            severity="info",
            message=f"{len(low_conf)} fields extracted with low confidence",
            field_ids=sorted({fv.field_id for fv in low_conf}),
        ))

    return flags


def _distinct_nonempty(values: list[str | None]) -> list[str]:
    return sorted({(v or "").strip() for v in values if v and (v or "").strip() and v.lower() != "none"})


def _first_value(fvs: list[FieldValue]) -> str | None:
    for fv in fvs:
        if fv.value and fv.value.lower() != "none":
            return fv.value
    return None
