# LeaseGenie Lifecycle Scripts

Seven scripts for managing the Docker Compose stack and database. Each is shipped in **two flavors** — `.sh` for bash (Linux / macOS / WSL / Git Bash) and `.ps1` for PowerShell (Windows native, no bash needed). Same behavior, same exit codes, idiomatic flags for each platform.

| Script | Purpose | Typical wall-clock time |
|---|---|---|
| `start.sh` / `start.ps1` | Bring the stack up | 30s warm / 5-30 min cold |
| `stop.sh` / `stop.ps1` | Bring the stack down gracefully | 30-90s |
| `restart.sh` / `restart.ps1` | Restart with the right strategy | 15s rolling / 60-90s full |
| `status.sh` / `status.ps1` | One-screen health view | <2s |
| `logs.sh` / `logs.ps1` | Tail logs | live |
| `db.sh` / `db.ps1` | Database management (init, status, seed, sql) | 1-10s |
| `daily_run.sh` / `daily_run.ps1` | Drain + backup + rolling restart for cron / Task Scheduler | 2-30 min |

All bash scripts source `_common.sh`; all PowerShell scripts dot-source `_Common.ps1`. Both modules provide logging, lock files, health-check polling, Compose detection. Scripts are safe to run from any directory — they auto-resolve to the project root.

For database-specific operations, see `scripts/db/README.md`.

---

## Quick reference

```bash
# === First-time setup ===
scripts/start.sh --cold           # pulls Ollama models, runs DB init
scripts/db.sh init                # idempotent — creates 8 tables if absent
scripts/db.sh seed                # optional: inserts a demo project/tenant

# === Daily development ===
scripts/start.sh                   # warm start (~30s)
scripts/restart.sh                 # rolling restart of api+worker
scripts/restart.sh --build         # after code changes
scripts/stop.sh                    # graceful stop, preserves data

# === Diagnostics ===
scripts/status.sh                  # quick health snapshot
scripts/status.sh --watch          # auto-refresh every 5s
scripts/logs.sh api                # follow API logs
scripts/logs.sh worker 500         # last 500 worker log lines

# === Specialty operations ===
scripts/start.sh --infra           # DB+Redis+Ollama only (run uvicorn locally)
scripts/restart.sh --hard          # recreate containers (after Dockerfile change)
scripts/stop.sh --down             # remove containers, keep volumes
scripts/stop.sh --clean            # NUKE EVERYTHING (asks for confirmation)
scripts/stop.sh --force            # SIGKILL after 10s grace (when hung)
```

---

## When to use which script

### `start.sh`

| Situation | Command |
|---|---|
| First time on this machine | `scripts/start.sh --cold` |
| Daily startup | `scripts/start.sh` |
| After `git pull` with code changes | `scripts/start.sh --build` |
| Local dev (uvicorn outside Docker) | `scripts/start.sh --infra` |

`--cold` triggers the Ollama model pull (`qwen2.5:32b` is ~20 GB; takes 10-30 min on first run depending on bandwidth). `--warm` (default) skips the pull and assumes the model is cached.

### `restart.sh`

The script picks the **lightest-weight** restart that actually applies your change:

| What changed | Use this |
|---|---|
| Python code only | `scripts/restart.sh` (rolling, ~15s) |
| Python code + `requirements.txt` | `scripts/restart.sh --build` (rebuild + restart, ~60s) |
| `Dockerfile` or environment vars | `scripts/restart.sh --hard` (recreate containers, ~90s) |
| `docker-compose.yml` services list | `scripts/restart.sh --full --all` |
| Confused / unsure | `scripts/stop.sh && scripts/start.sh` (clean slate, slower) |

A **rolling restart** keeps the database, Redis, and Ollama running — no model re-downloads, no DB warm-up, no Redis cache loss. Use it as your default.

### `stop.sh`

| Situation | Command |
|---|---|
| End of dev session | `scripts/stop.sh` |
| Switch to a different project, free disk | `scripts/stop.sh --down` |
| Reproducing a "fresh install" issue | `scripts/stop.sh --clean` |
| Stack is hung, won't stop normally | `scripts/stop.sh --force` |

Stop ordering is **deliberate** — API drains first (so no new requests get rejected mid-flight), then workers (so any running extraction completes), then infrastructure last (so the DB flushes cleanly). The defaults give workers 60s to finish their Celery jobs; bump it if you have long-running extractions in flight.

`--clean` is destructive: it removes the `mssql_data`, `redis_data`, `ollama_data`, and `chroma_data` volumes. After `--clean`, the next start MUST use `--cold` so models get re-pulled.

---

## How they handle failures

- **No Docker installed** — script exits with a clear message before attempting anything.
- **Concurrent invocation** — a `.lifecycle.lock` file prevents two scripts running at once. The lock is stale-PID-aware: if the holding script crashed, the next invocation cleans it up.
- **Service slow to become healthy** — `wait_for_health` polls Compose's reported health every 3s up to a per-service timeout. On timeout the script exits non-zero AND prints the last 30 log lines of the failing service.
- **Ollama model pull fails** — start script warns but continues. The API will boot; extractions will fail at request time with a clear error. You can re-run `scripts/start.sh --cold` later.
- **`mssql-init` fails** — the script logs the failure but the API still starts. You can re-run `scripts/start.sh` and it will re-attempt init.

---

## What they do NOT do

These scripts are intentionally **not** a CI/CD or production deployment tool. Specifically:

- No version pinning of Compose images (use a dedicated production override file)
- No secrets management (use Docker secrets or a vault in production)
- No rolling deploys with zero-downtime traffic shifting (use Kubernetes for that)
- No backup or restore of `mssql_data` (use `scripts/backup.sh` if you add one — recommended for production)
- No log rotation (Docker handles that with `--log-opt max-size`)

For production, treat these scripts as the local-development equivalent. The patterns they encode (graceful drain order, health polling, lock files, mode flags) translate directly to systemd units, Kubernetes lifecycle hooks, or Ansible playbooks.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Hard failure (missing prerequisite, unhealthy critical service) |
| 2 | Soft failure (started but some service not yet healthy — check logs) |

Use `scripts/start.sh && scripts/some_test.sh` confidently; CI scripts should treat exit 2 as a warning that warrants a longer health-check timeout.
