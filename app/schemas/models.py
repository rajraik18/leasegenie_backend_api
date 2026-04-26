"""Pydantic schemas for request/response bodies."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


AbstractType = Literal[
    "Basic Economic Abstract",
    "Financial Terms",
    "Short Form Abstract",
    "Economic",
    "Full Abstract",
]
PropertyType = Literal["Retail", "Industrial", "Office", "Mixed-Use"]
DocumentType = Literal["base_lease", "amendment"]


# ---------------------------------------------------------------------------
# Field configuration
# ---------------------------------------------------------------------------

class FieldConfigOut(BaseModel):
    field_id: str
    name: str
    category: str
    output_type: str
    keyword_count: int
    question_count: int
    abstract_applicability: dict[str, bool]
    property_applicability: dict[str, bool]


class FieldQuestionOut(BaseModel):
    question: str
    extract: str | None
    output: str | None
    priority: int | None


class FieldDetailOut(FieldConfigOut):
    keywords: list[str]
    questions: list[FieldQuestionOut]


# ---------------------------------------------------------------------------
# Orders / Projects / Properties / Tenants
# ---------------------------------------------------------------------------

class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    suite_number: str | None = None
    abstract_type: AbstractType = "Full Abstract"


class PropertyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    property_type: PropertyType
    address: str | None = None
    tenants: list[TenantIn] = Field(default_factory=list)


class OrderCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    properties: list[PropertyIn] = Field(min_length=1)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    suite_number: str | None
    abstract_type: str


class PropertyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    property_type: str
    address: str | None
    tenants: list[TenantOut]


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    created_at: datetime
    properties: list[PropertyOut]


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    filename: str
    document_type: str
    document_order: int
    effective_date: datetime | None
    ocr_status: str
    uploaded_at: datetime


# ---------------------------------------------------------------------------
# Extraction jobs
# ---------------------------------------------------------------------------

class JobOut(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "8e1d4a3c-7c2f-4b8a-9e3d-2f1a8b7c6d5e",
                "tenant_id": "1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f",
                "schema_id": "acme_retail_v1",
                "schema_version": "1.2.0",
                "status": "running",
                "progress": 47,
                "total_fields": 79,
                "completed_fields": 37,
                "error": None,
                "started_at": "2026-04-26T05:30:05Z",
                "finished_at": None,
                "created_at": "2026-04-26T05:30:00Z",
            }
        },
    )
    id: str
    tenant_id: str
    schema_id: str | None = None
    schema_version: str | None = None
    status: str
    progress: int
    total_fields: int
    completed_fields: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Abstraction output (the main deliverable)
# ---------------------------------------------------------------------------

class FieldValueOut(BaseModel):
    document_id: str
    document_label: str           # "Base Lease" | "Amendment 1" | ...
    value: str | None
    confidence: float
    page_number: int | None
    clause_number: str | None
    clause_text: str | None
    # Agentic extraction metadata
    condition_type_taken: str | None = None
    red_flags: list[str] = Field(default_factory=list)
    needs_review: bool = False
    cross_field_notes: list[str] = Field(default_factory=list)
    trace_summary: str | None = None


class AbstractedField(BaseModel):
    """One row of the lease abstraction grid — base lease + amendments +
    override + concluded value + confidence + citation."""
    field_id: str
    name: str
    category: str
    output_type: str
    per_document: list[FieldValueOut]   # ordered: base lease, amendment 1, 2, ...
    override_value: str | None
    concluded_value: str | None
    concluded_source: str                # "override" | "base_lease" | "amendment_N" | "none"

    # Citation + confidence for the concluded value (pulled from whichever
    # document won the precedence rules). None when concluded_source is
    # "override" or "none".
    concluded_confidence: float = 0.0
    confidence_level: str = "low"        # "high" (>=0.8) | "medium" (>=0.5) | "low" | "none"
    source_document_label: str | None = None   # "Base Lease" | "Amendment 2" | "Override" | None
    source_document_id: str | None = None
    page_number: int | None = None
    clause_number: str | None = None
    clause_text: str | None = None       # supporting clause snippet


class AbstractionSummary(BaseModel):
    """Rolled-up extraction quality view for the whole tenant."""
    total_fields: int
    fields_extracted: int                # concluded_value != "None"
    fields_none: int                     # concluded_value == "None"
    fields_overridden: int
    fields_flagged_review: int
    mean_confidence: float               # mean over extracted fields only
    high_confidence_count: int           # >= 0.8
    medium_confidence_count: int         # 0.5 - 0.8
    low_confidence_count: int            # < 0.5 and > 0


class TenantAbstractionOut(BaseModel):
    tenant_id: str
    tenant_name: str
    suite_number: str | None
    abstract_type: str
    property_type: str
    documents: list[DocumentOut]
    fields: list[AbstractedField]
    summary: AbstractionSummary


class FieldOverrideIn(BaseModel):
    value: str | None
    comment: str | None = None
    actor: str | None = None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    field_id: str
    action: str
    old_value: str | None
    new_value: str | None
    actor: str | None
    timestamp: datetime


# ---------------------------------------------------------------------------
# Red flags (LeaseLens)
# ---------------------------------------------------------------------------

class RedFlag(BaseModel):
    code: str                 # "RSF_MISMATCH", "DATE_INCONSISTENCY", etc.
    severity: str             # "info" | "warning" | "critical"
    message: str
    field_ids: list[str]
    context: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extraction Schemas — user-uploaded JSON specs
# ---------------------------------------------------------------------------

class SchemaUploadIn(BaseModel):
    """Request body when uploading via JSON. For multipart-file upload the
    raw JSON file content is read by the route handler instead."""
    model_config = ConfigDict(extra="allow")  # accept the full schema doc

    schema_id: str = Field(
        ...,
        description="Slug, lowercase, 3-128 chars, [a-z0-9_-]",
        examples=["acme_retail_v1"],
    )
    name: str = Field(
        ..., description="Human-readable display name",
        examples=["Acme Retail Lease Abstraction"],
    )
    version: str = Field(
        default="1.0.0",
        description="Semver. Server auto-bumps if the version already exists.",
        examples=["1.0.0"],
    )
    description: str | None = Field(
        default=None,
        examples=["Subset of BRD fields plus 2 ESG questions"],
    )
    fields: list[dict[str, Any]] = Field(
        ...,
        description=(
            "Array of field definitions. Each entry is either "
            "{'use_playbook': '<id>'} (reference a built-in BRD playbook) "
            "or a full inline definition with field_id/field_name/category/"
            "questions/etc."
        ),
    )
    default_property_type: str | None = None
    default_abstract_type: str | None = None


class SchemaOut(BaseModel):
    """Returned by GET /schemas/{id} and POST /schemas (after upload)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    schema_id: str
    name: str
    version: str
    description: str | None
    is_active: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    schema_json: dict[str, Any]


class SchemaSummaryOut(BaseModel):
    """Lightweight version returned by GET /schemas (list endpoint)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    schema_id: str
    name: str
    version: str
    description: str | None
    is_active: bool
    created_at: datetime
    field_count: int = 0    # populated from schema_json by the route


class SchemaListOut(BaseModel):
    schemas: list[SchemaSummaryOut]
    count: int
    active_schema_id: str | None


class SchemaValidationErrorItem(BaseModel):
    path: str
    code: str
    message: str


class SchemaValidationErrorOut(BaseModel):
    """Returned with HTTP 400 when an upload fails validation."""
    detail: str = "schema validation failed"
    errors: list[SchemaValidationErrorItem]
    warnings: list[SchemaValidationErrorItem] = Field(default_factory=list)
