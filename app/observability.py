"""Prometheus metrics for the LeaseGenie API.

Exposes a `/metrics` endpoint (text/plain Prometheus exposition format) and
provides typed counters/histograms/gauges that the rest of the app can
import to record events. Metric overhead is negligible: counters increment
in O(1), histograms bucket in O(log n) over the bucket bounds.

The endpoint must be IP-restricted at the reverse proxy — see
deploy/windows/REVERSE_PROXY.md.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "leasegenie_http_requests_total",
    "HTTP requests handled by the API.",
    labelnames=("method", "path_template", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "leasegenie_http_request_duration_seconds",
    "HTTP request latency.",
    labelnames=("method", "path_template"),
    # Buckets sized for a mostly-async API (uploads can be slow).
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

OLLAMA_CALLS = Counter(
    "leasegenie_ollama_calls_total",
    "Ollama API calls grouped by outcome.",
    labelnames=("kind", "outcome"),  # kind=chat|chat_json|embed; outcome=ok|error|retry
)
OLLAMA_CALL_DURATION = Histogram(
    "leasegenie_ollama_call_duration_seconds",
    "Wall-clock time spent in a single Ollama call (success only).",
    labelnames=("kind",),
    buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180),
)

EXTRACTIONS = Counter(
    "leasegenie_extractions_total",
    "Tenant extraction jobs grouped by outcome.",
    labelnames=("outcome",),  # complete|failed|timeout
)
EXTRACTION_DURATION = Histogram(
    "leasegenie_extraction_duration_seconds",
    "Wall-clock time per extraction job (success only).",
    buckets=(10, 30, 60, 120, 300, 600, 1200, 1800),
)

CLEANUP_FILES_REMOVED = Counter(
    "leasegenie_cleanup_files_removed_total",
    "Files removed by the retention task.",
    labelnames=("kind",),  # uploads|exports
)
CLEANUP_BYTES_RECLAIMED = Counter(
    "leasegenie_cleanup_bytes_reclaimed_total",
    "Disk bytes reclaimed by the retention task.",
    labelnames=("kind",),
)

DB_POOL_IN_USE = Gauge(
    "leasegenie_db_pool_in_use",
    "Connections currently checked out of the SQLAlchemy pool.",
)


# ---------------------------------------------------------------------------
# HTTP middleware
# ---------------------------------------------------------------------------

class PrometheusMiddleware(BaseHTTPMiddleware):
    """Record per-request count + duration with the route template as label.

    Using the route template (`/api/v1/tenants/{tenant_id}/audit`) instead of
    the raw path keeps cardinality bounded — Prometheus would otherwise
    explode when a UUID-rich URL space is labelled per request.
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        # Resolve the matched route template (FastAPI sets request.scope["route"]).
        route = request.scope.get("route")
        path_template = getattr(route, "path", request.url.path)

        labels = {"method": request.method, "path_template": path_template}
        HTTP_REQUEST_DURATION.labels(**labels).observe(elapsed)
        HTTP_REQUESTS.labels(
            method=request.method,
            path_template=path_template,
            status=str(response.status_code),
        ).inc()

        # Refresh the connection-pool gauge opportunistically.
        try:
            from app.db.session import engine

            pool = engine.pool
            if hasattr(pool, "checkedout"):
                DB_POOL_IN_USE.set(pool.checkedout())
        except Exception:
            pass
        return response


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

router = APIRouter(tags=["meta"])


@router.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Prometheus exposition endpoint.

    Not authenticated — your reverse proxy MUST IP-restrict /metrics to
    internal scrape targets. See deploy/windows/REVERSE_PROXY.md.
    """
    payload = generate_latest()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
