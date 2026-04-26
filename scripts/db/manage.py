#!/usr/bin/env python3
"""LeaseGenie database management CLI.

Subcommands:
    init        Create extensions + all tables (idempotent)
    drop        Drop all tables (DESTRUCTIVE — requires --yes)
    reset       drop + init (DESTRUCTIVE — requires --yes)
    check       Verify schema matches ORM (no changes)
    status      Show row counts per table + pgvector status
    seed        Insert minimal demo data (a project, property, tenant)
    migrate     Apply forward-only schema additions from ORM
    upgrade     Alias of init (Alembic-compat naming)
    pgvector    Verify pgvector extension is installed and usable
    sql         Run scripts/db/schema.sql via psycopg2 (raw SQL path)

Backends:
    - PostgreSQL + pgvector (PRIMARY — production)
    - SQLite (dev fallback — pgvector replaced with stub queries)

The CLI uses `app.config.settings` so it talks to the same database the API
does. DATABASE_URL examples:
    postgresql+psycopg2://leasegenie:pwd@postgres:5432/leasegenie     (Compose)
    postgresql+psycopg2://user:pwd@host:5432/leasegenie?sslmode=require  (managed)
    sqlite:///./leasegenie.db                                          (local dev)

Examples:
    python -m scripts.db.manage init
    python -m scripts.db.manage status
    python -m scripts.db.manage pgvector       # verify ext installed
    python -m scripts.db.manage reset --yes    # DEV ONLY
    python -m scripts.db.manage seed --project Demo
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is importable when invoked directly
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("db.manage")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# --------------------------------------------------------------------------- #
# Lazy imports — keep --help working even before `pip install`
# --------------------------------------------------------------------------- #
def _import_app():
    """Import sqlalchemy + app.* lazily with a friendly error on failure."""
    try:
        from sqlalchemy import inspect, text                 # noqa: F401
        from app.config import settings                      # noqa: F401
        from app.db.session import Base, engine, init_db     # noqa: F401
        from app.models import orm                           # noqa: F401
    except ModuleNotFoundError as exc:
        msg = (
            f"Required dependency not installed: {exc.name}\n"
            "Install project dependencies first:  pip install -r requirements.txt\n"
            "Or run from the API container:        scripts/db.sh init"
        )
        sys.stderr.write(f"\033[31m✗ {msg}\033[0m\n")
        sys.exit(1)
    return {
        "settings":  __import__("app.config", fromlist=["settings"]).settings,
        "Base":      __import__("app.db.session", fromlist=["Base"]).Base,
        "engine":    __import__("app.db.session", fromlist=["engine"]).engine,
        "init_db":   __import__("app.db.session", fromlist=["init_db"]).init_db,
        "orm":       __import__("app.models.orm", fromlist=["*"]),
        "inspect":   __import__("sqlalchemy", fromlist=["inspect"]).inspect,
        "text":      __import__("sqlalchemy", fromlist=["text"]).text,
    }


# --------------------------------------------------------------------------- #
# Tables (in creation order — children at end)
# --------------------------------------------------------------------------- #
TABLES_IN_ORDER = [
    "projects",
    "properties",
    "tenants",
    "documents",
    "field_values",
    "field_overrides",
    "audit_log",
    "extraction_jobs",
    "extraction_schemas",
    "clause_embeddings",
]
DROP_ORDER = list(reversed(TABLES_IN_ORDER))


# --------------------------------------------------------------------------- #
# Backend detection
# --------------------------------------------------------------------------- #
def _is_postgres(engine) -> bool:
    return engine.dialect.name in ("postgresql", "postgres")


def _is_sqlite(engine) -> bool:
    return engine.dialect.name == "sqlite"


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #

def cmd_init(_args, deps):
    """Create extensions (Postgres) + all tables. Idempotent."""
    settings = deps["settings"]
    inspect = deps["inspect"]
    engine = deps["engine"]
    init_db = deps["init_db"]
    text = deps["text"]

    logger.info("Database URL: %s", _redact(settings.database_url))
    logger.info("Backend:      %s", engine.dialect.name)

    # Step 1: Postgres extensions
    if _is_postgres(engine):
        _ensure_pg_extensions(engine, text)
    elif _is_sqlite(engine):
        logger.warning(
            "SQLite backend — pgvector unavailable. Vector search will fall "
            "back to in-memory cosine similarity (slower). Use Postgres for "
            "production."
        )

    # Step 2: Create tables
    insp = inspect(engine)
    existing = set(insp.get_table_names())

    will_create = [t for t in TABLES_IN_ORDER if t not in existing]
    will_skip = [t for t in TABLES_IN_ORDER if t in existing]

    if will_skip:
        logger.info("  Already present (skipping):")
        for t in will_skip:
            logger.info("    • %s", t)

    if will_create:
        logger.info("  Creating:")
        for t in will_create:
            logger.info("    + %s", t)
        init_db()    # SQLAlchemy create_all — idempotent

    # Step 3: HNSW index on Postgres (ORM doesn't model pgvector indexes)
    if _is_postgres(engine):
        _ensure_pgvector_index(engine, text)

    # Step 4: Verify
    insp = inspect(engine)
    after = set(insp.get_table_names())
    missing = [t for t in TABLES_IN_ORDER if t not in after]
    if missing:
        logger.error("Tables still missing after init: %s", missing)
        return 2

    logger.info("✓ Database initialized — %d table(s) ready", len(TABLES_IN_ORDER))
    _record_schema_version(deps, "2.0.0", note="init")
    return 0


def cmd_drop(args, deps):
    """Drop all tables. DESTRUCTIVE."""
    if not args.yes:
        logger.error("Refusing to drop tables without --yes flag")
        logger.error("This will DELETE ALL DATA in the database.")
        return 1
    engine = deps["engine"]
    Base = deps["Base"]
    text = deps["text"]
    inspect = deps["inspect"]

    insp = inspect(engine)
    existing = set(insp.get_table_names())
    to_drop = [t for t in DROP_ORDER if t in existing]
    if not to_drop:
        logger.info("No tables to drop.")
        return 0

    logger.warning("Dropping %d table(s)...", len(to_drop))

    if _is_postgres(engine):
        # CASCADE handles HNSW index + dependent FKs automatically
        with engine.begin() as conn:
            for t in to_drop:
                conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))
            conn.execute(text("DROP TABLE IF EXISTS schema_version CASCADE"))
    else:
        Base.metadata.drop_all(bind=engine)
        with engine.begin() as conn:
            try:
                conn.execute(text("DROP TABLE IF EXISTS schema_version"))
            except Exception:
                pass

    logger.info("✓ All tables dropped.")
    return 0


def cmd_reset(args, deps):
    """drop + init."""
    rc = cmd_drop(args, deps)
    if rc != 0:
        return rc
    return cmd_init(args, deps)


def cmd_check(_args, deps):
    """Compare DB tables to the ORM model. No changes."""
    inspect = deps["inspect"]
    engine = deps["engine"]
    Base = deps["Base"]

    insp = inspect(engine)
    db_tables = set(insp.get_table_names())
    orm_tables = set(Base.metadata.tables.keys())

    missing = orm_tables - db_tables
    extra = db_tables - orm_tables - {"schema_version"}

    if missing:
        logger.error("Tables defined in ORM but missing from DB:")
        for t in sorted(missing):
            logger.error("  - %s", t)
    if extra:
        logger.warning("Tables in DB not defined in ORM:")
        for t in sorted(extra):
            logger.warning("  - %s", t)

    if not missing and not extra:
        logger.info("✓ Schema matches ORM (%d tables)", len(orm_tables))
        # Postgres: also verify pgvector
        if _is_postgres(engine):
            ok = _verify_pgvector(engine, deps["text"])
            return 0 if ok else 2
        return 0

    if missing:
        logger.error("Run 'python -m scripts.db.manage init' to create missing tables.")
    return 1 if missing else 0


def cmd_status(_args, deps):
    """Show row counts per table."""
    inspect = deps["inspect"]
    engine = deps["engine"]
    text = deps["text"]

    insp = inspect(engine)
    db_tables = set(insp.get_table_names())

    logger.info("Table                Rows")
    logger.info("-------------------  -----------")
    total = 0
    with engine.connect() as conn:
        for t in TABLES_IN_ORDER + ["schema_version"]:
            if t not in db_tables:
                logger.info("%-20s  (missing)", t)
                continue
            try:
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0
                logger.info("%-20s  %s", t, f"{count:,}")
                if t != "schema_version":
                    total += count
            except Exception as exc:
                logger.error("%-20s  error: %s", t, exc)
    logger.info("-------------------  -----------")
    logger.info("%-20s  %s", "TOTAL", f"{total:,}")

    # Postgres-specific extras
    if _is_postgres(engine):
        with engine.connect() as conn:
            try:
                row = conn.execute(text(
                    "SELECT extname, extversion FROM pg_extension "
                    "WHERE extname IN ('vector','pgcrypto')"
                )).fetchall()
                logger.info("")
                logger.info("Postgres extensions:")
                for name, version in row:
                    logger.info("  %s %s", name, version)
            except Exception as exc:
                logger.warning("Could not query pg_extension: %s", exc)

    return 0


def cmd_pgvector(_args, deps):
    """Verify pgvector is installed and the embedding column type works."""
    engine = deps["engine"]
    text = deps["text"]

    if not _is_postgres(engine):
        logger.error("pgvector check only applies to Postgres backends")
        logger.info("Current backend: %s", engine.dialect.name)
        return 1

    return 0 if _verify_pgvector(engine, text) else 2


def cmd_seed(args, deps):
    """Insert minimal demo data — useful for local UI dev / smoke tests."""
    SessionLocal = __import__("app.db.session", fromlist=["SessionLocal"]).SessionLocal
    orm = deps["orm"]

    project_name = args.project or "Demo Project"
    property_name = args.prop or "Demo Industrial Building"
    tenant_name = args.tenant or "Demo Tenant LLC"

    db = SessionLocal()
    try:
        existing = db.query(orm.Tenant).filter_by(name=tenant_name).first()
        if existing:
            logger.info("Tenant '%s' already exists (id=%s) — skipping seed",
                        tenant_name, existing.id)
            return 0

        proj = orm.Project(id=str(uuid.uuid4()), name=project_name,
                           created_at=datetime.now(timezone.utc))
        db.add(proj)
        db.flush()

        prop = orm.Property(
            id=str(uuid.uuid4()),
            project_id=proj.id,
            name=property_name,
            property_type="Industrial",
            address="4900 Demo Road, Garner NC 27529",
        )
        db.add(prop)
        db.flush()

        tnt = orm.Tenant(
            id=str(uuid.uuid4()),
            property_id=prop.id,
            name=tenant_name,
            suite_number="100",
            abstract_type="Basic Economic Abstract",
            created_at=datetime.now(timezone.utc),
        )
        db.add(tnt)
        db.flush()

        doc = orm.Document(
            id=str(uuid.uuid4()),
            tenant_id=tnt.id,
            filename="demo_lease.pdf",
            storage_path="/data/seed/demo_lease.pdf",
            document_type="base_lease",
            document_order=0,
            ocr_status="pending",
            uploaded_at=datetime.now(timezone.utc),
        )
        db.add(doc)

        db.commit()
        logger.info("✓ Seeded:")
        logger.info("    project_id   = %s", proj.id)
        logger.info("    property_id  = %s", prop.id)
        logger.info("    tenant_id    = %s", tnt.id)
        logger.info("    document_id  = %s", doc.id)
    except Exception as exc:
        db.rollback()
        logger.error("Seed failed: %s", exc)
        return 2
    finally:
        db.close()
    return 0


def cmd_migrate(_args, deps):
    """Forward-only migration: add new ORM tables that aren't in DB yet.

    Does NOT drop or alter existing columns. For non-trivial migrations,
    use Alembic.
    """
    inspect = deps["inspect"]
    engine = deps["engine"]
    Base = deps["Base"]
    text = deps["text"]

    # Postgres: ensure extensions first
    if _is_postgres(engine):
        _ensure_pg_extensions(engine, text)

    insp = inspect(engine)
    existing = set(insp.get_table_names())
    orm_tables = list(Base.metadata.tables.keys())
    new_tables = [t for t in orm_tables if t not in existing]

    if not new_tables:
        logger.info("Schema is up to date.")
        return 0

    logger.info("Creating %d new table(s):", len(new_tables))
    for t in new_tables:
        logger.info("  + %s", t)

    Base.metadata.create_all(bind=engine, checkfirst=True)

    if _is_postgres(engine):
        _ensure_pgvector_index(engine, text)

    logger.info("✓ Migration applied.")
    _record_schema_version(deps, "migrate", note=f"added: {', '.join(new_tables)}")
    return 0


def cmd_sql(_args, deps):
    """Execute scripts/db/schema.sql directly. Postgres-only."""
    engine = deps["engine"]
    text = deps["text"]

    if not _is_postgres(engine):
        logger.error("Raw SQL path requires Postgres backend.")
        logger.info("Use 'init' for SQLite/other backends (uses ORM create_all).")
        return 1

    sql_file = Path(__file__).parent / "schema.sql"
    if not sql_file.exists():
        logger.error("schema.sql not found at %s", sql_file)
        return 1

    sql = sql_file.read_text()
    # psql backslash commands (\echo etc.) won't work over psycopg2 — strip them
    sql = "\n".join(
        line for line in sql.splitlines()
        if not line.strip().startswith("\\")
    )

    logger.info("Executing schema.sql via psycopg2 (%d bytes)...", len(sql))
    with engine.begin() as conn:
        # Postgres can run multiple statements in one execute() if separated by ;
        conn.execute(text(sql))
    logger.info("✓ schema.sql applied")
    return 0


# --------------------------------------------------------------------------- #
# Postgres helpers
# --------------------------------------------------------------------------- #

def _ensure_pg_extensions(engine, text) -> None:
    """Create pgcrypto + vector extensions if not present.

    Both are idempotent. pgcrypto provides gen_random_uuid(); vector
    provides the VECTOR type and HNSW index method.
    """
    extensions = ["pgcrypto", "vector"]
    with engine.begin() as conn:
        for ext in extensions:
            try:
                conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
                logger.info("  ✓ extension '%s' ready", ext)
            except Exception as exc:
                if ext == "vector":
                    logger.error("Failed to create 'vector' extension: %s", exc)
                    logger.error(
                        "Install pgvector first:\n"
                        "  - Bare-metal: apt install postgresql-15-pgvector\n"
                        "  - Docker:     use image pgvector/pgvector:pg15-or-later\n"
                        "  - Managed:    enable 'vector' extension in your provider"
                    )
                    raise
                else:
                    logger.warning("Could not create extension '%s': %s", ext, exc)


def _ensure_pgvector_index(engine, text) -> None:
    """Create the HNSW index on clause_embeddings.embedding if missing.

    SQLAlchemy's ORM doesn't model pgvector index parameters, so we create
    it imperatively. Safe to re-run.
    """
    sql = """
        CREATE INDEX IF NOT EXISTS ix_clause_emb_hnsw
            ON clause_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        logger.info("  ✓ HNSW index on clause_embeddings.embedding")
    except Exception as exc:
        logger.warning("Could not create HNSW index: %s", exc)
        logger.warning("Vector search will still work but be slower (sequential scan)")


def _verify_pgvector(engine, text) -> bool:
    """Run a tiny round-trip query to confirm pgvector is functional."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )).fetchone()
            if not row:
                logger.error("✗ 'vector' extension not installed")
                logger.error("  Run: scripts/db/manage.py init  (or CREATE EXTENSION vector)")
                return False
            version = row[0]
            logger.info("✓ pgvector %s installed", version)

            # Smoke test: cast a literal to vector, compute cosine distance
            result = conn.execute(text(
                "SELECT '[1,2,3]'::vector(3) <=> '[4,5,6]'::vector(3)"
            )).scalar()
            logger.info("✓ vector ops work (test distance=%.4f)", result)

            # Confirm HNSW index exists
            hnsw = conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE indexname = 'ix_clause_emb_hnsw'"
            )).fetchone()
            if hnsw:
                logger.info("✓ HNSW index present")
            else:
                logger.warning("⚠ HNSW index missing (will be created on next init)")

            return True
    except Exception as exc:
        logger.error("✗ pgvector verification failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Schema-version helper
# --------------------------------------------------------------------------- #

def _record_schema_version(deps, version: str, *, note: str = "") -> None:
    """Best-effort insert into schema_version. No-op if table missing."""
    text = deps["text"]
    engine = deps["engine"]
    inspect = deps["inspect"]
    insp = inspect(engine)

    if "schema_version" not in insp.get_table_names():
        try:
            with engine.begin() as conn:
                if _is_sqlite(engine):
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS schema_version (
                            version    TEXT NOT NULL PRIMARY KEY,
                            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            applied_by TEXT,
                            notes      TEXT
                        )
                    """))
                else:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS schema_version (
                            version    VARCHAR(32) NOT NULL PRIMARY KEY,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            applied_by VARCHAR(255),
                            notes      TEXT
                        )
                    """))
        except Exception as exc:
            logger.debug("schema_version create skipped: %s", exc)
            return

    try:
        with engine.begin() as conn:
            actor = os.environ.get("USER") or os.environ.get("USERNAME") or "manage.py"
            if _is_postgres(engine):
                conn.execute(
                    text("""
                        INSERT INTO schema_version (version, applied_by, notes)
                        VALUES (:v, :a, :n)
                        ON CONFLICT (version) DO UPDATE
                            SET applied_at = NOW(),
                                applied_by = EXCLUDED.applied_by,
                                notes = EXCLUDED.notes
                    """),
                    {"v": version, "a": actor[:255], "n": note[:1024]},
                )
            else:
                # SQLite — try insert, ignore PK conflict
                try:
                    conn.execute(
                        text("""
                            INSERT INTO schema_version (version, applied_by, notes)
                            VALUES (:v, :a, :n)
                        """),
                        {"v": version, "a": actor[:255], "n": note[:1024]},
                    )
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("schema_version insert skipped: %s", exc)


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #

def _redact(url: str) -> str:
    """Hide password in a database URL for logging."""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manage.py",
        description="LeaseGenie database management CLI (Postgres + pgvector)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init",     help="Create extensions + all tables (idempotent)")
    drop = sub.add_parser("drop", help="Drop all tables (DESTRUCTIVE)")
    drop.add_argument("--yes", action="store_true", help="Required for drop")
    reset = sub.add_parser("reset", help="drop + init (DESTRUCTIVE)")
    reset.add_argument("--yes", action="store_true", help="Required for reset")
    sub.add_parser("check",    help="Verify DB schema matches ORM (no changes)")
    sub.add_parser("status",   help="Show row counts per table + pgvector status")
    sub.add_parser("pgvector", help="Verify pgvector extension is installed and functional")
    sub.add_parser("migrate",  help="Add new ORM tables that don't yet exist")
    sub.add_parser("upgrade",  help="Alias for init (Alembic-style naming)")
    sub.add_parser("sql",      help="Execute scripts/db/schema.sql directly (Postgres only)")

    seed = sub.add_parser("seed", help="Insert minimal demo data")
    seed.add_argument("--project", default=None)
    seed.add_argument("--prop",    default=None)
    seed.add_argument("--tenant",  default=None)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    deps = _import_app()

    handlers = {
        "init":     cmd_init,
        "drop":     cmd_drop,
        "reset":    cmd_reset,
        "check":    cmd_check,
        "status":   cmd_status,
        "pgvector": cmd_pgvector,
        "seed":     cmd_seed,
        "migrate":  cmd_migrate,
        "upgrade":  cmd_init,
        "sql":      cmd_sql,
    }
    return handlers[args.cmd](args, deps)


if __name__ == "__main__":
    sys.exit(main())
