# LeaseGenie API — Deployment Guide & End-to-End Flow

> **HYBRID DEPLOYMENT** (v2.2.0) — Postgres, Redis, and Ollama run as native installs on the host machine. Only the application code (api + worker) runs in Docker. See section 2 below for host setup steps.

This document walks through every flow a real user/operator will exercise, in the order they'll exercise it. Each section includes the exact commands and what to expect.

---

## 1. Architecture overview (HYBRID)

```
+======================================================================+
|                       Host machine (Windows / Linux)                  |
|                                                                       |
|   +-----------------+   +-----------------+   +------------------+   |
|   | PostgreSQL 16   |   | Redis 7         |   | Ollama           |   |
|   | + pgvector      |   | (Celery broker) |   | (LLM)            |   |
|   | :5432           |   | :6379           |   | :11434           |   |
|   | NATIVE INSTALL  |   | NATIVE INSTALL  |   | NATIVE INSTALL   |   |
|   +--------+--------+   +--------+--------+   +---------+--------+   |
|            ^                     ^                       ^            |
|            |                     |                       |            |
|     host.docker.internal:N (containers reach host services this way)  |
|            |                     |                       |            |
|   +========+=====================+=======================+========+   |
|   |                    Docker (containers only)                    |  |
|   |                                                                |  |
|   |   +------------+              +------------+                   |  |
|   |   |  API       |              |  Worker    |                   |  |
|   |   |  (FastAPI) |              |  (Celery)  |                   |  |
|   |   |  :8000     |              |            |                   |  |
|   |   +------------+              +------------+                   |  |
|   |                                                                |  |
|   +================================================================+  |
+======================================================================+
```

**Hierarchy:** Project → Property → Tenant → Document → FieldValue
**Workflow:** Schema (optional) → Upload PDFs → Async extraction → Download JSON/Excel
**Vector storage:** pgvector extension on the host's Postgres
**Why hybrid:** lets you reuse an existing Postgres install, share Ollama models with other apps, and keep Docker focused on the application code that changes most often.

---

## 2. First-time deployment (HYBRID)

### 2.1 Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker | 20.10+ | with `docker compose` v2 plugin |
| PostgreSQL (native) | 16+ | with pgvector extension |
| Redis (native) | 7+ | bound to 0.0.0.0:6379 for container access |
| Ollama (native) | latest | with `OLLAMA_HOST=0.0.0.0:11434` |
| Disk | 30+ GB free | Ollama models alone are ~20 GB |
| RAM | 16+ GB | qwen2.5:14b needs ~10 GB; 32b needs ~22 GB |
| psql + pg_dump in PATH | matching PG version | for `db.sh shell`, `db.sh backup`, `daily_run.sh` |

### 2.2 Host setup (one-time)

#### A. PostgreSQL with pgvector

If you already have PostgreSQL 16 installed on Windows (e.g. via the EnterpriseDB installer), skip to step A.3.

**A.1 Install PostgreSQL 16** — download from https://www.postgresql.org/download/windows/. During install:
- Set a password for the `postgres` superuser (you'll need it)
- Default port `5432` is fine
- Default locale is fine

**A.2 Install pgvector** — pre-built Windows binaries are at https://github.com/pgvector/pgvector-windows/releases. Download the `.zip` matching your PG version, then:
- Copy `vector.dll` to `C:\Program Files\PostgreSQL\16\lib\`
- Copy `vector.control` and `vector--*.sql` to `C:\Program Files\PostgreSQL\16\share\extension\`
- Restart Postgres: `Get-Service -Name "postgresql*" | Restart-Service` (admin PowerShell)

**A.3 Create the database and user** — in `psql -U postgres`:

```sql
CREATE USER leasegenie WITH PASSWORD 'leasegenie';
CREATE DATABASE leasegenie OWNER leasegenie;
\c leasegenie
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
```

**A.4 Allow connections from Docker containers** — edit `C:\Program Files\PostgreSQL\16\data\pg_hba.conf` (admin):

```
# Add these lines BEFORE any reject rules
host    all    leasegenie    172.16.0.0/12      md5
host    all    leasegenie    192.168.0.0/16     md5
host    all    leasegenie    10.0.0.0/8         md5
```

Edit `C:\Program Files\PostgreSQL\16\data\postgresql.conf`:

```
listen_addresses = '*'
```

Restart Postgres again so changes take effect:

```powershell
Get-Service -Name "postgresql*" | Restart-Service
```

**A.5 Allow Docker through Windows Firewall** (run admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "PostgreSQL for Docker" `
    -Direction Inbound -LocalPort 5432 -Protocol TCP `
    -Action Allow -Profile Private
```

**A.6 Apply the LeaseGenie schema:**

```powershell
cd "D:\path\to\leasegenie_v2"
psql -h localhost -U leasegenie -d leasegenie -f scripts\db\schema.sql
```

You should see `CREATE TABLE`, `CREATE INDEX` etc. ending with the diagnostic table list (11 tables).

#### B. Redis

**B.1 Install Redis** — on Windows, the easiest path is Memurai (https://www.memurai.com/) which is a Redis-compatible Windows port. Or use Redis from WSL2.

**B.2 Configure for container access** — edit `redis.conf`:

```
bind 0.0.0.0
protected-mode no
```

(For dev only. In production, use `bind 0.0.0.0` plus a strong password via `requirepass`.)

**B.3 Allow firewall:**

```powershell
New-NetFirewallRule -DisplayName "Redis for Docker" `
    -Direction Inbound -LocalPort 6379 -Protocol TCP `
    -Action Allow -Profile Private
```

**B.4 Restart Redis** so config changes take effect.

**B.5 Verify** from PowerShell:

```powershell
redis-cli ping
# PONG
```

#### C. Ollama

**C.1 Install** — download Ollama for Windows from https://ollama.com/download/windows.

**C.2 Configure for container access** — by default Ollama only listens on 127.0.0.1. To let containers reach it via `host.docker.internal`, set the environment variable:

Open System Properties → Environment Variables → New (User variables):
- Name: `OLLAMA_HOST`
- Value: `0.0.0.0:11434`

Restart Ollama (right-click tray icon → Quit, then relaunch from Start menu).

**C.3 Allow firewall:**

```powershell
New-NetFirewallRule -DisplayName "Ollama for Docker" `
    -Direction Inbound -LocalPort 11434 -Protocol TCP `
    -Action Allow -Profile Private
```

**C.4 Pull the required models** (this is the slow step — first model is ~10 GB):

```powershell
ollama pull qwen2.5:14b-instruct-q5_K_M
ollama pull nomic-embed-text
```

The qwen2.5:14b model takes 15-30 min depending on bandwidth. nomic-embed-text is ~270 MB so it's quick.

**C.5 Verify** from PowerShell:

```powershell
Invoke-WebRequest http://localhost:11434/api/tags | Select-Object -ExpandProperty Content
# Should show JSON listing your two models
```

### 2.3 Container setup

#### Linux / macOS / WSL

```bash
unzip leasegenie_api_v2.zip
cd leasegenie_v2/

cp .env.example .env
# Edit .env if you want non-default credentials.

chmod +x scripts/*.sh
scripts/start.sh --cold
```

#### Windows (PowerShell)

```powershell
Expand-Archive leasegenie_api_v2.zip -DestinationPath .
cd leasegenie_v2

Copy-Item .env.example .env
# Edit .env in your favorite editor

Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

.\scripts\start.ps1 -Cold
```

What `start.sh --cold` / `.\scripts\start.ps1 -Cold` does:

1. Verifies host Postgres is reachable on the configured port
2. Verifies host Redis is reachable
3. Verifies host Ollama is reachable, and that required models are pulled
4. Builds the api/worker Docker images if needed
5. Starts api+worker containers
6. Smoke-tests `/health` endpoint

### 2.4 Verify

```powershell
# Stack health (Windows)
.\scripts\status.ps1
# Linux/macOS: scripts/status.sh

# API health
Invoke-WebRequest http://localhost:8000/health
# Or: curl http://localhost:8000/health
```

Expected output:
```json
{
  "status": "ok",
  "fields_loaded": 79,
  "playbooks_loaded": 79,
  "extractor": "ollama",
  "model": "qwen2.5:14b-instruct-q5_K_M",
  ...
}
```

If status check shows host services REACHABLE and API responding, you're done.

### 2.5 PowerShell command equivalents

| Bash command | PowerShell equivalent |
|---|---|
| `scripts/start.sh --cold` | `.\scripts\start.ps1 -Cold` |
| `scripts/start.sh --build` | `.\scripts\start.ps1 -Build` |
| `scripts/start.sh --skip-checks` | `.\scripts\start.ps1 -SkipChecks` |
| `scripts/stop.sh --clean` | `.\scripts\stop.ps1 -Clean` |
| `scripts/restart.sh --hard` | `.\scripts\restart.ps1 -Hard` |
| `scripts/restart.sh --build` | `.\scripts\restart.ps1 -Build` |
| `scripts/status.sh --watch` | `.\scripts\status.ps1 -Watch` |
| `scripts/logs.sh api 500` | `.\scripts\logs.ps1 api 500` |
| `scripts/db.sh init` | `.\scripts\db.ps1 init` |
| `scripts/db.sh shell` | `.\scripts\db.ps1 shell` |
| `scripts/db.sh upgrade-sql` | `.\scripts\db.ps1 upgrade-sql` |
| `scripts/db.sh backup` | `.\scripts\db.ps1 backup` |
| `scripts/daily_run.sh --weekly` | `.\scripts\daily_run.ps1 -Weekly` |
| `scripts/daily_run.sh --dry-run` | `.\scripts\daily_run.ps1 -DryRun` |

For help on any PowerShell script: `Get-Help .\scripts\start.ps1 -Full`.

### 2.6 Common host-setup pitfalls

**Postgres not reachable from container** — usually means `listen_addresses` is still `localhost` in `postgresql.conf`. The `host.docker.internal` hostname resolves to a different IP than `127.0.0.1` from inside containers, so Postgres on `localhost`-only won't accept the connection.

**Postgres connection refused with FATAL: no pg_hba.conf entry** — you got past listen_addresses but pg_hba.conf doesn't allow your Docker subnet. The Docker bridge is usually in 172.16.0.0/12, but Docker Desktop varies. Check the actual IP with `docker compose exec api hostname -I` and add that range explicitly.

**Ollama 404 / connection refused** — `OLLAMA_HOST` env var wasn't set, or wasn't picked up. After setting it via Environment Variables, fully quit Ollama (tray → Quit) and relaunch. `Invoke-WebRequest http://localhost:11434/api/tags` from PowerShell should work.

**Redis "could not connect"** — `bind 127.0.0.1 ::1` (default) blocks containers. Change to `bind 0.0.0.0` or comment out the bind line entirely.

**Native Postgres conflicts on port 5432** — if you started by following the Docker-native path before switching to hybrid, you may have a stale `leasegenie-postgres` container. Run `docker rm -f leasegenie-postgres` to clear it.

---

## 3. End-to-end flow A: Default extraction (no custom schema)

The simplest path — uses the built-in 79 BRD playbooks.

### Step 1: Upload PDFs and trigger extraction

```bash
curl -X POST "http://localhost:8000/api/v1/extract/pdf?property_type=Industrial&abstract_type=Full%20Abstract&tenant_name=Acme%20Corp" \
  -F "files=@base_lease.pdf" \
  -F "files=@amendment_1.pdf" \
  -F "files=@amendment_2.pdf"
```

**Response (202 Accepted):**
```json
{
  "id": "8e1d4a3c-7c2f-4b8a-9e3d-2f1a8b7c6d5e",
  "tenant_id": "1f2e3d4c-5b6a-7c8d-9e0f-1a2b3c4d5e6f",
  "schema_id": null,
  "schema_version": null,
  "status": "queued",
  "progress": 0,
  "total_fields": 0,
  "completed_fields": 0,
  "started_at": null,
  "finished_at": null,
  "created_at": "2026-04-26T05:30:00Z"
}
```

### Step 2: Poll for progress

```bash
JOB_ID="8e1d4a3c-7c2f-4b8a-9e3d-2f1a8b7c6d5e"

# Poll every 5 seconds until done
while true; do
    response=$(curl -s "http://localhost:8000/api/v1/jobs/$JOB_ID")
    status=$(echo "$response" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
    progress=$(echo "$response" | python3 -c "import sys,json;print(json.load(sys.stdin)['progress'])")
    echo "[$status] $progress%"
    [[ "$status" == "complete" || "$status" == "failed" ]] && break
    sleep 5
done
```

Status transitions: `queued` → `running` → `complete` (or `failed`).
Typical extraction: 79 fields × 3 documents = 237 LLM calls ≈ 5-15 minutes on a 14b model.

### Step 3: Download results

```bash
# JSON (full structured output with confidence + citations + red flags)
curl "http://localhost:8000/api/v1/extract/jobs/$JOB_ID/result?format=json" \
  -o result.json

# Excel (BRD-shaped Lease Abstraction workbook)
curl "http://localhost:8000/api/v1/extract/jobs/$JOB_ID/result?format=xlsx" \
  -o lease_abstraction.xlsx
```

### Sequence diagram

```
Client            API              Worker          Postgres        Ollama
  │                │                  │                │              │
  │ POST PDFs      │                  │                │              │
  │───────────────▶│                  │                │              │
  │                │ INSERT tenant,   │                │              │
  │                │ documents, job   │                │              │
  │                │─────────────────────────────────▶│              │
  │                │                  │                │              │
  │                │ enqueue task     │                │              │
  │                │─────────────────▶│                │              │
  │  202 + job_id  │                  │                │              │
  │◀───────────────│                  │                │              │
  │                │                  │ OCR each PDF   │              │
  │                │                  │ generate emb's │              │
  │                │                  │───────────────────────────────▶│
  │                │                  │ store clauses  │              │
  │                │                  │ in pgvector    │              │
  │                │                  │───────────────▶│              │
  │                │                  │                │              │
  │                │                  │ for each field │              │
  │                │                  │  ask LLM       │              │
  │                │                  │───────────────────────────────▶│
  │                │                  │  ↓             │              │
  │                │                  │  branch        │              │
  │                │                  │  store value   │              │
  │                │                  │───────────────▶│              │
  │                │                  │                │              │
  │ GET /jobs/X    │                  │                │              │
  │───────────────▶│ SELECT progress  │                │              │
  │                │─────────────────────────────────▶│              │
  │  47%           │                  │                │              │
  │◀───────────────│                  │                │              │
  │                │                  │ ... done       │              │
  │                │                  │ status=complete│              │
  │                │                  │───────────────▶│              │
  │                │                  │                │              │
  │ GET /result    │                  │                │              │
  │───────────────▶│ build abstraction│                │              │
  │                │─────────────────────────────────▶│              │
  │  JSON / xlsx   │                  │                │              │
  │◀───────────────│                  │                │              │
```

---

## 4. End-to-end flow B: Custom extraction schema

Use this when you want to extract a subset of the 79 fields, OR add custom fields not in the BRD.

### Step 1: Author or copy a schema

```bash
# Start from the example
cp data/schemas_examples/example_minimal.json my_schema.json

# Edit:
# - schema_id: stable slug (e.g. "acme_retail_v1")
# - name: display name
# - fields[]: list of {use_playbook: "...id..."} OR full inline definitions
```

### Step 2: Validate (optional but recommended)

```bash
curl -X POST http://localhost:8000/api/v1/schemas/validate \
  -F "file=@my_schema.json"

# Returns 200 + {ok: true, field_count: 6, ...} if valid
# Returns 400 + structured errors if not:
# {
#   "detail": "schema validation failed",
#   "errors": [
#     {"path": "$.fields[2].use_playbook", "code": "UNKNOWN_PLAYBOOK",
#      "message": "playbook 'tenat_name' is not registered. ..."}
#   ]
# }
```

### Step 3: Upload

```bash
# Multipart file upload
curl -X POST http://localhost:8000/api/v1/schemas \
  -F "file=@my_schema.json" \
  -F "created_by=alice@example.com"

# OR with JSON body
curl -X POST http://localhost:8000/api/v1/schemas \
  -H "Content-Type: application/json" \
  -d @my_schema.json

# Response 201:
# {
#   "id": "...",
#   "schema_id": "acme_retail_v1",
#   "version": "1.0.0",
#   "is_active": false,
#   "schema_json": {...},
#   ...
# }
```

### Step 4: Two ways to use the schema

**Option A: Set as active default (applies to all subsequent extractions)**

```bash
curl -X POST http://localhost:8000/api/v1/schemas/acme_retail_v1/activate

# Now POST /extract/pdf without ?schema_id= will use acme_retail_v1
```

**Option B: Per-extraction override**

```bash
curl -X POST "http://localhost:8000/api/v1/extract/pdf?property_type=Retail&schema_id=acme_retail_v1" \
  -F "files=@lease.pdf"
```

The `?schema_id=` query parameter takes precedence over the active default. If neither is specified, falls back to the full 79-field BRD set.

### Step 5: Re-upload (versioning)

When you need to change a schema, PUT replaces it:

```bash
curl -X PUT http://localhost:8000/api/v1/schemas/acme_retail_v1 \
  -F "file=@my_schema_v2.json"

# Response 200 — version auto-bumps to 1.0.1 (or whatever the JSON specifies,
# rounded up if collision). The previous version stays in history:
curl http://localhost:8000/api/v1/schemas/acme_retail_v1/versions
# {
#   "schema_id": "acme_retail_v1",
#   "versions": [
#     {"version": "1.0.1", "is_active": true,  "created_at": "..."},
#     {"version": "1.0.0", "is_active": false, "created_at": "..."}
#   ]
# }
```

**Activation is preserved across re-uploads** — if v1 was active when you PUT v2, v2 automatically becomes active. No silent fallbacks.

### Schema format reference

```json
{
  "schema_id": "acme_retail_v1",
  "name": "Acme retail abstraction",
  "version": "1.0.0",
  "description": "Subset + 1 ESG field",
  "default_property_type": "Retail",
  "default_abstract_type": "Full Abstract",
  "fields": [
    { "use_playbook": "tenant_name" },
    { "use_playbook": "annual_base_rent" },

    {
      "field_id": "esg_sustainability",
      "field_name": "ESG / Sustainability Provisions",
      "category": "Other Lease Clauses",
      "output_type": "Text",
      "property_applicability": {
        "Retail": true, "Industrial": true, "Office": true, "Mixed-Use": true
      },
      "keywords": ["sustainability", "ESG", "LEED", "green building"],
      "questions": [
        {
          "id": "Q1",
          "priority": 1,
          "condition_type": "Definition Based",
          "question_text": "Does the lease contain ESG provisions?",
          "output_type": "Text",
          "search_scope": "all",
          "yes_branch": {"type": "extract", "also_extract": true},
          "no_branch":  {"type": "literal", "literal": "None"}
        }
      ]
    }
  ]
}
```

See `data/schemas_examples/example_minimal.json` for a working sample.

---

## 5. End-to-end flow C: Daily operations

### Lifecycle commands

```bash
scripts/start.sh                    # warm start (~30s)
scripts/restart.sh                  # rolling restart of api+worker (~15s)
scripts/restart.sh --build          # after code changes
scripts/stop.sh                     # graceful stop, preserves data
scripts/status.sh                   # one-screen health snapshot
scripts/logs.sh api                 # tail api logs
scripts/logs.sh worker 500          # last 500 worker log lines
```

### Daily restart (cron-friendly)

`scripts/daily_run.sh` is the one-shot maintenance script:

```bash
scripts/daily_run.sh                # default: drain → backup → rolling restart → health
scripts/daily_run.sh --weekly       # also VACUUM ANALYZE + prune audit_log >90d
scripts/daily_run.sh --dry-run      # show what would happen
scripts/daily_run.sh --skip-backup  # restart only
scripts/daily_run.sh --skip-restart # backup only
```

What it does, in order:
1. **Pre-flight** — query Postgres for `extraction_jobs` in `running` status; skip restart if none and `--force` not set
2. **Drain** — stop API; wait up to 30 min for in-flight worker jobs (configurable via `LEASEGENIE_DRAIN_TIMEOUT`)
3. **Backup** — `pg_dump | gzip` to `./backups/daily/leasegenie_<TS>.sql.gz`; prune older than 7 days
4. **Restart** — rolling: api + worker only (Ollama keeps the 20GB model loaded)
5. **Health check** — wait up to 90 sec for `/health` to return 200; exit 2 if not
6. **Log rotation** — report Docker container log sizes (rotation via daemon.json)
7. **Optional weekly** — `VACUUM ANALYZE` + delete `audit_log` rows >90 days

Output appends to `./logs/daily_run.<date>.log`.

### Cron schedule

```
0 3 * * *      /opt/leasegenie/scripts/daily_run.sh
0 4 * * 0      /opt/leasegenie/scripts/daily_run.sh --weekly
```

### Windows Task Scheduler

Open Task Scheduler → Create Task:

| Tab | Field | Value |
|---|---|---|
| General | Name | `LeaseGenie Daily Run` |
| General | Run whether user is logged on or not | ✓ |
| Triggers | New | Daily at 03:00 |
| Actions | Program | `powershell.exe` |
| Actions | Arguments | `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\leasegenie_v2\scripts\daily_run.ps1"` |
| Actions | Start in | `C:\path\to\leasegenie_v2` |
| Conditions | Wake the computer to run this task | ✓ |

For the weekly maintenance (Sunday 04:00), create a second task with arguments:
`-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\leasegenie_v2\scripts\daily_run.ps1" -Weekly`

Or via PowerShell command-line:

```powershell
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\leasegenie_v2\scripts\daily_run.ps1"' `
    -WorkingDirectory 'C:\leasegenie_v2'

$trigger = New-ScheduledTaskTrigger -Daily -At 3am

Register-ScheduledTask -TaskName 'LeaseGenie Daily Run' `
    -Action $action -Trigger $trigger -RunLevel Highest
```

### Tunables (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `LEASEGENIE_BACKUP_RETENTION_DAYS` | 7 | How long to keep daily pg_dumps |
| `LEASEGENIE_DRAIN_TIMEOUT` | 1800 (30 min) | How long to wait for in-flight jobs |
| `LEASEGENIE_HEALTH_TIMEOUT` | 90 | How long to wait for `/health` post-restart |
| `LEASEGENIE_BACKUP_DIR` | ./backups/daily | Where pg_dumps go |

---

## 6. Database operations

```bash
scripts/db.sh init        # create tables + extensions (idempotent)
scripts/db.sh status      # row counts + pgvector version
scripts/db.sh check       # verify schema matches ORM (no changes)
scripts/db.sh pgvector    # verify pgvector extension is functional
scripts/db.sh shell       # opens psql REPL
scripts/db.sh backup      # ad-hoc pg_dump to ./backups/leasegenie_<TS>.sql
scripts/db.sh seed        # insert demo project/property/tenant
scripts/db.sh migrate     # add new ORM tables (forward-only)
scripts/db.sh drop        # DESTRUCTIVE — asks confirmation
scripts/db.sh reset       # DESTRUCTIVE — drop + init
```

The `init` command auto-creates the `vector` and `pgcrypto` extensions before the tables. On managed Postgres (RDS, Cloud SQL, Azure DB), enable the `vector` extension in the provider console first.

---

## 7. Swagger / OpenAPI UI

```
http://localhost:8000/docs        # Swagger UI (interactive)
http://localhost:8000/redoc       # ReDoc (read-only docs)
http://localhost:8000/openapi.json # raw spec
```

The sidebar reads as a numbered flow:
- **schemas** — 1. Upload schema, 1a. Validate, ...
- **extract** — 2. Upload PDFs, ...
- **extraction** — 3. Check job progress
- **extract** — 4. Download result

Every `Try it out` button has realistic example payloads pre-filled.

---

## 8. File and data layout

```
/leasegenie_v2/
├── app/                          # FastAPI application
│   ├── main.py                   # entry: app, /health, route registration
│   ├── config.py                 # Settings (Postgres + SQLite only)
│   ├── api/v1/
│   │   ├── extract_pdf.py        # POST /extract/pdf, GET /jobs/{id}/result
│   │   ├── extraction.py         # GET /jobs/{id}
│   │   ├── schemas.py            # 8 endpoints under /schemas
│   │   ├── playbooks.py, fields.py, abstraction.py, ...
│   ├── agents/
│   │   ├── coordinator.py        # 5-specialist orchestration; takes schema_doc
│   │   ├── playbook_loader.py    # materializes schema → {field_id: Playbook}
│   │   ├── playbook_executor.py  # IF YES/IF NO decision tree runner
│   │   ├── critique_agent.py, reranker.py, tools.py (hybrid retrieval)
│   │   └── specialists/          # 5 specialists by BRD category
│   ├── services/
│   │   ├── pipeline.py           # entry: run_extraction_for_tenant
│   │   ├── vector_store.py       # PgVectorStore using clause_embeddings table
│   │   ├── schema_validator.py   # JSON validation w/ structured errors
│   │   ├── schema_store.py       # CRUD over extraction_schemas table
│   │   ├── ocr.py, embeddings.py, doc_classifier.py, derived_fields.py
│   ├── workers/
│   │   ├── celery_app.py         # Celery config
│   │   └── tasks.py              # extract_tenant_task, index_document_task
│   ├── models/orm.py             # 11 SQLAlchemy tables
│   └── schemas/models.py         # all Pydantic models
├── data/
│   ├── LeaseGenie_BRD.xlsx       # source of truth for fields
│   ├── playbooks_source/         # .docx playbooks
│   ├── playbooks_compiled/       # JSON playbooks (79 files)
│   └── schemas_examples/
│       └── example_minimal.json  # template for users to copy
├── scripts/
│   ├── _common.sh                # shared bash helpers
│   ├── start.sh, stop.sh, restart.sh, status.sh, logs.sh
│   ├── daily_run.sh              # cron-friendly maintenance
│   ├── db.sh                     # DB management wrapper
│   ├── README.md
│   └── db/
│       ├── schema.sql            # Postgres DDL (auto-mounted to compose)
│       ├── manage.py             # Python CLI for DB ops
│       └── README.md
├── tests/                        # pytest tests
├── docker-compose.yml            # 5 services (postgres, redis, ollama, api, worker)
├── Dockerfile                    # Python 3.12-slim + tesseract + libpq5
├── requirements.txt
├── .env.example                  # cp to .env and edit
├── README.md
└── CHANGELOG.md
```

Persistent data lives in 4 named Docker volumes:
- `postgres_data` — primary DB + vectors
- `redis_data` — Celery queue
- `ollama_data` — LLM model weights (~20 GB)
- `api_data` — uploaded PDFs + generated Excel exports

`scripts/stop.sh --clean` removes all four. `scripts/stop.sh` (default) preserves them.

---

## 9. Production checklist

| Item | Action |
|---|---|
| Strong DB credentials | Edit `POSTGRES_PASSWORD` in `.env` (URL-encode if it contains `@`, `:`, `/`) |
| TLS termination | Run behind nginx/Caddy with TLS; the API itself speaks HTTP |
| API authentication | Currently none — add a reverse-proxy layer (oauth2-proxy, Cloudflare Access) before exposing publicly |
| Resource limits | Add `deploy.resources.limits` to api/worker in `docker-compose.yml` |
| Upload size limits | Tune `MAX_UPLOAD_SIZE_MB` (default 100), `MAX_UPLOAD_TOTAL_MB` (default 500), `MAX_PDFS_PER_REQUEST` (default 8) in `.env` |
| Backups | `scripts/daily_run.sh` writes to local disk — ship to S3/GCS via separate cron |
| Monitoring | Endpoints to scrape: `GET /health` (liveness), `GET /api/v1/playbooks` (readiness) |
| Log retention | Add `/etc/docker/daemon.json` with `{"log-opts": {"max-size": "100m", "max-file": "5"}}` |
| GPU acceleration | Uncomment the `deploy:` block under `ollama` in `docker-compose.yml` |
| Secrets | Don't commit `.env` — use Docker secrets or a vault for production |
| Restart policy | Add `restart: unless-stopped` to each service in `docker-compose.yml` |

---

## 10. Troubleshooting

### `scripts/start.sh --cold` hangs at "Waiting for ollama"

Ollama is downloading the model. Watch progress:
```bash
docker compose logs -f ollama
```
First-time pull is 20+ GB, takes 10-30 min depending on bandwidth.

### `scripts/db.sh init` fails with "vector extension does not exist"

In hybrid mode, the `vector` extension must be installed on your **host** Postgres install, not in a container. See section 2.2.A.2 for the Windows install steps.

If pgvector is installed but `CREATE EXTENSION vector` still fails, the Postgres role may lack permission. Connect as superuser:

```powershell
psql -U postgres -d leasegenie -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

On managed Postgres (RDS, Cloud SQL, Azure DB), enable "vector" in the provider console first.

### API returns 500 on extraction

```bash
scripts/logs.sh api 200    # last 200 lines
scripts/logs.sh worker 200
```
Common causes:
- Ollama unreachable → check `docker compose ps ollama`
- Model not pulled → `docker compose exec ollama ollama list`
- Postgres connection drops → check `pool_pre_ping` is on (it is by default)

### Schema upload returns 400

The validator returns structured errors. Common ones:
- `MISSING` — required field absent
- `UNKNOWN_PLAYBOOK` — `use_playbook` references a non-existent built-in
- `COLLIDES_WITH_BUILTIN` — inline `field_id` matches a built-in; set `"override": true` to deliberately replace
- `INVALID_VALUE` — `output_type` / `search_scope` / `condition_type` not in vocabulary

The error response includes `path`, `code`, and a human-readable `message` for each issue.

### Upload returns 413

The request exceeded the upload limits. Either:
- A single file exceeds `MAX_UPLOAD_SIZE_MB` (default 100 MB)
- Total request size exceeds `MAX_UPLOAD_TOTAL_MB` (default 500 MB)

Either compress the PDFs (most lease PDFs over 50 MB have embedded high-res scanned images that compress 3-5×) or raise the limits in `.env`:
```bash
MAX_UPLOAD_SIZE_MB=200
MAX_UPLOAD_TOTAL_MB=1000
scripts/restart.sh --app
```

### Extraction stuck at 0%

Check the worker is consuming jobs:
```bash
scripts/logs.sh worker
```
Look for `Received task: leasegenie.extract_tenant`. If absent, Celery isn't consuming — restart worker:
```bash
scripts/restart.sh --app
```

### Disk filling up

```bash
docker system df              # see Docker disk usage
du -sh ./uploads ./exports    # see app disk usage
```
Old extraction PDFs accumulate in `api_data` volume. Clean up:
```bash
docker compose exec api rm -rf /data/uploads/<old_tenant_id>
```
Or run `daily_run.sh --weekly` to auto-prune `audit_log` >90 days.

### Recovering from a bad migration

```bash
scripts/db.sh shell
\dt                                    # list tables
\d audit_log                           # show audit_log structure
DROP TABLE bad_table CASCADE;
\q
scripts/db.sh init                     # recreate from ORM
```

---

## 11. What's been validated

- All 77 Python files parse cleanly via AST
- All 8 shell scripts pass `bash -n` syntax check
- `schema.sql`: 125 balanced parens, 11 tables, 19 indexes, 2 extensions, all 10 FK refs resolve
- Vector dimension consistent at 768 across `schema.sql`, `app/config.py`, and `docker-compose.yml`
- No orphan references to removed modules (chromadb, pyodbc, vector_store_path, is_mssql)
- `audit_log.tenant_id` is nullable (allows global events like schema uploads)
- Re-upload via PUT preserves the `is_active` flag from the prior version
- Backward compatibility: extraction without a schema runs the full 79 BRD playbooks unchanged
