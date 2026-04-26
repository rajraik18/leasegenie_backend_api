## v2.2.0 - HYBRID deployment mode

User feedback: existing host installs of Postgres/Redis/Ollama (or corporate constraints) make the all-Docker default friction-prone on Windows. v2.2 makes the canonical deployment a hybrid: only application code (api + worker) runs in Docker; infrastructure runs natively on the host.

### What changed

**`docker-compose.yml`** completely rewritten. The `postgres`, `redis`, and `ollama` services are gone. Only `api` and `worker` remain. Both have:
- `extra_hosts: "host.docker.internal:host-gateway"` so the same hostname works on Linux without Docker Desktop
- All three connection strings now default to `host.docker.internal:N` (the special hostname Docker Desktop provides for reaching the host machine)
- All env values are configurable via `${VAR:-default}` syntax so `.env` overrides work
- Added `MAX_UPLOAD_SIZE_MB`, `MAX_UPLOAD_TOTAL_MB`, `MAX_PDFS_PER_REQUEST` env wiring (these settings existed in code but were not surfaced to compose)
- Added `restart: unless-stopped` for production-grade auto-restart
- Added a `healthcheck` block on the api container

The `postgres_data`, `redis_data`, `ollama_data` Docker volumes are gone. Only `api_data` (uploaded PDFs + generated exports) remains.

**`.env.example`** rewritten with hybrid-mode defaults. All three URLs (`DATABASE_URL`, `CELERY_BROKER_URL`, `OLLAMA_BASE_URL`) point to `host.docker.internal:N`. Inline comments explain what to install/configure on the host.

**`scripts/_common.sh` and `scripts/_Common.ps1`** — `ALL_SERVICES`/`AppServices` arrays narrowed to `(api worker)`. `INFRA_SERVICES` is now empty (infra is on the host).

**`scripts/start.sh` / `start.ps1`** rewritten. New "Phase 1 - verify host infrastructure" does:
- TCP connectivity check on Postgres host:port from `.env`
- TCP connectivity check on Redis host:port
- HTTP GET on Ollama `/api/tags`
- On `--cold` / `-Cold`: also queries `/api/tags` JSON to verify the configured `OLLAMA_MODEL` and `OLLAMA_EMBED_MODEL` are pulled
- Fails fast with a clear error and pointer to `DEPLOYMENT.md` if any host service is unreachable

The bash script translates `host.docker.internal` to `localhost` for the host-side TCP checks (since the host doesn't reach itself via that hostname). New `--skip-checks` / `-SkipChecks` flag bypasses pre-flight when you know the infra is up.

**`scripts/stop.sh` / `stop.ps1`** simplified. Only api+worker to drain. `-Clean` now removes only the `api_data` volume (no other volumes exist). Output explicitly notes that local Postgres/Redis/Ollama are NOT touched.

**`scripts/restart.sh` / `restart.ps1`** simplified. Only api+worker to restart.

**`scripts/db.sh` / `db.ps1`** rewritten. Now uses host `psql` and `pg_dump` instead of `docker compose exec postgres psql`. Falls back to running `manage.py` inside the api container for ORM-aware operations (`status`, `check`, `seed`). Parses connection details from `DATABASE_URL` in `.env`, translating `host.docker.internal` back to `localhost` for host-side execution.

**`scripts/status.sh` / `status.ps1`** rewritten. Shows Docker containers (api, worker) plus reachability of host services. The Ollama models section now queries the host's `/api/tags` directly via curl (bash) or `Invoke-WebRequest` (PowerShell).

**`scripts/daily_run.sh` / `daily_run.ps1`** rewritten. The backup step now uses host `pg_dump` instead of `docker compose exec postgres pg_dump`. The drain logic queries Postgres directly via host `psql`. Weekly `VACUUM ANALYZE` and audit_log prune also use host `psql`.

**`scripts/db/schema.sql`** — fixed the trailing diagnostic query that was using `tablename` (does not exist in `pg_stat_user_tables`) instead of `relname`. Also added `extraction_schemas` to the table list it reports.

**`DEPLOYMENT.md`** — section 1 architecture diagram redrawn for hybrid layout. Section 2 completely rewritten with step-by-step Windows host setup:
- Installing pgvector on native Windows Postgres (using pgvector-windows pre-built binaries)
- Configuring `pg_hba.conf` and `postgresql.conf` for Docker bridge connections
- Creating Windows Firewall rules for ports 5432, 6379, 11434
- Configuring Redis `bind 0.0.0.0`
- Setting `OLLAMA_HOST=0.0.0.0:11434` env var
- Pulling required Ollama models on the host
- Common host-setup pitfalls section (5 most common failures and their fixes)

### Migration from earlier versions

If you had v2.1 running with all 5 services in Docker:

```bash
# 1. Tear down the old all-Docker stack (data preserved if you skip -v)
docker compose down

# 2. Export your data from the Docker postgres
docker compose up -d postgres
docker compose exec postgres pg_dump -U leasegenie leasegenie > leasegenie_v21_export.sql

# 3. Stop and remove the Docker postgres permanently
docker compose down -v

# 4. Set up host Postgres per DEPLOYMENT.md section 2.2.A
# 5. Import the data:
psql -U leasegenie -d leasegenie -f leasegenie_v21_export.sql

# 6. Apply v2.2 with hybrid compose:
unzip leasegenie_api_v2.zip   # overwrites your code
.\scripts\start.ps1 -Cold
```

Same for Redis: data is ephemeral (Celery queue), no migration needed - just install host Redis and continue. For Ollama, the model weights are big (~20 GB); rather than copying them, just `ollama pull` them again on the host install - same models, different location.

### Why hybrid

- **Reuse existing infra.** Many shops already have Postgres, Redis, or Ollama running on dev machines for other projects. Hybrid avoids duplicating them.
- **Faster iteration.** When only api+worker are containerised, `docker compose build` is much faster (no DB or LLM in the image).
- **GPU passthrough is easier.** Ollama on the host can use the Windows GPU directly. Through Docker, GPU passthrough requires nvidia-container-toolkit setup which is fragile on Windows.
- **Backups via familiar tools.** `pg_dump` from PowerShell + `Compress-Archive` is more native than `docker compose exec`.
- **Less Docker disk pressure.** No multi-GB postgres/ollama images to download or store.

### What's NOT changed

- All API endpoints, Pydantic models, ORM, agents, playbooks: unchanged
- Schema-driven extraction: unchanged
- 79-field BRD support: unchanged
- Swagger UI at `/docs`: unchanged
- Excel export format: unchanged
- The `extraction_schemas` upload flow: unchanged
- All v2.1.x audit fixes (audit_log nullable, is_active inheritance, upload size limits, etc.): preserved

---

## v2.1.2 — Windows / PowerShell parity

Triggered by user feedback: bash scripts don't run on stock Windows PowerShell. Every `.sh` script now ships alongside a `.ps1` equivalent with the same behavior.

### What's new

Eight new files in `scripts/`:

| Bash | PowerShell | Notes |
|---|---|---|
| `_common.sh` | `_Common.ps1` | Shared helpers (logging, lock files, health polling, Compose detection) |
| `start.sh` | `start.ps1` | Modes: `-Cold`, `-Warm`, `-Infra`, `-Build`, `-NoPull` |
| `stop.sh` | `stop.ps1` | Modes: default, `-Down`, `-Clean`, `-Force` |
| `restart.sh` | `restart.ps1` | Strategies: `-Rolling`, `-Full`, `-Hard`. Scope: `-App`, `-Infra`, `-All` |
| `status.sh` | `status.ps1` | `-Watch` mode for auto-refresh |
| `logs.sh` | `logs.ps1` | Same positional args (service name, tail count) |
| `db.sh` | `db.ps1` | All 12 subcommands: init, migrate, upgrade-sql, drop, reset, sql, status, check, pgvector, seed, shell, logs, backup |
| `daily_run.sh` | `daily_run.ps1` | All flags: `-DryRun`, `-SkipBackup`, `-SkipRestart`, `-Full`, `-Weekly`, `-Force` |

### Design choices

**Idiomatic flags per platform.** Bash uses GNU long-options (`--cold`, `--dry-run`); PowerShell uses parameter switches (`-Cold`, `-DryRun`). The PowerShell scripts use `[CmdletBinding()]` and proper comment-based help so `Get-Help .\scripts\start.ps1 -Full` works.

**Compose detection** in PowerShell mirrors bash: tries `docker compose` (v2) first, falls back to `docker-compose` (v1). Result is cached for the duration of the script.

**Lock files** use the same `.lifecycle.lock` path so a bash and PowerShell script can't accidentally run simultaneously. PowerShell writes `$PID` (the PowerShell process PID) and clears it on exit via `Register-EngineEvent PowerShell.Exiting`.

**Backup compression on Windows** uses `Compress-Archive` (built into Windows 10+) since `gzip` isn't standard. Output is `.sql.zip` instead of `.sql.gz` — same content, different container.

**Logging on Windows** uses `Start-Transcript` so the daily run captures both stdout and stderr to `.\logs\daily_run.<date>.log`, mirroring the `tee` behavior on Linux.

**Health checks** parse Compose's `--format json` output via `ConvertFrom-Json` instead of the Python one-liner the bash version uses. Same logic, no Python dependency.

### Documentation

- `DEPLOYMENT.md` section 2.2 now has Windows-specific install instructions and a bash↔PowerShell command equivalents table covering 14 common operations.
- `DEPLOYMENT.md` daily-run section adds a Windows Task Scheduler setup with both GUI walkthrough and `Register-ScheduledTask` PowerShell command.
- `scripts/README.md` updated to document the dual-shipping arrangement.

### Static validation

All 8 PowerShell scripts pass structural checks: balanced braces and parens, well-formed `param()` blocks, no malformed `$env:` references. Total: 45 KB of PowerShell code mirroring 35 KB of bash.

(PowerShell parsing requires `pwsh` which is not in our build sandbox; CI environments running `pwsh -NoLogo -Command "Get-Command -Syntax .\scripts\*.ps1"` should validate the syntax fully.)

---

## v2.1.1 — Hardening & upgrade-path fixes (audit pass)

Triggered by an end-to-end deployment audit. Three real bugs caught and fixed; one upgrade gap closed; one defense-in-depth feature added. No new functionality — this is purely a hardening release on top of v2.1.

### Bugs fixed

**1. Schema upload would crash on Postgres** — `app/services/schema_store.py`
The schema-upload paths (`upsert_schema`, `set_active_schema`, `delete_schema`) wrote `audit_log` rows with `tenant_id="00000000-0000-0000-0000-000000000000"` to mark global (non-tenant) events. But `audit_log.tenant_id` had a NOT NULL FK constraint to `tenants(id)`, so every schema upload would fail with a foreign-key violation in production Postgres. SQLite let it slide because FK enforcement is off by default.

Fix: made `audit_log.tenant_id` nullable in both `scripts/db/schema.sql` (`ALTER COLUMN ... DROP NOT NULL` semantics) and `app/models/orm.py` (`Mapped[str | None]` with `nullable=True`). The schema_store now passes `None` for global events. Tenant-scoped events still cascade-delete with the tenant.

**2. Re-upload silently broke active extractions** — `app/services/schema_store.py`
`PUT /api/v1/schemas/{id}` created a new version with `is_active=False` even when the previous version was the active default. Extractions using the active default would silently keep using the old version — a stale-config bug that's hard to debug because it's not logged anywhere.

Fix: `upsert_schema()` now checks if any prior version of the same `schema_id` is active, and if so, transfers the flag to the new version (clearing it from older versions in the same statement). Adds an INFO-level log line when this happens.

**3. Unused import** — `app/api/v1/schemas.py`
Removed unused `SchemaConflict` import.

### Upgrade path closed

**`scripts/db/migrations/v2_0_0_upgrade.sql`** (new)
Idempotent migration script for existing v1 (MSSQL+ChromaDB) deployments OR pre-fix v2 builds. Uses `IF NOT EXISTS`, `DO $$ ... END $$` blocks, and `ON CONFLICT DO UPDATE` so re-running is safe. Covers:
- Both extensions (`pgcrypto`, `vector`)
- `audit_log.tenant_id` → nullable
- `extraction_schemas` table creation
- `extraction_jobs.schema_id` + `schema_version` column adds
- All four new indexes (GIN on JSONB, partial unique on is_active, slug lookup, schema_id partial)
- HNSW index on `clause_embeddings` if the table exists
- `schema_version` sentinel row

**`scripts/db.sh upgrade-sql`** (new subcommand)
Runs every `.sql` file in `scripts/db/migrations/` in lexical order. Can be called either through the API container (preferred) or directly via psql when the API can't start yet.

### Defense-in-depth: upload size limits

Previously the only limit on PDF uploads was a hardcoded count of 8 files. A 500 MB PDF would be accepted and consume worker memory during OCR. Three new settings now enforced in `app/api/v1/extract_pdf.py`:

| Setting | Default | Enforcement |
|---|---|---|
| `MAX_UPLOAD_SIZE_MB` | 100 | per-file limit |
| `MAX_UPLOAD_TOTAL_MB` | 500 | total per request |
| `MAX_PDFS_PER_REQUEST` | 8 | file count (was hardcoded; now config-driven) |

Validation is two-layer:
- **Pre-flight** uses `UploadFile.size` if Starlette set it (multipart with `Content-Length`); rejects with 413 + the offending file's actual size in MB before writing anything to disk
- **Streaming check** during write — for chunked uploads where `.size` is `None`, the writer accumulates bytes per 1 MB chunk and raises 413 + cleans up the partial file on overflow

Also added an explicit MIME/extension check up front (rejects 400 if the file is not `application/pdf` or `*.pdf`). Closes the gap where a non-PDF rename could be uploaded and only fail during OCR.

### Test docstring updates

`tests/test_vector_store.py` and `tests/test_coordinator_integration.py` had stale references to ChromaDB. Renamed `test_semantic_search_falls_back_when_chromadb_unavailable` → `..._when_vector_store_unavailable`. Test logic was already storage-agnostic (uses `monkeypatch`), no functional changes needed.

### Static audits added

Two ad-hoc audit scripts (in /tmp during validation, not shipped):
- Cross-module import resolution: every `from app.X import Y` verified to point at a real definition
- Circular-import detection via topological sort on top-level imports

Both passed on the released codebase. 0 errors across 61 Python files, 37 modules.

---

## v2.1 — Postgres + pgvector, schema-driven extraction, daily restart automation

Released alongside the lifecycle script toolkit. Three coordinated workstreams:

---

### Database: MSSQL → Postgres + pgvector

Eliminates the ChromaDB sidecar by moving embeddings into the same Postgres database the app already uses. One server instead of two; native ACID across vectors and metadata; cross-table joins now possible.

- `docker-compose.yml` — `mssql` and `mssql-init` services replaced with `postgres` (image `pgvector/pgvector:pg16`). `chroma_data` volume removed; `postgres_data` added. `scripts/db/schema.sql` is now mounted to `/docker-entrypoint-initdb.d/01_schema.sql` for automatic first-boot init.
- `Dockerfile` — ODBC Driver 18 / `msodbcsql18` install removed. `psycopg2-binary` is wheel-only so no compile deps needed. Tesseract install retained.
- `requirements.txt` — `pyodbc==5.1.0` and `chromadb==0.5.20` replaced with `psycopg2-binary==2.9.10` and `pgvector==0.3.6`.
- `app/config.py` — `DATABASE_URL` defaults to a Postgres URL; new `is_postgres` property; `is_mssql` retained for backward compat but not used.
- `app/db/session.py` — Postgres-first engine with `pool_pre_ping`, `application_name=leasegenie-api`, JIT off (better latency on small queries).
- `app/services/vector_store.py` — full rewrite. New `PgVectorStore` class uses raw SQL against the `clause_embeddings` table with pgvector's `<=>` cosine distance operator. `_NoOpVectorStore` for the SQLite dev fallback. The public interface (`VectorStore`, `VectorHit`, `get_vector_store()`) is unchanged so callers don't update.
- `scripts/db/schema.sql` — Postgres DDL with 10 tables, `pgcrypto` and `vector` extensions, HNSW index on `clause_embeddings.embedding` (m=16, ef_construction=64), JSONB GIN index on `extraction_schemas`.
- `scripts/db/manage.py` — Python CLI (`init`, `drop`, `reset`, `check`, `status`, `pgvector`, `seed`, `migrate`, `sql`). Auto-detects Postgres vs SQLite. The `pgvector` subcommand runs a real cosine-distance round-trip to verify the extension is functional.
- `scripts/db.sh` — bash wrapper with two paths: through the API container (preferred — uses ORM, multi-backend) or directly via `psql` (cold-start path before API can boot). Adds `shell`, `logs`, `backup` subcommands.

---

### Schema-driven extraction (new feature)

Users can now upload JSON schemas that select or define which fields the extraction pipeline runs. This shifts the product from "fixed 79 BRD playbooks" to "user-defined extraction templates" while remaining 100% backward compatible — extractions without a schema run the full BRD set as before.

- `app/models/orm.py` — new `ExtractionSchema` table (id, schema_id, version, name, description, schema_json JSONB, is_active, created_by, timestamps). `extraction_jobs` gained `schema_id` + `schema_version` columns to track which schema each job ran against.
- `app/services/schema_validator.py` (new, ~370 lines) — structural + semantic validation. Catches: missing required fields, `use_playbook` references to unknown IDs, custom field IDs that collide with built-ins (with `override: true` escape hatch), invalid output_type / search_scope / condition_type / branch_type values, malformed branches. Returns structured errors for HTTP 400 responses with `path`, `code`, `message` per error.
- `app/services/schema_store.py` (new) — CRUD layer with auto-version-bumping on re-upload (preserves history via `audit_log`), `set_active_schema()` enforcing single-active invariant.
- `app/agents/playbook_loader.py` (new) — materializes a schema into the `{field_id: Playbook}` dict shape the executor expects. Supports both `use_playbook` references (point to built-ins) and inline custom field definitions. When `override: true` is set on an inline field that collides with a built-in, sensibly merges built-in defaults where the schema is silent.
- `app/agents/specialists/base.py` — added `_playbooks_override` slot and `set_playbooks()` method. `applicable_playbooks()` uses the override when set, otherwise falls back to `get_playbooks()`.
- `app/agents/coordinator.py` — `Coordinator.run()` gained a `schema_doc: dict | None = None` parameter. When provided, calls `load_playbooks_for_schema()` and propagates the filtered playbook set to every specialist via `set_playbooks()`.
- `app/api/v1/schemas.py` (new router) — 8 endpoints: `POST /schemas` (upload via file or JSON body), `POST /schemas/validate` (dry-run), `GET /schemas` (list), `GET /schemas/{id}` (get one, optional `?version=`), `GET /schemas/{id}/versions` (history), `PUT /schemas/{id}` (re-upload, auto-bumps version), `DELETE /schemas/{id}` (all versions), `POST /schemas/{id}/activate` (set as default).
- `app/api/v1/extract_pdf.py` — added `schema_id` query parameter on `POST /extract/pdf`. Added 8-PDF upload limit. Resolved schema_id + version stored on the resulting `ExtractionJob`.
- `app/workers/tasks.py` and `app/services/pipeline.py` — propagate `schema_id` from job → worker → pipeline → coordinator. Pipeline resolves the JSON document at run time so a schema can be re-uploaded without affecting in-flight jobs.
- `data/schemas_examples/example_minimal.json` — sample schema demonstrating both reference and inline patterns. Includes few-shot examples for the inline `esg_sustainability` field.

---

### Daily restart automation

`scripts/daily_run.sh` (~340 lines) — designed for cron, systemd timer, or Task Scheduler. Default mode: drain in-flight jobs → pg_dump backup → rolling restart of api + worker → health check.

Key behaviors:
- **Pre-flight** queries Postgres for `extraction_jobs` in `running` status; waits up to `LEASEGENIE_DRAIN_TIMEOUT` (default 30 min) before forcing a restart.
- **Backup** runs `pg_dump | gzip` to `./backups/daily/leasegenie_<TS>.sql.gz`; prunes older than `LEASEGENIE_BACKUP_RETENTION_DAYS` (default 7).
- **Rolling restart** (default) recycles only `api` + `worker` so the 20 GB Ollama models don't re-download. Use `--full` for a complete recycle.
- **Health check** polls `/health` for up to `LEASEGENIE_HEALTH_TIMEOUT` (default 90 s); exits 2 on failure so cron can alert.
- **Optional weekly tasks** (`--weekly` flag) run `VACUUM ANALYZE` and prune `audit_log` rows older than 90 days.
- All output appends to `./logs/daily_run.<date>.log` for next-morning review.

Cron examples baked into `--help`:

```
0 3 * * *      /opt/leasegenie/scripts/daily_run.sh
0 4 * * 0      /opt/leasegenie/scripts/daily_run.sh --weekly
```

---

### Swagger / OpenAPI improvements

- `openapi_tags` registered with paragraph descriptions per tag (extract, schemas, extraction, orders, documents, fields, abstraction, playbooks, meta) — Swagger sidebar now reads as a guided flow.
- Numbered operation summaries: "1. Upload a schema (optional)", "2. Upload PDFs and trigger extraction", "3. Check job progress", "4. Download result (JSON or Excel)".
- Realistic request/response examples on `POST /extract/pdf`, `GET /jobs/{id}`, `GET /jobs/{id}/result`.
- `JobOut` Pydantic model gained `schema_id` + `schema_version` fields and a `json_schema_extra` example that pre-fills Swagger's "Try it out".
- API title bumped to v2.0.0; description includes a quick-start checklist.
# LeaseGenie API — Accuracy Upgrade Changelog

## v2.0 — Corpus-driven accuracy pass (Weeks 1-3)

Addresses findings from the Sample 1 corpus analysis (17 PDFs, 1,192 pages) which projected current pipeline at ~60-70% accuracy vs. a 90%+ target. Nine discrete changes across OCR, playbooks, applicability, retrieval, prompting, derivation, voting, and verification.

Projected corpus-weighted accuracy after all changes: **86-91% → 90-94% with Tier 2 retrieval**.

---

### Week 1 — Foundation (required to reach 85%+)

**Step 1 — OCR upgrade with garble detection** ⟶ `app/services/ocr.py`
- Replaced single-pass pdfplumber with quality-scored parallel extraction
- Paddle OCR preferred backend (lazy-loaded, ~280 MB); Tesseract fallback
- New `_count_orphaned_money_values()` detects the subtle Sample 1 corruption pattern where money-field labels are followed by non-numeric garbage (e.g., `Common Area Charges: w`)
- Verified on real Sample 1 page 1: recovered all 4 previously-lost dollar amounts ($683.57 CAM, $540.68 Insurance, $428.68 Mgt Fee, $12,713.69 Total)
- `PageText` dataclass extended with `quality_score`, `digital_score`, `ocr_score`
- Strategy: run pdfplumber and score → if garbled or empty, run OCR and score → pick higher-scoring output (OCR must beat digital by 10 % margin to override)

**Step 2 — Eight missing HIGH-severity playbooks** ⟶ `data/playbooks_compiled/`
Added 8 new playbook JSONs, each following the full schema (overview, Q1→Q2→Q3 decision trees, keywords grounded in real ProLogis/HMBP-BCP phrasing, summary_keywords, amendment_controls):
- `late_payment` — late fee %, grace period, first-late waiver
- `move_out_conditions` — surrender standard, HVAC cert, restoration
- `notices` — notice addresses, delivery methods, "deemed given" timing
- `indemnification` — mutual indemnities, carve-outs
- `rules_and_regulations` — location, modification right
- `estoppel_certificate` — response deadline, consequence of non-delivery
- `force_majeure` — qualifying events, rent-obligation carve-out
- `brokers` — named brokers, commission allocation

Index updated: 71 → 79 playbooks total.

**Step 3 — Applicability gating + document_type auto-classification**
- `data/playbooks_compiled/*.json` — normalized `property_applicability` across **all 79 playbooks**. The 9 retail-only fields (`advertisement`, `marketing`, `reporting_of_gross_sales`, `sales_kick_out`, `co_tenancy`, `breakpoint`, `percentage_rent`, `continuous_operation`, `go_dark`) now carry `{Retail: True, Industrial: False, Office: False, Mixed-Use: True}`. All others have explicit `{all: True}`.
- `app/services/doc_classifier.py` (NEW) — auto-detects `document_type` (base_lease/amendment/sublease/guaranty) and `property_type` from keyword patterns. Tested on Sample 1 → Industrial ✓, Sample 6 → Industrial ✓, and 4 synthetic cases — all 6 pass.
- `app/agents/coordinator.py` — `_build_context()` now classifies each document; `run()` auto-infers `property_type` when caller passes empty; warns when a caller-labeled "base_lease" is actually an amendment or sublease.

---

### Week 2 — Accuracy boosters

**Step 4 — Self-consistency voting on numeric fields** ⟶ `app/agents/playbook_executor.py`
- New `_ask_with_voting()` runs the LLM N=3 at temp=0.3
- Majority-votes YES/NO; tie-breaks conservatively (NO > UNKNOWN > YES)
- Mode-selects canonical value across winning samples; sets `needs_review=True` when samples disagree on the value
- Gated by `_should_use_voting()` predicate → fires for currency/date/number/percentage output types only (prose fields stay single-shot)
- Helper `_canonicalize_value()` normalizes currency variants (`$1,234.56` ≡ `$1234.56` ≡ `1234.56`), date formats (`June 1, 2009` ≡ `6/1/2009` → `2009-06-01`), day counts, percentages — tested 11/11 cases

**Step 5 — Derived fields consolidation** ⟶ `app/services/derived_fields.py` (NEW)
- Start-date priority resolver: `original_lease_commencement_date` → `term_commencement_date` → `rent_commencement_date` → `most_recent_lease_start`
- Emits virtual `canonical_commencement_date` (category: Basic Information, output_type: Derived) with source_field_id and cross-field notes explaining overrides
- Address composer: `street_address + city + state` → `property_address` with comma-separated format
- `cross_field_notes_for()` attaches provenance notes to the non-winning component fields so the UI can show derivation
- Wired into coordinator after reconciliation; tested 3/3 cases

**Step 6 — Few-shot examples per playbook** ⟶ `app/agents/playbook_executor.py` + `app/agents/few_shot_library.py` (NEW)
- Added `few_shot_examples: list[dict]` field to `Playbook` schema
- `PER_QUESTION_USER` prompt template gained `{few_shot_block}` slot
- `_build_few_shot_block()` helper loads from (a) `playbook.few_shot_examples` (per-playbook) or (b) merged `FEW_SHOT_LIBRARY` (in-file, 8 flagship fields) with external `EXTENDED_FEW_SHOT_LIBRARY` (72 fields)
- **100% coverage achieved — all 79 playbooks now have at least one few-shot example.** Examples are grounded in Sample 1 (ProLogis) and Sample 6 (HMBP-BCP) real excerpts, with synthetic examples for retail-only fields
- Caps at 3 examples per question; question-id scoped so Q2 of `notices` shows Landlord-address example and Q3 shows Tenant-address example

---

### Week 3 — Retrieval and verification quality

**Step 7 — Hybrid BM25 + vector retrieval with RRF fusion** ⟶ `app/agents/tools.py`
- Added `hybrid_search()` to `DocumentContext`
- Lazy-builds in-memory clause-vector matrix on first call via existing `Embedder`
- Reciprocal Rank Fusion formula `score = Σ 1/(60 + rank_r)` (Cormack, Clarke & Buettcher 2009)
- Query both retrievers, fuse top-30 from each, return top-k
- Graceful fallback to pure BM25 when Ollama/embeddings unavailable
- Executor updated to prefer `hybrid_search` with `top_k=10` (deeper pool for the reranker)
- Tested end-to-end: literal keywords win via BM25, paraphrases win via vectors, clauses ranked by both retrievers fuse to top

**Step 8 — BGE cross-encoder reranker** ⟶ `app/services/reranker.py` (NEW)
- Wraps `BAAI/bge-reranker-base` (CPU-friendly, 278 MB)
- Lazy singleton loader, `MAX_PAIRS=50` latency guard, `MIN_CANDIDATES=3` skip threshold
- Executor retrieves top-10 from hybrid_search, reranks, keeps top-5
- Graceful fallback — if sentence-transformers not installed, returns candidates unchanged (never raises)
- `is_available()` cheap feature-detection check

**Step 9 — Critique agent for hallucination defense** ⟶ `app/agents/critique_agent.py` (NEW)
- Second narrow LLM call verifying `(field, value, clause_text)` coherence
- JSON schema: `{supports: bool, corrected_value: str|null, failure_mode, reasoning}`
- Failure modes detected: `hallucinated`, `wrong_number`, `wrong_party`, `wrong_date`, `partial`, `ambiguous`
- `should_critique()` gates firing to 20 high-stakes fields (currency, dates, party names) out of 79 — keeps cost proportional to impact
- `apply_critique_to_result()`:
  - supports=False → halves confidence, sets `needs_review=True`, appends red_flag
  - supports=True → +0.05 confidence boost (capped at 0.95)
  - Optional opt-in `apply_corrections=True` swaps value when critique supplied a correction
- Never raises — LLM/network errors return `ran=False` with safe defaults
- Tested 11/11 cases: supports=True boost, supports=False demote, apply_corrections swap, broken LLM, each failure mode

---

### Testing

New integration test suite ⟶ `tests/test_coordinator_integration.py`

14 tests covering:
- Document classifier (real text + applicability gates on the 9 retail-only playbooks)
- OCR garble detection against the Sample 1 corruption pattern
- Derived fields (canonical start date priority, fall-through, address composition)
- Voting canonicalization (currency variants, date variants, gating logic)
- Critique agent (demotion, boost, broken LLM safety)
- Few-shot library coverage (100% of 79 playbooks)
- Hybrid search RRF fusion correctness

In the production environment (with ollama, rank_bm25, pdfplumber, paddleocr or tesseract, sentence-transformers, pydantic-settings installed), all 14 tests should pass. In the sandbox where pydantic_settings is missing, 11/14 run and pass; 3 are correctly skipped.

---

### Files changed

**New files (7):**
- `app/services/doc_classifier.py`
- `app/services/derived_fields.py`
- `app/services/reranker.py`
- `app/agents/critique_agent.py`
- `app/agents/few_shot_library.py`
- `add_missing_playbooks.py` (generator script)
- `tests/test_coordinator_integration.py`
- Plus 8 new playbook JSONs in `data/playbooks_compiled/`

**Modified files (6):**
- `app/services/ocr.py` — parallel OCR with quality scoring
- `app/agents/coordinator.py` — classification + derived-fields pipeline
- `app/agents/playbook_executor.py` — voting + few-shot + hybrid retrieval + reranker + critique hook
- `app/agents/tools.py` — `hybrid_search` with RRF
- `app/agents/playbooks/schema.py` — `few_shot_examples` field
- 77 playbook JSONs — normalized applicability gates
- `data/playbooks_compiled/_index.json` — count 71→79

---

### Deployment notes

Optional dependencies this release introduces (each has graceful fallback):

| Dependency | Purpose | Sandbox-optional? | Install |
|---|---|---|---|
| `paddleocr` | Primary OCR backend | Yes (Tesseract fallback) | `pip install paddleocr` |
| `sentence-transformers` | BGE reranker | Yes (retrieval works without) | `pip install sentence-transformers` |
| `BAAI/bge-reranker-base` | 278 MB model | Auto-download on first rerank | (downloaded lazily) |

Existing required dependencies still apply: `ollama`, `pdfplumber`, `rank_bm25`, `pydantic-settings`, `chromadb` (for vector_store path).

---

### Projected accuracy

| Subset | Before | After Tier 1 | After Tier 2 (this release) |
|---|---|---|---|
| Clean digital leases (15 of 17) | 65-75% | 82-88% | 88-93% |
| Scanned leases (Sample 14, 18) | 15-35% | 70-82% | 78-87% |
| **Corpus-weighted** | **60-70%** | **80-85%** | **86-94%** |

Validation requires running against live Ollama (qwen2.5:32b) with ~3 hand-labeled ground-truth documents.
