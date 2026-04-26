"""Shared pytest fixtures.

Env vars are set in `pytest_configure` so `app.config.Settings` reads them
on the very first import of `app.*` — no module reloading needed. The
`client` fixture wires a FastAPI TestClient and truncates tables between
tests so each one starts from a clean slate.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


_TMP_ROOT = Path("/tmp/leasegenie-test")


def pytest_configure(config):
    """Mutate the env BEFORE any `app.*` module is imported."""
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    db_path = _TMP_ROOT / "test.db"
    upload_dir = _TMP_ROOT / "uploads"
    export_dir = _TMP_ROOT / "exports"
    upload_dir.mkdir(exist_ok=True)
    export_dir.mkdir(exist_ok=True)

    # Drop any leftover SQLite from a previous run so each session starts fresh.
    if db_path.exists():
        db_path.unlink()

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{db_path}")
    os.environ.setdefault("UPLOAD_DIR", str(upload_dir))
    os.environ.setdefault("EXPORT_DIR", str(export_dir))
    os.environ.setdefault("EXTRACTOR_BACKEND", "stub")
    os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
    # Use in-memory broker / result backend so eager-mode .delay() never
    # touches the network even if eager mode somehow flips off.
    os.environ.setdefault("CELERY_BROKER_URL", "memory://")
    os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
    os.environ.setdefault("DEBUG", "true")
    os.environ.pop("API_KEY", None)


@pytest.fixture(scope="session")
def _app_session():
    """Initialise the app once per session.

    By the time this fixture runs, `pytest_configure` has set the env so
    importing `app.main` is enough to wire everything correctly.

    We bypass Celery's eager-mode plumbing by monkey-patching `.delay()`
    on the two tasks to call them synchronously. This avoids any dependence
    on `task_always_eager` being correctly applied to the right Celery app
    instance during reloads, which has been brittle across Celery versions.
    """
    import app.db.session as session_module
    import app.main as main_module
    import app.workers.tasks as tasks_module

    def _sync_delay(_task):
        def _runner(*args, **kwargs):
            try:
                return _task(*args, **kwargs)
            except Exception:
                # Mirror eager_propagates=False so the API caller sees a
                # successful enqueue even if the task body raised.
                pass
        return _runner

    tasks_module.extract_tenant_task.delay = _sync_delay(tasks_module.extract_tenant_task)
    tasks_module.index_document_task.delay = _sync_delay(tasks_module.index_document_task)

    session_module.init_db()
    return {"session_module": session_module, "main_module": main_module}


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
