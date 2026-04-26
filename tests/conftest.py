"""Shared pytest fixtures.

The `client` fixture wires a FastAPI TestClient against an isolated SQLite
DB and a temporary upload directory. Auth is disabled (API_KEY unset) so
end-to-end tests can talk to all routes.

Module reloading is done once at session scope. Per-test isolation is
achieved by truncating the tables between tests instead of recreating the
engine — that way every imported module continues to see the same
SessionLocal / engine instance.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _app_session(tmp_path_factory):
    """Configure the app once per test session."""
    base = tmp_path_factory.mktemp("leasegenie")
    db_path = base / "test.db"
    upload_dir = base / "uploads"
    export_dir = base / "exports"
    upload_dir.mkdir()
    export_dir.mkdir()

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["UPLOAD_DIR"] = str(upload_dir)
    os.environ["EXPORT_DIR"] = str(export_dir)
    os.environ["EXTRACTOR_BACKEND"] = "stub"
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"
    os.environ["DEBUG"] = "true"
    os.environ.pop("API_KEY", None)

    # Pick up the env above. Reload happens BEFORE TestClient imports the app.
    import app.config as _config
    importlib.reload(_config)
    import app.db.session as _session
    importlib.reload(_session)
    import app.api.deps as _deps
    importlib.reload(_deps)

    # Routers / workers / services capture references to deps + SessionLocal
    # at import time. Reload them so they bind to the new instances.
    for name in (
        "app.api.v1.orders",
        "app.api.v1.documents",
        "app.api.v1.extraction",
        "app.api.v1.abstraction",
        "app.api.v1.fields",
        "app.api.v1.playbooks",
        "app.api.v1.extract_pdf",
        "app.api.v1.schemas",
        "app.workers.tasks",
        "app.services.pipeline",
        "app.services.doc_indexer",
        "app.services.vector_store",
    ):
        try:
            importlib.reload(importlib.import_module(name))
        except Exception:
            pass

    import app.main as _main
    importlib.reload(_main)

    _session.init_db()

    return {
        "session_module": _session,
        "main_module": _main,
        "config_module": _config,
    }


@pytest.fixture()
def client(_app_session):
    """Per-test TestClient. Tables are wiped between tests."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    main_module = _app_session["main_module"]
    session_module = _app_session["session_module"]

    with TestClient(main_module.app) as c:
        yield c

    # Truncate every table so the next test starts clean. SQLite doesn't
    # support TRUNCATE, so DELETE FROM in dependency-safe order is fine.
    engine = session_module.engine
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        for tbl in (
            "audit_log",
            "field_overrides",
            "field_values",
            "extraction_jobs",
            "extraction_schemas",
            "clause_embeddings",
            "documents",
            "tenants",
            "properties",
            "projects",
            "schema_version",
        ):
            try:
                conn.execute(text(f"DELETE FROM {tbl}"))
            except Exception:
                pass
        conn.execute(text("PRAGMA foreign_keys = ON"))
