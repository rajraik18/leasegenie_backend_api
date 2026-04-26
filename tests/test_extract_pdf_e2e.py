"""End-to-end PDF extraction test.

Runs the full POST /extract/pdf -> Celery task -> /jobs/{id} ->
GET /jobs/{id}/result flow against:
  - SQLite (no pgvector; vector store is the NoOp stub)
  - EXTRACTOR_BACKEND=stub (no Ollama in the loop)
  - CELERY_TASK_ALWAYS_EAGER=true (no broker)

What this catches:
  - regressions in the upload + Document.create + Tenant auto-create flow
  - regressions in the OCR / clause-extraction path (pdfplumber must
    parse the fixture PDF, BM25 must build, even if no LLM runs)
  - regressions in the Coordinator + specialist dispatch order
  - regressions in Job state transitions (queued -> running -> complete)
  - regressions in build_abstraction + the JSON result schema
"""
from __future__ import annotations

import time

import pytest

from tests._pdf_fixture import make_minimal_pdf


def _wait_for_complete(client, job_id: str, *, timeout_s: float = 30.0) -> dict:
    """Poll /jobs/{id} until status is `complete` or `failed`. Returns the row."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/jobs/{job_id}")
        # /jobs lives on the extraction router which is included with auth dep,
        # but in the test env API_KEY is unset, so it's open.
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("complete", "failed"):
            return last
        time.sleep(0.1)
    pytest.fail(f"job {job_id} stuck in status={last.get('status')!r}")


def test_extract_pdf_full_flow(client):
    pdf_bytes = make_minimal_pdf(
        "Acme Corp lease -- Suite 100, 5,000 RSF, 5 year term, $20/RSF base rent"
    )

    r = client.post(
        "/api/v1/extract/pdf",
        params={
            "property_type": "Office",
            "abstract_type": "Full Abstract",
            "tenant_name": "Acme Corp",
        },
        files=[("files", ("base_lease.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 202, r.text
    job = r.json()
    job_id = job["id"]
    tenant_id = job["tenant_id"]
    assert job["status"] in ("queued", "running", "complete")

    final = _wait_for_complete(client, job_id, timeout_s=60.0)
    assert final["status"] == "complete", final

    # Pull the abstraction. Should return a fully-shaped TenantAbstractionOut.
    r = client.get(f"/api/v1/extract/jobs/{job_id}/result?format=json")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["tenant_id"] == tenant_id
    assert body["tenant_name"] == "Acme Corp"
    assert body["property_type"] == "Office"
    assert isinstance(body["fields"], list)
    assert len(body["fields"]) > 0, "stub run should still emit one row per in-scope field"
    assert isinstance(body["documents"], list)
    assert len(body["documents"]) == 1
    # Summary block present and self-consistent.
    summary = body["summary"]
    assert summary["total_fields"] == len(body["fields"])

    # Red flags live on a separate endpoint.
    rf = client.get(f"/api/v1/tenants/{tenant_id}/red-flags")
    assert rf.status_code == 200
    assert isinstance(rf.json(), list)


def test_extract_pdf_xlsx_download(client):
    """The same job's result downloads as a well-formed .xlsx."""
    pdf_bytes = make_minimal_pdf("Acme")
    r = client.post(
        "/api/v1/extract/pdf",
        params={"property_type": "Office", "tenant_name": "Acme"},
        files=[("files", ("base.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 202
    job_id = r.json()["id"]

    _wait_for_complete(client, job_id, timeout_s=60.0)

    r = client.get(f"/api/v1/extract/jobs/{job_id}/result?format=xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # XLSX files start with the ZIP magic 'PK\x03\x04'.
    assert r.content[:4] == b"PK\x03\x04"
    # Disposition uses the new RFC 5987 filename* form.
    cd = r.headers.get("content-disposition", "")
    assert "lease_abstraction" in cd
    assert "filename*=UTF-8''" in cd


def test_extract_pdf_rejects_invalid_property_type(client):
    pdf_bytes = make_minimal_pdf("x")
    r = client.post(
        "/api/v1/extract/pdf",
        params={"property_type": "Bogus"},
        files=[("files", ("base.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 400


def test_extract_pdf_rejects_too_many_files(client):
    """The handler caps at MAX_PDFS_PER_REQUEST (default 8 = 1 base + 7 amendments)."""
    pdf_bytes = make_minimal_pdf("x")
    files = [
        ("files", (f"doc_{i}.pdf", pdf_bytes, "application/pdf"))
        for i in range(9)
    ]
    r = client.post(
        "/api/v1/extract/pdf",
        params={"property_type": "Office"},
        files=files,
    )
    # The handler raises HTTP 400 for too many files.
    assert r.status_code == 400, r.text
