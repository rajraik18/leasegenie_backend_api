"""Tests for the MEDIUM batch fixes.

Covers:
  - PATCH /tenants/{id}/fields/{field_id} returns the lightweight ack
    instead of the full TenantAbstractionOut grid.
  - cleanup_stale_jobs marks queued/running ExtractionJob rows older than
    JOB_STALE_TTL_HOURS as failed.
  - PDF page-count cap rejects oversized PDFs.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from tests._pdf_fixture import make_minimal_pdf


def _make_tenant(client) -> tuple[str, str]:
    """Helper -- create a project/property/tenant via the orders API."""
    payload = {
        "project_name": "Demo",
        "properties": [{
            "name": "Tower One",
            "property_type": "Office",
            "tenants": [{"name": "Acme"}],
        }],
    }
    r = client.post("/api/v1/orders", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    project_id = body["id"]
    tenant_id = body["properties"][0]["tenants"][0]["id"]
    return project_id, tenant_id


# ---------------------------------------------------------------------------
# M-ABSTRACTION-PERF: PATCH returns FieldOverrideAck
# ---------------------------------------------------------------------------

def test_set_override_returns_lightweight_ack(client):
    _, tenant_id = _make_tenant(client)

    r = client.patch(
        f"/api/v1/tenants/{tenant_id}/fields/annual_base_rent",
        json={"value": "120000", "comment": "negotiated", "actor": "alice"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Was previously TenantAbstractionOut. Now must be the small ack:
    assert set(body.keys()) == {"tenant_id", "field_id", "action", "value"}
    assert body["tenant_id"] == tenant_id
    assert body["field_id"] == "annual_base_rent"
    assert body["action"] == "override"
    assert body["value"] == "120000"

    # Audit row landed
    r = client.get(f"/api/v1/tenants/{tenant_id}/audit?limit=10")
    assert r.status_code == 200
    assert any(
        row["action"] == "override" and row["new_value"] == "120000"
        for row in r.json()
    )


def test_clear_override_returns_revert_ack(client):
    _, tenant_id = _make_tenant(client)

    # Set then clear
    client.patch(
        f"/api/v1/tenants/{tenant_id}/fields/annual_base_rent",
        json={"value": "120000"},
    )
    r = client.patch(
        f"/api/v1/tenants/{tenant_id}/fields/annual_base_rent",
        json={"value": None},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "revert"
    assert body["value"] is None


# ---------------------------------------------------------------------------
# M-JOB-TTL: cleanup_stale_jobs
# ---------------------------------------------------------------------------

def test_cleanup_stale_jobs_marks_old_queued_jobs_failed(client):
    """Insert a queued ExtractionJob whose created_at is well past the TTL,
    then run the sweeper and assert it transitions to failed."""
    from app.config import settings
    from app.db.session import SessionLocal
    from app.models.orm import ExtractionJob
    from app.workers.tasks import cleanup_stale_jobs_task

    _, tenant_id = _make_tenant(client)

    # Manually insert a stale queued job. created_at is server_default in
    # the schema, so we explicitly bypass it.
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.job_stale_ttl_hours + 1)
        job = ExtractionJob(tenant_id=tenant_id, status="queued", created_at=cutoff)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    out = cleanup_stale_jobs_task()
    assert out["failed"] >= 1

    db = SessionLocal()
    try:
        refreshed = db.get(ExtractionJob, job_id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert "timed out" in (refreshed.error or "").lower()
    finally:
        db.close()


def test_cleanup_stale_jobs_skips_recent_rows(client):
    from app.db.session import SessionLocal
    from app.models.orm import ExtractionJob
    from app.workers.tasks import cleanup_stale_jobs_task

    _, tenant_id = _make_tenant(client)

    db = SessionLocal()
    try:
        # created_at default is "now"
        job = ExtractionJob(tenant_id=tenant_id, status="queued")
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    cleanup_stale_jobs_task()

    db = SessionLocal()
    try:
        assert db.get(ExtractionJob, job_id).status == "queued"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# M-PDF-SIZE: page-count cap rejects oversized PDFs
# ---------------------------------------------------------------------------

def test_ocr_rejects_pdf_over_page_cap(monkeypatch):
    """`extract_document_text` raises ValueError when len(pdf.pages) > MAX_PDF_PAGES."""
    import pathlib
    import tempfile

    from app.config import settings
    from app.services.ocr import extract_document_text

    # 5-page PDF -- cheaper than rendering 500.
    pdf_bytes = _multipage_pdf(5)
    monkeypatch.setattr(settings, "max_pdf_pages", 3)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = pathlib.Path(f.name)

    try:
        with pytest.raises(ValueError, match="MAX_PDF_PAGES"):
            extract_document_text(path)
    finally:
        path.unlink(missing_ok=True)


def test_ocr_disabled_cap_lets_large_pdf_through(monkeypatch):
    import pathlib
    import tempfile

    from app.config import settings
    from app.services.ocr import extract_document_text

    pdf_bytes = _multipage_pdf(4)
    monkeypatch.setattr(settings, "max_pdf_pages", 0)  # 0 disables

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = pathlib.Path(f.name)

    try:
        # Should not raise.
        doc = extract_document_text(path)
        assert len(doc.pages) == 4
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _multipage_pdf(n_pages: int) -> bytes:
    """Build a multi-page PDF by concatenating page resources."""
    # Reuse the make_minimal_pdf helper -- repeats the same content stream
    # across N pages by extending the /Kids array.
    # Quick path: just call make_minimal_pdf N times into a single PDF
    # would not actually create N pages. Build inline instead.
    text_objs: list[bytes] = []
    for i in range(n_pages):
        cs = f"BT /F1 12 Tf 72 720 Td (page {i + 1}) Tj ET".encode("latin-1")
        text_objs.append(b"<< /Length " + str(len(cs)).encode("latin-1") + b" >>\nstream\n" + cs + b"\nendstream")

    objects: list[bytes] = []
    # 1: Catalog
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    # 2: Pages root -- placeholder, fills in after we know page object ids
    page_ids_start = 3
    page_count = n_pages
    page_id_list = " ".join(f"{page_ids_start + i} 0 R" for i in range(page_count))
    objects.append(
        ("<< /Type /Pages /Kids [" + page_id_list + f"] /Count {page_count} >>").encode("latin-1")
    )
    # 3..3+n-1: each Page; content streams come right after, then the font
    content_start = page_ids_start + page_count
    for i in range(page_count):
        content_id = content_start + i
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {content_start + page_count} 0 R >> >> >>"
        ).encode("latin-1")
        objects.append(page_obj)
    # content streams
    for cs in text_objs:
        objects.append(cs)
    # font
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n"
    out += f"0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += b"trailer\n"
    out += f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("latin-1")
    out += b"startxref\n"
    out += f"{xref_offset}\n".encode("latin-1")
    out += b"%%EOF\n"
    return bytes(out)
