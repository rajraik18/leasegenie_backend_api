# LeaseGenie API — Deployment Guide (Windows-native, v3.0)

> All components run on a single Windows host as native processes. There is no Docker dependency. Postgres + pgvector, Redis, and Ollama are installed as Windows services. The API and Celery worker run as Python processes from a project virtual environment — directly during dev, registered as Windows Services in production.

---

## 1. Architecture overview

```
+======================================================================+
|                            Windows host                              |
|                                                                      |
|   +-----------------+   +-----------------+   +------------------+   |
|   | PostgreSQL 16   |   | Redis 7         |   | Ollama           |   |
|   | + pgvector      |   | (Celery broker) |   | (LLM)            |   |
|   | localhost:5432  |   | localhost:6379  |   | localhost:11434  |   |
|   | NATIVE INSTALL  |   | NATIVE INSTALL  |   | NATIVE INSTALL   |   |
|   +--------+--------+   +--------+--------+   +---------+--------+   |
|            ^                     ^                       ^           |
|            |                     |                       |           |
|   +========+=====================+=======================+========+  |
|   |                    Python venv (.venv)                       |  |
|   |                                                              |  |
|   |   +-------------+              +-------------+               |  |
|   |   |  uvicorn    |              |  celery     |               |  |
|   |   |  app.main   |              |  worker     |               |  |
|   |   |  :8000      |              |             |               |  |
|   |   +-------------+              +-------------+               |  |
|   |                                                              |  |
|   |  Dev:  scripts\start.ps1 -- background or foreground         |  |
|   |  Prod: deploy\windows\install-services.ps1 (Windows Services)|  |
|   +==============================================================+  |
+======================================================================+
```

**Hierarchy:** Project -> Property -> Tenant -> Document -> FieldValue
**Workflow:** Schema (optional) -> Upload PDFs -> Async extraction -> Download JSON / Excel
**Vector storage:** pgvector extension on the host's Postgres

---

## 2. First-time deployment

### 2.1 Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | Add to PATH during install |
| PostgreSQL | 16+ | with pgvector extension |
| Redis | 7+ | listening on 6379 |
| Ollama | latest | with the required models pulled |
| `psql` + `pg_dump` in PATH | matching PG version | for `db.ps1 shell`, `db.ps1 backup` |
| Disk | 30+ GB free | Ollama models alone are ~20 GB |
| RAM | 16+ GB | qwen2.5:14b needs ~10 GB; 32b needs ~22 GB |

### 2.2 Host setup (one-time)

#### A. PostgreSQL with pgvector

If you already have PostgreSQL 16 installed on Windows (e.g. via the EnterpriseDB installer), skip to step A.3.

**A.1 Install PostgreSQL 16** -- download from <https://www.postgresql.org/download/windows/>. During install:
- Set a password for the `postgres` superuser (you'll need it).
- Default port `5432` is fine.
- Default locale is fine.

**A.2 Install pgvector** -- pre-built Windows binaries are at <https://github.com/pgvector/pgvector-windows/releases>. Download the `.zip` matching your PG version, then:
- Copy `vector.dll` to `C:\Program Files\PostgreSQL\16\lib\`.
- Copy `vector.control` and `vector--*.sql` to `C:\Program Files\PostgreSQL\16\share\extension\`.
- Restart Postgres (admin PowerShell): `Get-Service -Name "postgresql*" | Restart-Service`.

**A.3 Create the database and user** -- in `psql -U postgres`:

```sql
CREATE USER leasegenie WITH PASSWORD '<your-strong-password>';
CREATE DATABASE leasegenie OWNER leasegenie;
\c leasegenie
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
\q
```

#### B. Redis

Install via Memurai (<https://www.memurai.com/>) on Windows, or use the unofficial Microsoft Redis port. Default `localhost:6379` is what the project expects. Confirm with:

```powershell
Test-NetConnection -ComputerName localhost -Port 6379
```

#### C. Ollama

**C.1 Install Ollama** -- <https://ollama.com/download/windows>. After install, the Ollama tray app starts automatically and listens on `127.0.0.1:11434`. The default binding is what the project expects -- no environment variables required.

**C.2 Pull the required models** -- from any PowerShell:

```powershell
ollama pull qwen2.5:14b-instruct-q5_K_M    # ~10 GB, fits on a 16 GB laptop
# OR
ollama pull qwen2.5:32b-instruct-q5_K_M    # ~22 GB, recommended for workstations

ollama pull nomic-embed-text                # ~270 MB, embedder
```

Confirm:

```powershell
Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing | Select-Object -ExpandProperty Content
```

### 2.3 Application setup

```powershell
cd C:\path\to\leasegenie_backend_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env -- set DATABASE_URL with your Postgres user/pass; pick OLLAMA_MODEL.
.\scripts\db.ps1 init       # creates extensions + tables (idempotent)
```

### 2.4 First start

```powershell
.\scripts\start.ps1
# Smoke test
Invoke-WebRequest http://localhost:8000/health -UseBasicParsing
# Open swagger
Start-Process http://localhost:8000/docs
```

`scripts\start.ps1` checks that Postgres / Redis / Ollama are reachable on `localhost`, then launches uvicorn and the Celery worker in background mode (PID files in `.\run\`, logs in `.\logs\`). For live console output during dev:

```powershell
.\scripts\start.ps1 -Foreground
```

---

## 3. Day-2 operations (dev / single-host)

```powershell
# Code changes only
.\scripts\restart.ps1

# Code + new requirements
.\scripts\restart.ps1 -Full

# Stop everything
.\scripts\stop.ps1

# Inspect logs
Get-Content .\logs\api.log -Tail 40 -Wait
Get-Content .\logs\worker.log -Tail 40 -Wait

# Database
.\scripts\db.ps1 status
.\scripts\db.ps1 shell      # psql REPL
.\scripts\db.ps1 backup     # pg_dump to .\backups\
.\scripts\db.ps1 check      # ORM-vs-DB drift check
```

The lifecycle scripts share a `.lifecycle.lock` file so two of them never run at the same time. If a script is forcibly killed mid-run, the next invocation will warn about the stale lock and clean it up.

---

## 4. Production: Windows Services

For unattended operation (auto-start on boot, auto-restart on crash) register the API and worker as Windows Services:

```powershell
# elevated PowerShell, from the repo root
.\deploy\windows\install-services.ps1 -FetchNSSM
```

This creates two services:

| Service | Runs |
|---|---|
| `leasegenie-api` | `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| `leasegenie-worker` | `.\.venv\Scripts\python.exe -m celery -A app.workers.celery_app:celery_app worker --loglevel=info --concurrency=2 -P solo` |

Both auto-start on boot, restart 5 s after any crash, and read environment from `.\.env`. The worker depends on the API, so Windows starts the API first.

`-UseSC` switches to the built-in `sc.exe` (no third-party binary, but no graceful shutdown -- Celery may abandon in-flight tasks). NSSM is recommended.

Verify after install:

```powershell
Get-Service leasegenie-api, leasegenie-worker
Get-Content .\logs\api.log -Tail 40
Invoke-WebRequest http://localhost:8000/health -UseBasicParsing
```

To uninstall: `.\deploy\windows\uninstall-services.ps1` (also elevated). Postgres / Redis / Ollama services are not touched.

Do not run the dev `scripts\start.ps1` while the Windows Services are up -- they'd both try to bind port 8000.

---

## 5. Configuration reference

All settings live in `.env`. Pydantic-settings loads them at process start. Key entries:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | (none -- must set) | `postgresql+psycopg2://USER:PASS@localhost:5432/leasegenie`. URL-encode special chars. |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker. |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/1` | Separate Redis DB index. |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Set `true` only for tests / dev runs without a worker. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | |
| `OLLAMA_MODEL` | `qwen2.5:14b-instruct-q5_K_M` | Chat model -- must support JSON mode. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | 768-dim. |
| `OLLAMA_EMBED_DIM` | `768` | Must match `VECTOR(N)` in `schema.sql`. |
| `EXTRACTOR_BACKEND` | `ollama` | Use `stub` for offline tests. |
| `UPLOAD_DIR` | `./uploads` | Relative to repo root. |
| `EXPORT_DIR` | `./exports` | Relative to repo root. |
| `MAX_UPLOAD_SIZE_MB` | `100` | Per file. |
| `MAX_UPLOAD_TOTAL_MB` | `500` | Across all files in one request. |
| `MAX_PDFS_PER_REQUEST` | `8` | 1 base + 7 amendments. |
| `DEBUG` | `false` | Verbose logs and stack traces. Never `true` in production. |
| `API_PORT` | `8000` | |
| `CORS_ALLOW_ORIGINS` | `["http://localhost:3000"]` | JSON list. |

---

## 6. Troubleshooting

**Postgres not reachable on `localhost:5432`** -- confirm the Windows Postgres service is running: `Get-Service postgresql*`. Start it: `Start-Service postgresql-x64-16`.

**`vector` extension does not exist** -- pgvector binaries weren't copied to the right `lib\` and `share\extension\` paths, or Postgres wasn't restarted afterwards. Re-do step 2.2 A.2.

**Connection refused to Postgres but service is running** -- check that nothing else is bound to 5432: `Get-NetTCPConnection -LocalPort 5432`. If a stale `postgres.exe` is hanging on the port, restart the service.

**`scripts\start.ps1` says "api already running"** -- there's a stale `.\run\api.pid` file pointing at a still-alive process. `.\scripts\stop.ps1 -Force` to clear it. If that fails, `taskkill /PID <pid> /T /F` and `Remove-Item .\run\api.pid`.

**Ollama 404 / connection refused** -- Ollama isn't running. Launch it from the Start menu or run `ollama serve` from a PowerShell. `Invoke-WebRequest http://localhost:11434/api/tags` should return JSON.

**`/health` returns 200 but `/api/v1/extract/pdf` returns 500** -- likely an Ollama model issue. Check `.\logs\api.log` for `model not found`; pull it with `ollama pull <model>`.

**Celery worker ignores tasks** -- check `.\logs\worker.log` and confirm Redis is reachable: `redis-cli -h localhost ping`.

**Windows Service won't start** -- check the Event Viewer (Application log, source `nssm` or `Service Control Manager`). Common causes: `.env` missing, venv missing, port already bound. `.\.venv\Scripts\python.exe -m uvicorn app.main:app` from a manual PowerShell will surface the real error.

---

## 7. Backups

Local `pg_dump`:

```powershell
.\scripts\db.ps1 backup     # writes .\backups\leasegenie_<timestamp>.sql
```

For production, prefer `pg_basebackup` + WAL archiving, or schedule `pg_dump` via Windows Task Scheduler:

```powershell
# Task Scheduler -- daily at 02:00
Register-ScheduledTask -TaskName 'leasegenie-backup' `
    -Action (New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\path\to\repo\scripts\db.ps1 backup') `
    -Trigger (New-ScheduledTaskTrigger -Daily -At '02:00') `
    -RunLevel Highest
```

The dumps are SQL -- restore with `psql -U leasegenie -d leasegenie -f <file.sql>`.

---

## 8. Rollback procedure

If a release breaks production, revert in this order:

```powershell
# 1. Stop the services so the broken code stops handling traffic.
Stop-Service leasegenie-worker, leasegenie-api -Force

# 2. (Optional) snapshot the DB before rolling back, so you can compare or
#    forward-fix if the new release wrote data the old code can't read.
.\scripts\db.ps1 backup     # writes .\backups\leasegenie_<timestamp>.sql

# 3. Check out the previous good commit. Use the merge commit SHA from
#    `git log origin/main` -- pick the one BEFORE the broken release.
git fetch origin main
git checkout <good-commit-sha>

# 4. Re-install dependencies in case requirements.txt changed between
#    the good and broken release.
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 5. (If the broken release added a schema migration) revert the schema.
#    For additive changes (`ADD COLUMN`, new tables) the OLD code ignores
#    the extra columns and works fine, so you can skip this. For
#    destructive migrations, restore from the pg_dump from step 2:
#    psql -U leasegenie -d leasegenie -f .\backups\leasegenie_<timestamp>.sql

# 6. Restart the services.
Start-Service leasegenie-api
Start-Service leasegenie-worker

# 7. Confirm.
Invoke-WebRequest http://localhost:8000/readiness -UseBasicParsing
Get-Content .\logs\api.log -Tail 40
```

**Recommended discipline.** Tag every production release: `git tag -a v3.0.1 -m "..."` before `Start-Service`. Then rollbacks are `git checkout v3.0.0` instead of hunting commit SHAs. Push tags to the remote: `git push --tags`.

**What rolling back does NOT undo:**
- Files already deleted by the daily retention beat task.
- Audit-log rows written by the new release (they're append-only).
- Vector-store rows from extractions run during the broken window — the orphan-cleanup beat task handles these.

---

## 9. Migrating from v2.x (Docker hybrid) to v3.0 (Windows-native)

```powershell
# 1. Tear down the old containers (data lives in host Postgres -- nothing to migrate)
docker compose down -v

# 2. (Optional) free disk
docker system prune -af

# 3. Bring up the venv and start
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 4. Update .env -- replace any host.docker.internal with localhost
(Get-Content .env) -replace 'host\.docker\.internal','localhost' | Set-Content .env

# 5. Start
.\scripts\start.ps1
```

If you were running Postgres on the host already (the v2.2 hybrid setup), it stays where it is -- no schema changes are needed for v3.0.

---

## 10. What this guide does NOT cover

- High-availability multi-host topologies (use a managed Postgres + Redis cluster + a load balancer in front of multiple Windows hosts).
- Linux deployment (the lifecycle scripts and Windows Services are Windows-only by design).
- Secrets management beyond `.env` (use Windows Credential Manager or an enterprise secrets store; do not commit `.env`).
- Log shipping to SIEM (NSSM rotates locally; integrate with Filebeat / FluentBit for off-host shipping).
