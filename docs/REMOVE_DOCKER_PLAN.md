# Plan — Remove Docker From the Project

## Context

Today the repo runs in **hybrid Docker mode**: the API and Celery worker run inside containers built from the `Dockerfile`, while Postgres/pgvector, Redis, and Ollama run as host-installed services that the containers reach via `host.docker.internal`. The user wants to drop Docker entirely so every component — including the API and the worker — runs as a native process on the host.

The architecture work is already 80 % done because Postgres/Redis/Ollama are already on the host. The remaining work is:
1. Delete the container artefacts.
2. Rewrite the lifecycle scripts (`start`/`stop`/`restart`/`status`/`logs`/`db`/`daily_run`) to manage native `uvicorn` + `celery` processes instead of compose services.
3. Strip `host.docker.internal` references from `.env.example`, configs, and docs (services are now reached at plain `localhost`).
4. Provide a process-management story for production (systemd unit files for Linux, NSSM/Task Scheduler hint for Windows).

The end state: `python -m venv .venv && pip install -r requirements.txt && scripts/start.sh` brings the system up; `scripts/stop.sh` brings it down. No Docker daemon required anywhere.

## Scope inventory

### A. Files to delete

| Path | Why |
|---|---|
| `Dockerfile` | API image no longer built |
| `docker-compose.yml` | No services to compose |
| `.dockerignore` | Only used by `docker build` |

### B. Scripts to rewrite (drop the compose wrapper, drive native processes)

All of `scripts/*.sh` and `scripts/*.ps1` currently call `${COMPOSE} up/down/exec/logs`. They become:

| Script | New behaviour |
|---|---|
| `scripts/_common.sh` / `_Common.ps1` | Remove `detect_compose`, `wait_for_health` (compose-based), `ensure_ollama_model` (compose `exec`), `graceful_stop`. Replace with: `start_bg <name> <cmd>` (writes PID file under `./run/<name>.pid`, redirects stdout/stderr to `./logs/<name>.log`), `stop_bg <name>` (SIGTERM + grace + SIGKILL), `pid_alive <name>`, `port_listening <port>`. Keep `acquire_lock`, the colour helpers, the `.env` boot-strap, and the URL-parsing helpers. |
| `scripts/start.sh` / `start.ps1` | Pre-flight check Postgres / Redis / Ollama on `localhost` (already there). Activate `./.venv`. `start_bg api uvicorn app.main:app --host 0.0.0.0 --port 8000`. `start_bg worker celery -A app.workers.celery_app:celery_app worker --loglevel=info --concurrency=2`. Tail `./logs/api.log` until `/health` responds. |
| `scripts/stop.sh` / `stop.ps1` | `stop_bg worker` then `stop_bg api`. `--down` flag becomes meaningless (no containers/volumes); keep `--purge` to delete `./logs` and `./run`. |
| `scripts/restart.sh` / `restart.ps1` | `--rolling` = `stop_bg` + `start_bg` per service. `--full` = same plus `pip install -r requirements.txt` first (replaces `compose build`). |
| `scripts/status.sh` / `status.ps1` | Print PIDs from `./run`, listening-port checks, `/health` curl, host Postgres/Redis/Ollama reachability. |
| `scripts/logs.sh` / `logs.ps1` | `tail -F ./logs/<service>.log`. `--follow`, `--tail=N`, `<service>` args preserved. |
| `scripts/db.sh` / `db.ps1` | Already mostly host-native. Drop the `${COMPOSE} exec api python -m scripts.db.manage` fallback — call `python -m scripts.db.manage` directly (the venv is already active). Keep `psql`/`pg_dump` paths. |
| `scripts/daily_run.sh` / `daily_run.ps1` | Replace `${COMPOSE} stop -t 30 api` and `${COMPOSE} up -d --no-deps` with the new `stop_bg`/`start_bg`. Replace `${COMPOSE} logs --tail=30 api` with `tail -n 30 ./logs/api.log`. |

### C. Files to update (not delete)

| Path | Change |
|---|---|
| `.env.example` | Replace every `host.docker.internal` with `localhost`. Drop the multi-paragraph "containers reach the host via..." block; replace with a short "Run all services on localhost" preamble. |
| `app/db/session.py:80` | Comment mentions `docker-compose's mounted schema.sql` — rewrite to "tables created by `scripts/db.sh init` (which runs `scripts/db/schema.sql` via host `psql`)". |
| `app/config.py` | The `database_url` default is `postgresql+psycopg2://CHANGE_ME:CHANGE_ME@postgres:5432/leasegenie` — change `postgres` to `localhost` for consistency. |
| `README.md` | Replace the "Quick start with Docker" section with "Quick start (native)": create venv, `pip install -r requirements.txt`, set `.env`, `scripts/start.sh`. Drop the 10 docker references. |
| `DEPLOYMENT.md` | Heavy edit (31 references). Replace "Hybrid setup" → "Native setup". Add a new section for systemd unit files (see §D below). Keep the host-side prerequisite checklists for Postgres, Redis, Ollama unchanged. |
| `CHANGELOG.md` | Add a single entry under a new version line (`v3.0.0 — Docker removed`); do NOT rewrite history. |
| `scripts/README.md` | Replace docker-compose lifecycle table with the new native scripts. |
| `scripts/db/README.md` | Drop the "fall back to api container" branch in the description. |
| `scripts/db/migrations/v2_0_0_upgrade.sql` | One header comment; trim if obsolete. |

### D. Files to add

| Path | Purpose |
|---|---|
| `deploy/systemd/leasegenie-api.service` | Linux production unit: `ExecStart=/path/to/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000`, `EnvironmentFile=/path/to/.env`, `Restart=on-failure`, `User=leasegenie`. |
| `deploy/systemd/leasegenie-worker.service` | Same shape, runs the celery command. `After=leasegenie-api.service redis.service postgresql.service`. |
| `deploy/systemd/README.md` | Two-paragraph install snippet (`sudo cp *.service /etc/systemd/system/ && systemctl enable --now ...`). |
| `.gitignore` additions | `run/`, `logs/`, `.venv/` (verify; if already there, skip). |

Windows production users get a NSSM / Task Scheduler hint in `DEPLOYMENT.md` rather than separate artefacts — the dev `scripts/*.ps1` are sufficient for non-prod.

### E. Files NOT touched

- All of `app/` apart from the two surgical edits above. The application code already works without Docker (we proved this in the last verification — uvicorn ran fine against SQLite locally).
- `requirements.txt` — same Python deps, same versions.
- `tests/` — unchanged.
- `data/` — unchanged.
- `scripts/db/schema.sql`, `scripts/db/manage.py` — these were always host-side; no change.

## Process management design

Dev / single-host prod uses a tiny PID-file approach to keep the dev story (and the existing scripts) one-command:

```
./run/api.pid          # PID written by scripts/_common.sh::start_bg
./run/worker.pid
./logs/api.log         # uvicorn stdout/stderr
./logs/worker.log      # celery stdout/stderr
```

`start_bg` is roughly:
```bash
start_bg() {
    local name=$1; shift
    local pidfile="./run/${name}.pid"
    local logfile="./logs/${name}.log"
    if pid_alive "$name"; then die "${name} already running (PID $(cat "$pidfile"))"; fi
    mkdir -p ./run ./logs
    nohup "$@" >>"$logfile" 2>&1 &
    echo $! >"$pidfile"
    log "Started ${name} (PID $!) — logs: ${logfile}"
}
```

For multi-host or unattended production, the systemd units in `deploy/systemd/` are the canonical answer; the PID-file scripts are dev-only.

## Verification (after implementation)

1. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` — clean install.
2. Copy `.env.example` → `.env`, fill in real Postgres creds.
3. `scripts/db.sh init` — pgvector + tables created on host Postgres.
4. `scripts/start.sh` — both processes start; `./run/*.pid` written.
5. `curl -fsS http://localhost:8000/health` → 200 with `fields_loaded=72`, `playbooks_loaded=79`.
6. `scripts/status.sh` — both PIDs alive, ports 8000 listening, host services reachable.
7. `scripts/logs.sh api --tail=20` → recent uvicorn output.
8. End-to-end PDF extraction: `curl -F "files=@sample.pdf" http://localhost:8000/api/v1/extract/pdf` → 202 with `job_id`; poll `/jobs/{id}` until `complete`.
9. `scripts/restart.sh --rolling` → both processes restart, no dropped requests on `/health`.
10. `scripts/stop.sh` → both PIDs gone, ports free.
11. `pytest tests/ -q` — same 23 passing as before (4 still skipped due to local `_cffi_backend` env quirk).
12. Confirm no remaining matches: `grep -rIn -E 'docker|host\.docker\.internal|compose' --exclude-dir=.git --exclude=CHANGELOG.md .` returns only the new CHANGELOG entry.

## Risks & open questions

| Risk | Mitigation |
|---|---|
| Operators currently running on Docker need a migration path | Single command in `DEPLOYMENT.md`: `docker compose down -v && python -m venv .venv && pip install -r requirements.txt && scripts/start.sh`. Data is in host Postgres, so nothing to migrate. |
| Windows users without WSL: `nohup` and bash scripts don't apply | The `.ps1` siblings already exist; rewrite uses `Start-Process` + `$PID` files in the same shape. |
| Daily-run script's "stop API for 5 minutes during ETL" pattern | New `stop_bg api` then `start_bg api` is faster than `compose stop`/`up -d`, so behaviour is preserved or better. |
| `scripts/db.sh` has paths that called into the api container for ORM-aware ops (`status`, `check`, `seed`) | The container fallback was a convenience; the venv path is more direct. Drop the fallback entirely; the new flow runs `python -m scripts.db.manage` from the active venv. |
| Production resilience without Docker's restart policy | The systemd units handle this (`Restart=on-failure`, `RestartSec=5s`). |

**Open questions for the reviewer**:
1. Is Linux-only systemd acceptable for production, or do you also need an NSSM/Windows-service deliverable in `deploy/`?
2. Should the dev scripts launch the worker in the same shell (as today via compose) or open a new terminal? My plan assumes background + log file, no terminal.
3. Keep the `scripts/*.ps1` siblings, or drop PowerShell entirely now that the deploy target is bash + systemd? (Default in plan: keep them, mirroring the bash versions.)

## Suggested execution order (when approved)

1. Branch `claude/remove-docker` from `main`.
2. Add new `scripts/_common.sh` primitives + rewrite `start.sh`/`stop.sh`/`status.sh`/`logs.sh`. Leave the docker scripts in place but unused so each step stays small.
3. Rewrite `restart.sh`, `daily_run.sh`, `db.sh` to use the new primitives.
4. Mirror the changes into the `.ps1` siblings.
5. Edit `.env.example`, `app/db/session.py:80`, `app/config.py` default URL.
6. Add `deploy/systemd/*.service` + `deploy/systemd/README.md`.
7. Delete `Dockerfile`, `docker-compose.yml`, `.dockerignore`.
8. Edit `README.md`, `DEPLOYMENT.md`, `CHANGELOG.md`, `scripts/README.md`, `scripts/db/README.md`.
9. Run the full verification checklist; fix any drift.
10. Open a PR titled `Remove Docker — run everything natively`.
