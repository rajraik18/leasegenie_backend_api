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
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.playbooks import get_playbooks
from app.agents.playbooks.compiler import compile_all
from app.api.v1 import abstraction, documents, extract_pdf, extraction, fields, orders, playbooks, schemas
from app.config import settings
from app.core.reference_data import get_reference_data
from app.db.session import init_db

logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


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
        version="2.0.0",
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
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    prefix = settings.api_v1_prefix
    app.include_router(orders.router, prefix=prefix)
    app.include_router(documents.router, prefix=prefix)
    app.include_router(extraction.router, prefix=prefix)
    app.include_router(abstraction.router, prefix=prefix)
    app.include_router(fields.router, prefix=prefix)
    app.include_router(playbooks.router, prefix=prefix)
    app.include_router(extract_pdf.router, prefix=prefix)
    app.include_router(schemas.router, prefix=prefix)

    @app.on_event("startup")
    def _startup() -> None:
        init_db()
        ref = get_reference_data()
        _ensure_playbooks_compiled()
        playbooks = get_playbooks()
        logger.info(
            "LeaseGenie ready | fields=%d | playbooks=%d | extractor=%s | ollama=%s model=%s",
            len(ref.fields),
            len(playbooks),
            settings.extractor_backend,
            settings.ollama_base_url,
            settings.ollama_model,
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
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
            "ollama_base_url": settings.ollama_base_url,
        }

    return app


app = create_app()
