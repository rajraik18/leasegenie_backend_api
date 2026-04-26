"""API end-to-end smoke tests.

Exercises the public endpoints against a TestClient + SQLite. Covers:
  - /health, /readiness, /docs gating
  - CORS exposure
  - X-Request-ID round-trip
  - API-key auth on/off
  - OrderCreate validators (post-PR #1)
  - Audit pagination
  - Fields pagination
  - Schema upload (small + oversized)
"""
from __future__ import annotations

import importlib
import json

import pytest


# ---------------------------------------------------------------------------
# Liveness / readiness / docs
# ---------------------------------------------------------------------------

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["fields_loaded"] >= 1
    assert "ollama_base_url" not in body, "H2: /health must not leak ollama_base_url"


def test_readiness_returns_200_or_503(client):
    """Readiness probes Postgres + Ollama. With SQLite + extractor=stub the
    DB check passes and Ollama is skipped, so we expect 200."""
    r = client.get("/readiness")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.json()["ready"] is True


def test_docs_exposed_in_debug(client):
    """When DEBUG=true the OpenAPI UI is served. Production deploys set
    DEBUG=false and these return 404."""
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------

def test_request_id_round_trip(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers.get("X-Request-ID") == "abc-123"


def test_request_id_generated_when_missing(client):
    r = client.get("/health")
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8


# ---------------------------------------------------------------------------
# Order create + validators (PR #1)
# ---------------------------------------------------------------------------

def test_orders_validators(client):
    # empty properties -> 422
    r = client.post(
        "/api/v1/orders",
        json={"project_name": "Demo", "properties": []},
    )
    assert r.status_code == 422

    # empty project_name -> 422
    r = client.post(
        "/api/v1/orders",
        json={
            "project_name": "",
            "properties": [{
                "name": "P1",
                "property_type": "Office",
                "tenants": [],
            }],
        },
    )
    assert r.status_code == 422


def test_orders_create_and_get(client):
    payload = {
        "project_name": "Demo",
        "properties": [{
            "name": "Tower One",
            "property_type": "Office",
            "tenants": [{"name": "Acme Corp"}],
        }],
    }
    r = client.post("/api/v1/orders", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Demo"
    assert len(body["properties"]) == 1
    assert body["properties"][0]["tenants"][0]["name"] == "Acme Corp"

    project_id = body["id"]
    r = client.get(f"/api/v1/orders/{project_id}")
    assert r.status_code == 200
    assert r.json()["id"] == project_id


# ---------------------------------------------------------------------------
# Audit pagination (PR #1)
# ---------------------------------------------------------------------------

def test_audit_pagination_caps(client):
    # limit must be 1..500
    r = client.get("/api/v1/tenants/some-id/audit?limit=2000")
    assert r.status_code == 422

    r = client.get("/api/v1/tenants/some-id/audit?limit=10")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# Fields pagination (PR #1)
# ---------------------------------------------------------------------------

def test_fields_pagination(client):
    r = client.get("/api/v1/fields?limit=5")
    assert r.status_code == 200
    assert len(r.json()) <= 5


# ---------------------------------------------------------------------------
# Schema upload (PR #1)
# ---------------------------------------------------------------------------

def test_schema_upload_too_large_returns_413(client):
    big = json.dumps({
        "schema_id": "huge",
        "version": "1.0.0",
        "fields": ["x" * 100] * 70_000,
    }).encode()
    r = client.post(
        "/api/v1/schemas",
        files={"file": ("big.json", big, "application/json")},
    )
    assert r.status_code == 413


def test_schema_upload_invalid_returns_400(client):
    r = client.post(
        "/api/v1/schemas",
        files={"file": ("tiny.json", b'{"not":"valid"}', "application/json")},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

def test_cors_blocked_for_unknown_origin(client):
    """The CORS allowlist defaults to localhost:3000. A different origin
    must NOT receive the Access-Control-Allow-Origin header."""
    r = client.get("/health", headers={"Origin": "http://evil.example"})
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_allowed_for_configured_origin(client):
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------------------
# Auth on/off
# ---------------------------------------------------------------------------

def test_auth_off_when_api_key_unset(client):
    """The conftest fixture leaves API_KEY unset, so all routes are open.
    This locks in current behaviour."""
    r = client.get("/api/v1/orders/00000000")
    # 404 (not 401) confirms the request reached the handler
    assert r.status_code == 404


def test_auth_required_when_api_key_set(client, monkeypatch):
    """When API_KEY is set, missing/invalid keys are rejected; matching
    keys pass through to the handler."""
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "secret-key-xyz")

    # No auth -> 401
    r = client.get("/api/v1/orders/00000000")
    assert r.status_code == 401, r.text

    # Wrong key -> 401
    r = client.get("/api/v1/orders/00000000", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401

    # Correct key via X-API-Key -> reaches handler (404 because no row)
    r = client.get("/api/v1/orders/00000000", headers={"X-API-Key": "secret-key-xyz"})
    assert r.status_code == 404

    # Correct key via Authorization: Bearer -> reaches handler
    r = client.get(
        "/api/v1/orders/00000000",
        headers={"Authorization": "Bearer secret-key-xyz"},
    )
    assert r.status_code == 404

    # /health stays open even with API key configured
    assert client.get("/health").status_code == 200
