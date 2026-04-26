"""CRUD layer for extraction_schemas table.

All operations are session-scoped — the caller passes a `Session` so writes
participate in the surrounding transaction. The API layer commits.

Versioning rule: re-uploading the same `schema_id` creates a NEW row with
a bumped version (preserves history). The "active" version of a schema_id
is the latest by `created_at`. The `is_active` flag is separate — it
designates one schema as the default for new extractions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.orm import Session

from app.models.orm import AuditLog, ExtractionSchema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SchemaNotFound(LookupError):
    pass


class SchemaConflict(ValueError):
    """Raised when a schema operation would violate uniqueness/state rules."""
    pass


# ---------------------------------------------------------------------------
# Version bumping
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _bump_patch(version: str) -> str:
    """1.2.3 -> 1.2.4. Falls back to '1.0.0' on bad input."""
    m = _VERSION_RE.match(version or "")
    if not m:
        return "1.0.0"
    major, minor, patch = (int(g) for g in m.groups())
    return f"{major}.{minor}.{patch + 1}"


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_schemas(db: Session) -> list[ExtractionSchema]:
    """Return latest version of each schema_id, newest first."""
    # Subquery: latest created_at per schema_id
    rows = db.execute(
        select(ExtractionSchema).order_by(ExtractionSchema.created_at.desc())
    ).scalars().all()
    seen: set[str] = set()
    latest: list[ExtractionSchema] = []
    for r in rows:
        if r.schema_id in seen:
            continue
        seen.add(r.schema_id)
        latest.append(r)
    return latest


def get_schema(
    db: Session,
    schema_id: str,
    *,
    version: str | None = None,
) -> ExtractionSchema:
    """Get one schema. Without `version`, returns the most recent."""
    stmt = select(ExtractionSchema).where(ExtractionSchema.schema_id == schema_id)
    if version is not None:
        stmt = stmt.where(ExtractionSchema.version == version)
        row = db.execute(stmt).scalar_one_or_none()
        if row is None:
            raise SchemaNotFound(f"schema_id={schema_id} version={version} not found")
        return row
    stmt = stmt.order_by(desc(ExtractionSchema.created_at)).limit(1)
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise SchemaNotFound(f"schema_id={schema_id} not found")
    return row


def get_schema_versions(db: Session, schema_id: str) -> list[ExtractionSchema]:
    """Return all versions of a schema_id, newest first."""
    return db.execute(
        select(ExtractionSchema)
        .where(ExtractionSchema.schema_id == schema_id)
        .order_by(desc(ExtractionSchema.created_at))
    ).scalars().all()


def get_active_schema(db: Session) -> ExtractionSchema | None:
    """Return the schema currently flagged as default, or None."""
    return db.execute(
        select(ExtractionSchema)
        .where(ExtractionSchema.is_active.is_(True))
        .order_by(desc(ExtractionSchema.created_at))
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def upsert_schema(
    db: Session,
    *,
    schema_doc: dict[str, Any],
    created_by: str | None = None,
) -> ExtractionSchema:
    """Insert a new schema or a new version of an existing schema.

    If `schema_doc["version"]` matches an existing version, automatically
    bumps the patch number. Returns the newly-created row.

    Audit trail: writes one row to `audit_log` so the change is traceable.
    """
    schema_id = schema_doc["schema_id"]
    requested_version = schema_doc.get("version", "1.0.0")
    name = schema_doc["name"]
    description = schema_doc.get("description")

    # If this version already exists for this schema_id, auto-bump
    existing_versions = {
        s.version for s in get_schema_versions(db, schema_id)
    }
    final_version = requested_version
    while final_version in existing_versions:
        final_version = _bump_patch(final_version)
    if final_version != requested_version:
        logger.info(
            "schema_id=%s version %s already exists — bumping to %s",
            schema_id, requested_version, final_version,
        )

    # If a previous version of this schema_id was active, transfer the active
    # flag to the new version. Without this, PUT /schemas/{id} would silently
    # leave extractions using the old version.
    prior_versions = get_schema_versions(db, schema_id)
    inherit_active = any(p.is_active for p in prior_versions)

    row = ExtractionSchema(
        schema_id=schema_id,
        name=name,
        version=final_version,
        description=description,
        schema_json=schema_doc,
        is_active=False,            # set below if inheriting
        created_by=created_by,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()

    if inherit_active:
        # Clear the flag from older versions of THIS schema_id and any other
        # schemas, then set it on the new row.
        db.execute(
            update(ExtractionSchema).values(is_active=False)
        )
        db.flush()
        row.is_active = True
        db.flush()
        logger.info(
            "schema_id=%s: prior version was active, transferred flag to v%s",
            schema_id, final_version,
        )

    # Audit log
    db.add(AuditLog(
        tenant_id=None,  # global event (not tenant-scoped)
        field_id=f"schema:{schema_id}",
        action="extract",                                    # closest enum match
        old_value=None,
        new_value=f"version={final_version}",
        actor=created_by,
    ))
    return row


def set_active_schema(db: Session, schema_id: str) -> ExtractionSchema:
    """Mark `schema_id` as the active default. Clears any other active flag."""
    schema = get_schema(db, schema_id)  # raises SchemaNotFound

    db.execute(
        update(ExtractionSchema).values(is_active=False)
    )
    db.flush()
    schema.is_active = True
    db.flush()

    db.add(AuditLog(
        tenant_id=None,  # global event (not tenant-scoped)
        field_id=f"schema:{schema_id}",
        action="override",
        old_value=None,
        new_value=f"set_active=true (version={schema.version})",
        actor=None,
    ))
    return schema


def deactivate_all(db: Session) -> int:
    """Clear every is_active flag. Returns number of rows updated."""
    result = db.execute(
        update(ExtractionSchema)
        .where(ExtractionSchema.is_active.is_(True))
        .values(is_active=False)
    )
    return result.rowcount or 0


def delete_schema(db: Session, schema_id: str) -> int:
    """Delete ALL versions of `schema_id`. Returns count deleted."""
    rows = get_schema_versions(db, schema_id)
    if not rows:
        raise SchemaNotFound(f"schema_id={schema_id} not found")
    for r in rows:
        db.delete(r)
    db.flush()

    db.add(AuditLog(
        tenant_id=None,  # global event (not tenant-scoped)
        field_id=f"schema:{schema_id}",
        action="revert",
        old_value=f"versions={len(rows)}",
        new_value=None,
        actor=None,
    ))
    return len(rows)
