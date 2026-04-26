# LeaseGenie DB Management

Three-layer toolkit for managing the database schema:

| Layer | File | When to use |
|---|---|---|
| PowerShell wrapper | `scripts\db.ps1` | Day-to-day operations from a Windows shell |
| Python CLI | `scripts\db\manage.py` | Multi-backend (Postgres + SQLite); invoked by `db.ps1` |
| Pure SQL | `scripts\db\schema.sql` | DBA review, direct `psql` execution, cold-start bootstrap |

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
| `vector` (pgvector) | `VECTOR` column type + HNSW index | Pre-built Windows binaries: https://github.com/pgvector/pgvector-windows/releases. Or, on managed Postgres (RDS / Cloud SQL), enable the "vector" extension via the provider console. |

`manage.py init` runs `CREATE EXTENSION IF NOT EXISTS` for both — your Postgres role just needs CREATEDB-level privileges. On managed Postgres you may need to enable the extension via the provider console first, then `manage.py init` will succeed.

## Two paths to initialise

```powershell
# Path A: through the venv (preferred — uses ORM, multi-backend)
.\scripts\db.ps1 init
# This calls: .\.venv\Scripts\python.exe -m scripts.db.manage init

# Path B: direct psql (Postgres only — useful when the venv isn't ready yet)
.\scripts\db.ps1 sql
# This calls: psql -h <host> -p <port> -U <user> -d <db> -f scripts\db\schema.sql
```

Path A is preferred because it uses the same SQLAlchemy version + DATABASE_URL the app does — guaranteed consistency. Path B is the escape hatch for a cold-start before Python is set up.

## Daily operations

```powershell
# Schema lifecycle
.\scripts\db.ps1 init        # create tables + extensions (idempotent)
.\scripts\db.ps1 check       # verify DB matches ORM (no changes)
.\scripts\db.ps1 migrate     # add new ORM tables (forward-only)
.\scripts\db.ps1 status      # row counts + pgvector version

# pgvector verification
.\scripts\db.ps1 pgvector    # confirms extension installed + functional + HNSW index

# Demo data
.\scripts\db.ps1 seed
.\scripts\db.ps1 seed --tenant "Acme Corp" --project "Q4 2026"

# Inspection
.\scripts\db.ps1 shell       # opens psql REPL

# Backups (DEV only — production should use pg_basebackup or managed snapshots)
.\scripts\db.ps1 backup      # pg_dump to .\backups\leasegenie_<timestamp>.sql

# Destructive (asks for confirmation)
.\scripts\db.ps1 drop
.\scripts\db.ps1 reset       # drop + init
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
| `clause_embeddings` | one per clause | Native pgvector — HNSW index for cosine search |
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

```powershell
# In CI, after applying schema:
.\scripts\db.ps1 check    # exit 0 if schema matches ORM, exit 1 if drift
.\scripts\db.ps1 pgvector # exit 0 if pgvector functional, exit 2 if not
```

Both commands are non-destructive and safe to run on production.

## Migrations

Two layered tools:

| When to use | Tool |
|---|---|
| **Additive change** (new table, new column with `NULL` allowed) | `.\scripts\db.ps1 migrate` -- runs the ORM `create_all`. Idempotent. No undo. |
| **Non-additive change** (drop, type change, data migration, NOT NULL with default) | `.\scripts\db.ps1 alembic-revision -m "<msg>" --autogenerate` -- generates a versioned migration in `alembic/versions/` that you commit. Apply with `alembic-upgrade`, roll back with `alembic-downgrade -1`. |

The Alembic baseline (`alembic/versions/0001_baseline.py`) is a no-op -- existing v3.0 deployments already have the schema, so the very first `alembic-upgrade` just stamps `0001` as applied. The next migration in the chain will be the first one with real DDL.

```powershell
# Daily flow for a schema change
.\scripts\db.ps1 alembic-revision -m "add tenant.contact_email" --autogenerate
# Edit alembic/versions/<id>_add_tenant_contact_email.py if needed
.\scripts\db.ps1 alembic-upgrade
.\scripts\db.ps1 alembic-current        # confirm the new revision is applied

# Rollback if something goes sideways
.\scripts\db.ps1 alembic-downgrade -1
```

## What this does NOT do

- **No automated backups.** `.\scripts\db.ps1 backup` is a one-shot pg_dump to local disk. Production should use pg_basebackup, WAL archiving, or your managed-Postgres provider's snapshot feature.
- **No replication setup.** Single-instance only. For HA, use Patroni / RDS Multi-AZ / Cloud SQL HA.
- **No row-level security.** All authorization is enforced at the API layer.

## Troubleshooting

**"`vector` extension does not exist"** — The pgvector extension isn't installed at the cluster level. Install it (see "Required Postgres extensions"), then re-run `.\scripts\db.ps1 init`.

**"permission denied to create extension"** — Your DB role doesn't have privileges. Either grant them (`ALTER USER leasegenie WITH SUPERUSER` — DEV only) or have a DBA create the extensions once with `CREATE EXTENSION vector;` and your role can then use them.

**`.\scripts\db.ps1 init` fails with "Host Postgres not reachable"** — Postgres isn't running on the configured host:port. Start the Windows Postgres service (`Get-Service -Name "postgresql*" | Start-Service`) and re-run.

**HNSW index build is slow on large datasets** — Expected. For >100K clauses, the index can take several minutes. To check progress: `.\scripts\db.ps1 shell` then `SELECT * FROM pg_stat_progress_create_index;`.
