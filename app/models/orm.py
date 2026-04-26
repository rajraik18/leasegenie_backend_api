"""SQLAlchemy ORM models.

Hierarchy (per BRD User Stories sheet):
    Project → Property → Tenant → Document(s) → FieldValue(s)

A Tenant has one "base lease" document and up to 7 amendments.
FieldValue rows store the extracted value for a given (tenant, field,
document) triple, plus override / concluded / audit metadata.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String, Integer, Float, DateTime, ForeignKey, Text, Boolean, JSON,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Order / Project / Property / Tenant
# ---------------------------------------------------------------------------

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    properties: Mapped[list["Property"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    property_type: Mapped[str] = mapped_column(String(32))  # Retail / Industrial / Office / Mixed-Use
    address: Mapped[str | None] = mapped_column(String(512))

    project: Mapped["Project"] = relationship(back_populates="properties")
    tenants: Mapped[list["Tenant"]] = relationship(
        back_populates="property", cascade="all, delete-orphan"
    )


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    suite_number: Mapped[str | None] = mapped_column(String(64))
    abstract_type: Mapped[str] = mapped_column(String(64))  # Basic Economic Abstract, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    property: Mapped["Property"] = relationship(back_populates="tenants")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan",
        order_by="Document.document_order",
    )
    field_values: Mapped[list["FieldValue"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024))
    document_type: Mapped[str] = mapped_column(String(32))  # "base_lease" | "amendment"
    document_order: Mapped[int] = mapped_column(Integer)   # 0 = base, 1..7 = amendments
    effective_date: Mapped[datetime | None] = mapped_column(DateTime)
    ocr_status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending | ocr_in_progress | extracting | complete | failed
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    tenant: Mapped["Tenant"] = relationship(back_populates="documents")
    field_values: Mapped[list["FieldValue"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "document_order", name="uq_tenant_doc_order"),
    )


# ---------------------------------------------------------------------------
# Field extraction results
# ---------------------------------------------------------------------------

class FieldValue(Base):
    """One extracted value for (tenant, field, document)."""
    __tablename__ = "field_values"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    field_id: Mapped[str] = mapped_column(String(128))  # slug, e.g. "annual_base_rent"

    # Extracted data
    value: Mapped[str | None] = mapped_column(Text)           # normalized string value
    raw_value: Mapped[str | None] = mapped_column(Text)       # original text from OCR
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    page_number: Mapped[int | None] = mapped_column(Integer)
    clause_number: Mapped[str | None] = mapped_column(String(64))
    clause_text: Mapped[str | None] = mapped_column(Text)     # full clause/paragraph fallback
    question_answers: Mapped[dict | None] = mapped_column(JSON)  # {question: answer, ...}

    extracted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    tenant: Mapped["Tenant"] = relationship(back_populates="field_values")
    document: Mapped["Document"] = relationship(back_populates="field_values")

    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "field_id", name="uq_fv_unique"),
        Index("ix_fv_tenant_field", "tenant_id", "field_id"),
    )


class FieldOverride(Base):
    """Manual override per (tenant, field). Takes precedence over extracted values."""
    __tablename__ = "field_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    field_id: Mapped[str] = mapped_column(String(128))
    value: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "field_id", name="uq_override_unique"),
    )


class AuditLog(Base):
    """Audit trail for field changes (BRD: keep original + last 2 edits).

    tenant_id is nullable so we can record global (non-tenant) events like
    schema uploads and deployments alongside tenant-scoped field changes.
    """
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True,
    )
    field_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(32))  # "extract" | "override" | "revert"
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(255))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_audit_tenant_field", "tenant_id", "field_id", "timestamp"),
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    schema_id: Mapped[str | None] = mapped_column(String(128))  # which user schema (null = built-in BRD)
    schema_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued | running | complete | failed
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    total_fields: Mapped[int] = mapped_column(Integer, default=0)
    completed_fields: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# ---------------------------------------------------------------------------
# Extraction schemas (user-uploaded JSON specs that override the BRD default)
# ---------------------------------------------------------------------------

class ExtractionSchema(Base):
    """User-uploaded extraction schema.

    Each schema has a stable `schema_id` slug that callers reference
    via `?schema_id=...` on extraction. Re-uploading bumps `version` and
    keeps the latest one as the active version of that slug. Old versions
    can be retrieved via `/schemas/{id}?version=...`.

    `is_active=True` marks the default schema for new extractions when
    no schema_id is specified. At most one row per is_active=True is
    enforced at the application layer (the API ensures this).
    """
    __tablename__ = "extraction_schemas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    schema_id: Mapped[str] = mapped_column(String(128))     # slug, stable across versions
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str | None] = mapped_column(Text)
    schema_json: Mapped[dict] = mapped_column(JSON)         # full uploaded JSON
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("schema_id", "version", name="uq_schema_version"),
        Index("ix_schema_active", "is_active"),
    )
