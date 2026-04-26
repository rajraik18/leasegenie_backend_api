# LeaseGenie DB Management

Three-layer toolkit for managing the database schema:

| Layer | File | When to use |
|---|---|---|
| Bash wrapper | `scripts/db.sh` | Day-to-day operations from a host shell |
| Python CLI | `scripts/db/manage.py` | Multi-backend (Postgres + SQLite); used inside the API container |
| Pure SQL | `scripts/db/schema.sql` | DBA review, direct `psql` execution, cold-start bootstrap |

## Backend support

| Backend | Status | How tables get created | Vector storage |
|---|---|---|---|
| **PostgreSQL 14+ with pgvector 0.7+** | Primary | `manage.py init` (creates extensions + tables) OR `psql -f schema.sql` | Native `VECTOR(768)` column with HNSW index |
| SQLite 3.x | Dev fallback | `manage.py init` (uses ORM `create_all`) | Stub — no vector index; in-memory cosine fallback |

Postgres is required for production. SQLite works for unit tests and offline development but loses pgvector — vector search falls back to slower in-memory cosine in that mode.

## Required Postgres extensions

Two extensions must be installed before `init` runs:

| Extension | Purpose | Install path |
|---|---|---|
| `pgcrypto` | `gen_random_uuid()` for primary keys | Bundled with Postgres ≥ 13 |
| `vector` (pgvector) | `VECTOR` column type + HNSW index | `apt install postgresql-15-pgvector`, or use `pgvector/pgvector:pg15-or-later` Docker image, or enable the "vector" extension in your managed Postgres provider |

`manage.py init` runs `CREATE EXTENSION IF NOT EXISTS` for both — your Postgres role just needs CREATEDB-level privileges. On managed Postgres (RDS, Cloud SQL, etc.) you may need to enable the extension via the provider console first, then `manage.py init` will succeed.

## Two paths to initialize

```bash
# Path A: through the API container (preferred — uses ORM, multi-backend)
scripts/db.sh init
# This calls: docker compose exec api python -m scripts.db.manage init

# Path B: direct psql (Postgres only — useful when API can't start yet)
scripts/db.sh sql
# This calls: cat scripts/db/schema.sql | docker compose exec postgres psql ...
```

Path A is preferred because it uses the same SQLAlchemy version + DATABASE_URL the app does — guaranteed consistency. Path B is the escape hatch for cold start before the API has a database to connect to.

## Daily operations

```bash
# Schema lifecycle
scripts/db.sh init        # create tables + extensions (idempotent)
scripts/db.sh check       # verify DB matches ORM (no changes)
scripts/db.sh migrate     # add new ORM tables (forward-only)
scripts/db.sh status      # row counts + pgvector version

# pgvector verification
scripts/db.sh pgvector    # confirms extension installed + functional + HNSW index

# Demo data
scripts/db.sh seed
scripts/db.sh seed --tenant "Acme Corp" --project "Q4 2026"

# Inspection
scripts/db.sh shell       # opens psql REPL
scripts/db.sh logs        # tails postgres logs

# Backups (DEV only — production should use pg_basebackup or managed snapshots)
scripts/db.sh backup      # pg_dump to ./backups/leasegenie_<timestamp>.sql

# Destructive (asks for confirmation)
scripts/db.sh drop
scripts/db.sh reset       # drop + init
```

## What gets created

The schema has 9 tables plus a `schema_version` sentinel:

| Table | Rows per | Why it exists |
|---|---|---|
| `projects` | client engagement | Top-level container |
| `properties` | building | Has property_type for retail-gate filtering |
| `tenants` | tenant in a building | Has abstract_type (BRD field) |
| `documents` | base lease + amendments | Up to 8 per tenant (1 base + 7 amend) via UNIQUE(tenant_id, document_order) |
| `field_values` | extracted (tenant, doc, field) | One row per of 79 fields × N documents |
| `field_overrides` | manual override | One row per (tenant, field) — takes precedence over extraction |
| `audit_log` | every value change | Append-only, indexed by (tenant, field, time DESC) |
| `extraction_jobs` | async job | Celery worker tracking — status/progress/error |
| `clause_embeddings` | one per clause | **Native pgvector — replaces ChromaDB**; HNSW index for cosine search |
| `schema_version` | one per migration | Used by `manage.py` to detect schema drift |

### Vector storage detail

`clause_embeddings.embedding` is `VECTOR(768)` — matches `OLLAMA_EMBED_DIM` for the default `nomic-embed-text` embedder. To swap embedders:

| Embedder | Dimension | What to change |
|---|---|---|
| `nomic-embed-text` (default) | 768 | (no change) |
| `bge-large-en-v1.5` | 1024 | `VECTOR(768)` → `VECTOR(1024)` in schema.sql; `OLLAMA_EMBED_DIM=1024` in .env |
| `text-embedding-3-small` (OpenAI) | 1536 | `VECTOR(1536)`; `OLLAMA_EMBED_DIM=1536` |

Changing dimensions requires re-embedding all clauses. Plan a maintenance window if you switch.

The HNSW index uses `m=16, ef_construction=64`. These defaults work well for ~10K-1M vectors at ~95-98% recall@5. For larger corpora, increase `ef_construction` to 128 or 256 — slower index build, better recall.

## CI / regression gates

```bash
# In CI, after applying schema:
scripts/db.sh check    # exit 0 if schema matches ORM, exit 1 if drift
scripts/db.sh pgvector # exit 0 if pgvector functional, exit 2 if not
```

Both commands are non-destructive and safe to run on production.

## What this does NOT do

- **No Alembic-style migrations.** `migrate` only ADDS tables. Column type changes, drops, and data migrations require Alembic. Add Alembic to `requirements.txt` and run `alembic init alembic` if you need that — the existing scripts are designed not to conflict.
- **No automated backups.** `scripts/db.sh backup` is a one-shot pg_dump to local disk. Production should use pg_basebackup, WAL archiving, or your managed-Postgres provider's snapshot feature.
- **No replication setup.** Single-instance only. For HA, use Patroni / RDS Multi-AZ / Cloud SQL HA.
- **No row-level security.** All authorization is enforced at the API layer.

## Troubleshooting

**"`vector` extension does not exist"** — The pgvector extension isn't installed at the cluster level. Install it (see "Required Postgres extensions"), then re-run `scripts/db.sh init`.

**"permission denied to create extension"** — Your DB role doesn't have privileges. Either grant them (`ALTER USER leasegenie WITH SUPERUSER` — DEV only) or have a DBA create the extensions once with `CREATE EXTENSION vector;` and your role can then use them.

**`scripts/db.sh init` fails with "neither api nor postgres is running"** — You haven't started the stack yet. Run `scripts/start.sh --infra` first to bring up just postgres + redis + ollama, then `scripts/db.sh init` to create the schema, then `scripts/start.sh` to start the API.

**HNSW index build is slow on large datasets** — Expected. For >100K clauses, the index can take several minutes. To check progress: `scripts/db.sh shell` then `SELECT * FROM pg_stat_progress_create_index;`.
