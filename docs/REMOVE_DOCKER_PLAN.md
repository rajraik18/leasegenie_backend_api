# Plan — Remove Docker From the Project (Windows-native)

## Context

Today the repo runs in **hybrid Docker mode**: the API and Celery worker run inside containers built from the `Dockerfile`, while Postgres/pgvector, Redis, and Ollama run as host-installed services that the containers reach via `host.docker.internal`. The user wants to drop Docker entirely so every component — including the API and the worker — runs as a native Windows process. Postgres + pgvector, Redis, and Ollama remain on the host (already the case).

**Reviewer answers (locked in)**

1. **Production target = Windows only.** No Linux systemd unit files. Production resilience is provided by registering the API and worker as **Windows Services** (NSSM-based wrapper described below), so they auto-start on reboot and auto-restart on crash.
2. **Two run modes for the dev / single-host scripts** — both supported, default is **background + log file**:
   - `-Background` (default) — `Start-Process … -WindowStyle Hidden`, PID file in `.\run\<name>.pid`, stdout/stderr redirected to `.\logs\<name>.log`.
   - `-Foreground` — opens a **new console window per process** via `Start-Process powershell -ArgumentList …`, so you can watch live output during development. PID file still written so `stop.ps1` works.
3. **Keep `scripts/*.ps1` only.** The bash `scripts/*.sh` siblings get **deleted**. Lifecycle scope is just **start / restart / stop** (no separate logs/status/daily_run scripts in scope; existing functionality is folded into the three lifecycle scripts and `db.ps1`).

The end state on a fresh Windows host:

```powershell
# one-time
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # then edit DATABASE_URL etc.
.\scripts\db.ps1 init

# daily
.\scripts\start.ps1            # background (default)
.\scripts\start.ps1 -Foreground  # opens 2 new consoles
.\scripts\restart.ps1
.\scripts\stop.ps1

# production (one-time service registration)
.\deploy\windows\install-services.ps1   # registers leasegenie-api + leasegenie-worker
```

No Docker daemon required anywhere.

## Scope inventory

### A. Files to delete

| Path | Why |
|---|---|
| `Dockerfile` | API image no longer built |
| `docker-compose.yml` | No services to compose |
| `.dockerignore` | Only used by `docker build` |
| `scripts/_common.sh` | Bash siblings dropped — Windows only |
| `scripts/start.sh` | "" |
| `scripts/stop.sh` | "" |
| `scripts/restart.sh` | "" |
| `scripts/status.sh` | "" |
| `scripts/logs.sh` | "" |
| `scripts/daily_run.sh` | "" |
| `scripts/db.sh` | "" |

### B. PowerShell scripts to rewrite

| Script | New behaviour |
|---|---|
| `scripts/_Common.ps1` | Drop `Test-DockerInstalled`, `Get-ComposeCommand`, `Wait-ForServiceHealth` (compose-based), `Invoke-OllamaModelPull` (compose `exec`), `Stop-ComposeService`. **Add**: `Start-LgProcess -Name -Cmd -Args -Mode <Background\|Foreground>` (writes `.\run\<name>.pid`, redirects to `.\logs\<name>.log` in Background, or `Start-Process powershell -NoExit -Command …` in Foreground), `Stop-LgProcess -Name [-TimeoutSec 30]` (graceful CTRL_BREAK_EVENT first; falls back to `Stop-Process -Force`), `Test-LgProcessAlive -Name`, `Test-PortListening -Port`. Keep the colour helpers, `.env` boot-strap, `Get-DatabaseUrlParts`, and the lifecycle lock. |
| `scripts/start.ps1` | Params: `[switch]$Foreground` (else background = default), `[switch]$SkipChecks`, `[switch]$Build` (now means `pip install -r requirements.txt`). Pre-flight: Postgres / Redis / Ollama TCP-reachable on `localhost`; `.\.venv` exists; `.env` present. Then: activate venv, `Start-LgProcess -Name api -Cmd python -Args 'app','main:app','-m','uvicorn','--host','0.0.0.0','--port','8000'` (or run uvicorn from venv directly), `Start-LgProcess -Name worker -Cmd celery -Args ' -A app.workers.celery_app:celery_app worker --loglevel=info --concurrency=2'`. Tail `.\logs\api.log` until `/health` returns 200. |
| `scripts/restart.ps1` | Params: `[switch]$Full`, `[switch]$Foreground`. `-Full` runs `pip install -r requirements.txt` first. Otherwise: `Stop-LgProcess worker`, `Stop-LgProcess api`, then `Start-LgProcess` for each, in the same `-Foreground`/background mode. |
| `scripts/stop.ps1` | Params: `[switch]$Purge`. Default: `Stop-LgProcess worker`, `Stop-LgProcess api`. With `-Purge`: also delete `.\run`, `.\logs`. Drop the existing `-Down` (compose-only) flag. |
| `scripts/db.ps1` | Already mostly host-native. Drop the `docker compose exec api python -m scripts.db.manage` fallback — the active venv runs `python -m scripts.db.manage` directly. Keep `psql` / `pg_dump` paths. |

### C. Files to update (not delete)

| Path | Change |
|---|---|
| `.env.example` | Replace every `host.docker.internal` with `localhost`. Drop the multi-paragraph "containers reach the host via..." block; replace with a short "Run all services on localhost" preamble. |
| `app/db/session.py:80` | Comment mentions `docker-compose's mounted schema.sql` — rewrite to "tables created by `scripts/db.ps1 init` (which runs `scripts/db/schema.sql` via host `psql`)". |
| `app/config.py` | Change the `database_url` default host fragment from `postgres` to `localhost` — keeps the `CHANGE_ME` cred placeholders. |
| `README.md` | Replace the "Quick start with Docker" section with "Quick start (Windows native)" — venv, `pip install -r requirements.txt`, `.env`, `scripts\db.ps1 init`, `scripts\start.ps1`. Drop all 10 docker references. |
| `DEPLOYMENT.md` | Heavy edit (31 references). Replace "Hybrid setup" → "Native Windows setup". Add a new section "Production: Windows Services" pointing at `deploy/windows/`. Keep the host-side prerequisite checklists for Postgres, Redis, Ollama; add a note that they should be configured to start automatically (Windows service auto-start). |
| `CHANGELOG.md` | Add a single entry under a new version line (`v3.0.0 — Docker removed, Windows-native`); do NOT rewrite history. |
| `scripts/README.md` | Replace docker-compose lifecycle table with the three PowerShell scripts. |
| `scripts/db/README.md` | Drop the "fall back to api container" branch in the description. |
| `scripts/db/migrations/v2_0_0_upgrade.sql` | One header comment to trim if it mentions docker. |
| `.gitignore` | Add `run/`, `logs/`, `.venv/`, `.lifecycle.lock` if not already present. |

### D. Files to add

| Path | Purpose |
|---|---|
| `deploy/windows/install-services.ps1` | One-shot installer. Detects `nssm.exe` on PATH (or downloads if `-FetchNSSM`); registers `leasegenie-api` and `leasegenie-worker` services pointing at the venv `python.exe` / `uvicorn.exe` / `celery.exe`. Sets `AppDirectory` to the repo root, `AppEnvironmentExtra` from `.env`, `AppStdout` / `AppStderr` to `.\logs\<svc>.log`, `Start=SERVICE_AUTO_START`, `AppExit Default Restart`, `AppRestartDelay=5000`. Ends with `Start-Service leasegenie-api leasegenie-worker`. |
| `deploy/windows/uninstall-services.ps1` | `Stop-Service` then `nssm remove <svc> confirm` for both services. |
| `deploy/windows/README.md` | Plain-English install / uninstall / view-logs instructions; explains how the services map to the dev `start.ps1` flow. |

### E. Files NOT touched

- All of `app/` apart from the two surgical edits above. The application code already works without Docker (verified earlier — uvicorn ran cleanly under SQLite locally).
- `requirements.txt` — same Python deps, same versions.
- `tests/` — unchanged.
- `data/` — unchanged.
- `scripts/db/schema.sql`, `scripts/db/manage.py` — were always host-side; no change.

## Process management design

### Dev: PID-file scripts (Windows)

```
.\run\api.pid          # PID written by Start-LgProcess
.\run\worker.pid
.\logs\api.log         # uvicorn stdout/stderr (Background mode)
.\logs\worker.log      # celery stdout/stderr (Background mode)
.\.lifecycle.lock      # prevents concurrent start.ps1/stop.ps1
```

**Background mode (default)** — sketch:
```powershell
function Start-LgProcess {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$Cmd,
        [string[]]$Args = @(),
        [ValidateSet('Background','Foreground')] [string]$Mode = 'Background'
    )
    $pidFile = ".\run\$Name.pid"
    $logFile = ".\logs\$Name.log"
    if (Test-LgProcessAlive -Name $Name) {
        Stop-WithError "$Name already running (PID $(Get-Content $pidFile))"
    }
    New-Item -ItemType Directory -Force -Path .\run, .\logs | Out-Null

    if ($Mode -eq 'Background') {
        $p = Start-Process -FilePath $Cmd -ArgumentList $Args `
            -RedirectStandardOutput $logFile -RedirectStandardError $logFile `
            -WindowStyle Hidden -PassThru
    } else {
        # Foreground: open a new console window so the user can see live output.
        $launch = "& '$Cmd' $($Args -join ' '); Read-Host 'Press Enter to close'"
        $p = Start-Process powershell -ArgumentList '-NoExit','-NoLogo','-Command',$launch -PassThru
    }

    Set-Content -Path $pidFile -Value $p.Id
    Write-LgInfo "Started $Name (PID $($p.Id), mode $Mode) — logs: $logFile"
}
```

`Stop-LgProcess` reads the PID, sends a graceful close (`Stop-Process -Id <pid>` first, falls back to `-Force` after `-TimeoutSec`), and deletes the PID file. The Foreground console windows close on Stop because the PID we recorded is the spawned `powershell.exe`.

### Production: Windows Services

`deploy/windows/install-services.ps1` registers two services via NSSM (small, MIT-licensed wrapper that turns any process into a Windows Service):

```powershell
nssm install leasegenie-api `"$venv\Scripts\python.exe`" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
nssm set leasegenie-api AppDirectory $repoRoot
nssm set leasegenie-api AppStdout $repoRoot\logs\api.log
nssm set leasegenie-api AppStderr $repoRoot\logs\api.log
nssm set leasegenie-api AppEnvironmentExtra (Get-Content .env -Raw)
nssm set leasegenie-api Start SERVICE_AUTO_START
nssm set leasegenie-api AppExit Default Restart
nssm set leasegenie-api AppRestartDelay 5000
# … same for leasegenie-worker (Celery) with After-style dependency on -api
nssm set leasegenie-worker DependOnService leasegenie-api
Start-Service leasegenie-api, leasegenie-worker
```

Operators can also use plain `sc.exe create … binPath=…` if they don't want NSSM, but NSSM is the default because it handles graceful shutdown (CTRL_BREAK_EVENT) and stdout logging cleanly. The PowerShell installer can be re-run idempotently — it stops + reinstalls if the services already exist.

## Verification (after implementation)

Run on a clean Windows host with Postgres+pgvector / Redis / Ollama installed:

1. `python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt` — clean install.
2. `Copy-Item .env.example .env`, edit `DATABASE_URL` with real creds.
3. `.\scripts\db.ps1 init` — pgvector + tables created on host Postgres.
4. `.\scripts\start.ps1` — both processes start in background; `.\run\api.pid` + `.\run\worker.pid` written.
5. `Invoke-WebRequest -UseBasicParsing http://localhost:8000/health` → 200, `fields_loaded=72`, `playbooks_loaded=79`.
6. `Get-Content .\logs\api.log -Tail 20` — recent uvicorn output, no docker references.
7. `.\scripts\start.ps1 -Foreground` (after a stop) → two new consoles open, live output streams to each.
8. End-to-end PDF extraction: `Invoke-RestMethod -Method Post -InFile sample.pdf -Uri http://localhost:8000/api/v1/extract/pdf` → 202 with `job_id`; poll until `complete`.
9. `.\scripts\restart.ps1` → both PIDs change; `/health` returns 200 within ~10 s.
10. `.\scripts\stop.ps1` → PID files removed, port 8000 free, processes gone.
11. `.\deploy\windows\install-services.ps1`; reboot; both services auto-start; `Get-Service leasegenie-*` shows `Running`.
12. `pytest tests/ -q` from the venv — same 23 passing.
13. Confirm no remaining matches: `Select-String -Pattern 'docker|host\.docker\.internal|compose' -Path . -Recurse -Exclude .git,CHANGELOG.md` returns only the new CHANGELOG entry.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Operators currently on Docker need a migration path | One-line in `DEPLOYMENT.md`: `docker compose down -v ; python -m venv .venv ; pip install -r requirements.txt ; .\scripts\start.ps1`. Data lives in host Postgres so nothing to migrate. |
| Foreground mode leaves orphan console windows if the user closes the parent shell | The PID file points at the spawned `powershell.exe`; `stop.ps1` kills it cleanly. We also document `taskkill /PID <pid> /T /F` as a manual fallback. |
| `daily_run.sh` ETL pattern (stop API, run ETL, restart) is dropped | Reviewer scope says only start/restart/stop. The same effect is `.\scripts\stop.ps1; <etl>; .\scripts\start.ps1`. If ETL automation is still needed, add a separate `daily_run.ps1` in a follow-up — out of scope for this PR. |
| NSSM is a third-party binary | Two options in the installer: `-UseNSSM` (default; pulls the MIT-licensed binary into `deploy\windows\bin\` if missing) or `-UseSC` (uses built-in `sc.exe` only — fewer features but no third-party dep). Operators choose. |
| Windows graceful shutdown for celery workers | NSSM sends CTRL_BREAK_EVENT by default, which celery handles correctly (warm shutdown, finishes in-flight tasks within `AppStopMethodConsole` timeout, default 1500 ms — bumped to 30 s in the installer). |

## Suggested execution order

1. Branch `claude/remove-docker` from `main`.
2. Add new `scripts/_Common.ps1` primitives (`Start-LgProcess`, `Stop-LgProcess`, `Test-LgProcessAlive`, `Test-PortListening`).
3. Rewrite `start.ps1`, `restart.ps1`, `stop.ps1` against the new primitives. Verify locally on a Windows host.
4. Update `db.ps1` to drop the `docker compose exec` fallback.
5. Edit `.env.example`, `app/db/session.py:80`, `app/config.py` default URL.
6. Add `deploy/windows/install-services.ps1`, `uninstall-services.ps1`, `README.md`.
7. Delete `Dockerfile`, `docker-compose.yml`, `.dockerignore`.
8. Delete `scripts/*.sh` and `scripts/_common.sh`.
9. Edit `README.md`, `DEPLOYMENT.md`, `CHANGELOG.md`, `scripts/README.md`, `scripts/db/README.md`.
10. Run the verification checklist on a Windows host; fix any drift.
11. Open a PR titled `Remove Docker — Windows-native deployment`.
