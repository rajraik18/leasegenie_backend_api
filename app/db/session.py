"""Database engine and session management.

Supports PostgreSQL with pgvector (production) and SQLite (dev fallback only).
Postgres uses psycopg2 with pool_pre_ping. The pgvector extension is
expected to be installed at the cluster level — see scripts/db/README.md.

Engine options (connection pooling, JIT off for predictable small-query
latency, application_name tag) are applied automatically based on the
DATABASE_URL scheme.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from app.config import settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    """Build the SQLAlchemy engine with backend-appropriate options."""
    if settings.is_sqlite:
        # Dev / unit-test fallback. pgvector is not available — vector search
        # falls back to in-memory cosine via app.agents.tools._vector_search,
        # and app.services.vector_store returns a no-op store.
        return create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            echo=False,
        )

    if settings.is_postgres:
        # psycopg2 + pgvector. JIT is disabled because for our query shapes
        # (lots of small lookups + the occasional ANN search) the JIT
        # planning overhead exceeds the speedup. application_name tags our
        # connections in pg_stat_activity so DBAs can identify the workload.
        return create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            connect_args={
                "application_name": "leasegenie-api",
                "options": "-c jit=off",
            },
            echo=False,
        )

    raise RuntimeError(
        f"Unsupported DATABASE_URL scheme: {settings.database_url!r}. "
        "Only PostgreSQL (postgresql+psycopg2://) and SQLite (sqlite:///) "
        "are supported. See .env.example."
    )


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """FastAPI dependency — yields a session and closes on teardown.

    Rolls back on exception so a half-applied transaction is not returned
    to the connection pool in a dirty state.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables. Called on application startup.

    Postgres production path: tables are created by docker-compose's mounted
    schema.sql on first boot, OR by `scripts/db.sh init` for managed Postgres.
    This `init_db()` is a fallback that runs `Base.metadata.create_all` —
    it's idempotent and does NOT install the pgvector extension or create
    the HNSW index on `clause_embeddings.embedding`. For full setup use
    `scripts/db.sh init` which calls `scripts/db/manage.py init`.
    """
    from app.models import orm  # noqa: F401
    Base.metadata.create_all(bind=engine)
