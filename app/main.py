"""LeaseGenie FastAPI application.

Entry point: `uvicorn app.main:app --reload`

Startup:
    1. Create database tables.
    2. Load the BRD field list (72 fields) into memory.
    3. If compiled playbooks directory is empty, run the playbook compiler
       (parses the 5 .docx guides + Questions.xlsx into JSON).
    4. Load compiled playbooks into memory.
    5. Log Ollama model + specialist agent wiring.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app.agents.playbooks import get_playbooks
from app.agents.playbooks.compiler import compile_all
from app.api.deps import require_api_key
from app.api.v1 import abstraction, documents, extract_pdf, extraction, fields, orders, playbooks, schemas
from app.config import settings
from app.core.reference_data import get_reference_data
from app.db.session import engine, init_db
from app.observability import PrometheusMiddleware, router as metrics_router


# ---------------------------------------------------------------------------
# Logging — JSON formatter that includes request_id from a contextvar.
# ---------------------------------------------------------------------------

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")


class _JsonFormatter(logging.Formatter):
    """Minimal structured-log formatter. No external dep."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": _REQUEST_ID.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging() -> None:
    root = logging.getLogger()
    # Strip any pre-existing handlers (e.g. uvicorn's basic config).
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if settings.debug:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] [rid=%(request_id)s] %(message)s"))
        # In debug mode we also stamp request_id via a Filter so plain text
        # formatting can include it.
        handler.addFilter(_RequestIdFilter())
    else:
        handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if settings.debug else logging.INFO)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID.get()
        return True


_configure_logging()
logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp each request with an X-Request-ID and surface it in logs."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = _REQUEST_ID.set(rid)
        try:
            response = await call_next(request)
        finally:
            _REQUEST_ID.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


def _ensure_playbooks_compiled() -> None:
    compiled_dir = Path(settings.brd_path).parent / "playbooks_compiled"
    source_dir = Path(settings.brd_path).parent / "playbooks_source"
    if compiled_dir.exists() and any(compiled_dir.glob("*.json")):
        return
    if not source_dir.exists():
        logger.warning("No playbook sources at %s — specialists will be limited", source_dir)
        return
    logger.info("Compiled playbooks missing — compiling from %s", source_dir)
    result = compile_all(source_dir, compiled_dir)
    logger.info("Compiled %d playbooks into %s", result["count"], compiled_dir)


def _redact_url(raw: str) -> str:
    """Return the URL with any user/password fragment masked."""
    import re

    return re.sub(r"(://[^:/@]+):[^@/]*@", r"\1:***@", raw)


def create_app() -> FastAPI:
    tags_metadata = [
        {
            "name": "extract",
            "description": (
                "**End-to-end PDF extraction.** Upload one or more PDFs (a "
                "base lease and up to 7 amendments), trigger the multi-agent "
                "extraction pipeline, and download the result as JSON or "
                "Excel. The fastest path to a lease abstraction — does not "
                "require pre-creating a project/property/tenant."
            ),
        },
        {
            "name": "schemas",
            "description": (
                "**User-uploaded extraction schemas.** Schemas are JSON "
                "documents that select or define which fields the extraction "
                "pipeline runs. Use them to extract a subset of the 79 BRD "
                "fields, or to add custom fields not in the BRD. A schema can "
                "be set as the active default, or referenced per-extraction "
                "via `?schema_id=...` on `POST /extract/pdf`."
            ),
        },
        {
            "name": "extraction",
            "description": (
                "Async extraction job tracking. Use `GET /jobs/{job_id}` to "
                "poll progress (0-100%). Created indirectly via `POST "
                "/extract/pdf` or directly via `POST /extraction`."
            ),
        },
        {
            "name": "orders",
            "description": (
                "Project / property / tenant CRUD. The hierarchy is "
                "Project → Property → Tenant → Document. Use these endpoints "
                "for portfolio-mode workflows where the same tenant is "
                "abstracted multiple times."
            ),
        },
        {
            "name": "documents",
            "description": "Per-document operations (upload, list, OCR status).",
        },
        {
            "name": "fields",
            "description": (
                "Per-field reads, manual overrides, and audit trail. The "
                "`field_overrides` table takes precedence over extracted "
                "values — the API exposes both."
            ),
        },
        {
            "name": "abstraction",
            "description": (
                "Final reconciled abstraction (post-amendment merge + red "
                "flags). This is the canonical 'tenant abstract' view used "
                "by Excel export."
            ),
        },
        {
            "name": "playbooks",
            "description": (
                "Read-only access to the compiled BRD playbooks. Useful for "
                "introspection (which fields exist?) and for building UIs "
                "that display extraction confidence by category."
            ),
        },
        {
            "name": "meta",
            "description": "Health checks and runtime introspection.",
        },
    ]

    app = FastAPI(
        title=settings.project_name,
        version="3.0.0",
        description=(
            "**Multi-agent lease data extraction API.**\n\n"
            "Five specialist agents (Basic Info, Financial, Reimbursements, "
            "Critical, Other) execute strict IF YES/IF NO decision-tree "
            "playbooks compiled from the BRD's master abstraction guides + "
            "Questions.xlsx. A local Ollama LLM answers per-question; "
            "branching is done deterministically in code. A reconciliation "
            "agent then cross-checks across base lease + amendments and "
            "emits LeaseLens red flags.\n\n"
            "**Quick start:**\n"
            "1. (Optional) `POST /api/v1/schemas` — upload a JSON schema "
            "to extract a subset or custom fields\n"
            "2. `POST /api/v1/extract/pdf` — upload PDFs, get a `job_id`\n"
            "3. `GET /api/v1/jobs/{job_id}` — poll until status is `complete`\n"
            "4. `GET /api/v1/extract/jobs/{job_id}/result?format=json` "
            "(or `?format=xlsx`) — download the abstraction\n"
        ),
        openapi_tags=tags_metadata,
        contact={
            "name": "LeaseGenie API",
            "url": "https://github.com/your-org/leasegenie-api",
        },
        license_info={
            "name": "Proprietary",
        },
        # OpenAPI UI is only exposed in debug mode. Production deployments
        # set DEBUG=false in .env so /docs and /redoc return 404.
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        # No endpoint uses cookie-based session credentials, so credentialed
        # CORS isn't needed. Keeping it false also avoids the "wildcard +
        # credentials" CSRF anti-pattern.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    prefix = settings.api_v1_prefix
    auth_dep = [Depends(require_api_key)]

    app.include_router(orders.router,      prefix=prefix, dependencies=auth_dep)
    app.include_router(documents.router,   prefix=prefix, dependencies=auth_dep)
    app.include_router(extraction.router,  prefix=prefix, dependencies=auth_dep)
    app.include_router(abstraction.router, prefix=prefix, dependencies=auth_dep)
    app.include_router(fields.router,      prefix=prefix, dependencies=auth_dep)
    app.include_router(playbooks.router,   prefix=prefix, dependencies=auth_dep)
    app.include_router(extract_pdf.router, prefix=prefix, dependencies=auth_dep)
    app.include_router(schemas.router,     prefix=prefix, dependencies=auth_dep)

    # /metrics is intentionally unauthenticated — IP-restrict in the reverse proxy.
    app.include_router(metrics_router)

    @app.on_event("startup")
    def _startup() -> None:
        init_db()
        ref = get_reference_data()
        _ensure_playbooks_compiled()
        playbooks = get_playbooks()
        logger.info(
            "LeaseGenie ready | fields=%d | playbooks=%d | extractor=%s | model=%s | auth=%s",
            len(ref.fields),
            len(playbooks),
            settings.extractor_backend,
            settings.ollama_model,
            "ON" if settings.api_key else "OFF",
        )
        if not settings.api_key:
            logger.warning(
                "API_KEY is not set -- all endpoints are unauthenticated. "
                "This is OK for local development only.",
            )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        """Liveness probe. Always returns 200 if the process is up."""
        ref = get_reference_data()
        playbooks = get_playbooks()
        by_cat: dict[str, int] = {}
        for pb in playbooks.values():
            by_cat[pb.category] = by_cat.get(pb.category, 0) + 1
        return {
            "status": "ok",
            "fields_loaded": len(ref.fields),
            "playbooks_loaded": len(playbooks),
            "playbooks_by_category": by_cat,
            "extractor_backend": settings.extractor_backend,
            "ollama_model": settings.ollama_model,
        }

    @app.get("/readiness", tags=["meta"])
    def readiness() -> dict:
        """Readiness probe. Verifies Postgres + Ollama are reachable."""
        from fastapi import HTTPException
        problems: list[str] = []

        # Postgres
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            problems.append(f"db: {exc.__class__.__name__}")

        # Ollama (only when ollama backend is selected)
        if settings.extractor_backend == "ollama":
            try:
                import urllib.request

                req = urllib.request.Request(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status != 200:
                        problems.append(f"ollama: status {resp.status}")
            except Exception as exc:
                problems.append(f"ollama: {exc.__class__.__name__}")

        if problems:
            raise HTTPException(status_code=503, detail={"ready": False, "problems": problems})
        return {"ready": True}

    return app


app = create_app()
