# LeaseGenie Lifecycle Scripts

Four PowerShell scripts for running the API and Celery worker as native Windows processes. There is no Docker dependency — Postgres, Redis, and Ollama run as host-installed services.

| Script | Purpose | Typical wall-clock time |
|---|---|---|
| `start.ps1` | Launch api + worker | 5-15 s |
| `stop.ps1` | Stop api + worker gracefully | 5-90 s |
| `restart.ps1` | Stop, then start | 10-30 s |
| `db.ps1` | Database management (init / status / backup / sql / shell) | 1-10 s |

All scripts dot-source `_Common.ps1`, which provides logging, the lifecycle lock, the `Start-LgProcess` / `Stop-LgProcess` / `Test-LgProcessAlive` / `Test-PortListening` primitives, `.env` parsing, and venv resolution. Scripts are safe to run from any directory — they auto-resolve to the project root.

For database-specific operations, see `scripts/db/README.md`. For production deployment as Windows Services, see `deploy/windows/README.md`.

---

## Quick reference

```powershell
# === First-time setup ===
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env             # edit DATABASE_URL etc.
.\scripts\db.ps1 init                   # creates extensions + tables
.\scripts\db.ps1 seed                   # optional demo data

# === Daily development ===
.\scripts\start.ps1                     # default: background + log file
.\scripts\start.ps1 -Foreground         # opens 2 new console windows
.\scripts\restart.ps1                   # stop + start
.\scripts\restart.ps1 -Full             # pip install before restart
.\scripts\stop.ps1                      # graceful stop
.\scripts\stop.ps1 -Purge               # also wipe .\run and .\logs

# === Inspection ===
Get-Content .\logs\api.log -Tail 40 -Wait    # tail API log
Get-Content .\logs\worker.log -Tail 40 -Wait
Get-Process -Id (Get-Content .\run\api.pid)  # confirm api is alive

# === Database ===
.\scripts\db.ps1 init
.\scripts\db.ps1 status
.\scripts\db.ps1 shell                  # psql REPL
.\scripts\db.ps1 backup                 # pg_dump to .\backups\
```

---

## Two run modes

`start.ps1` and `restart.ps1` accept `-Foreground` to switch from the default background mode. The trade-off:

| | Background (default) | Foreground (`-Foreground`) |
|---|---|---|
| Where output goes | `.\logs\api.log`, `.\logs\worker.log` | A new console window per process |
| Window | Hidden | Visible (with title `leasegenie-api` / `leasegenie-worker`) |
| `stop.ps1` works? | Yes | Yes (it kills the spawned `powershell.exe` host) |
| Best for | Headless dev, scripted CI | Live debugging, demos |

Either way, `Start-LgProcess` writes a PID file under `.\run\` so `stop.ps1` knows what to terminate.

---

## When to use which script

### `start.ps1`

| Situation | Command |
|---|---|
| Daily startup | `.\scripts\start.ps1` |
| Need to see live output | `.\scripts\start.ps1 -Foreground` |
| After `git pull` with new requirements | `.\scripts\start.ps1 -Build` |
| Host infra still warming up, want to skip checks | `.\scripts\start.ps1 -SkipChecks` |

`-Build` runs `pip install -r requirements.txt` against the venv before launching anything.

### `restart.ps1`

| Situation | Command |
|---|---|
| Python code only | `.\scripts\restart.ps1` |
| Python code + `requirements.txt` | `.\scripts\restart.ps1 -Full` |
| Want live output after restart | `.\scripts\restart.ps1 -Foreground` |

### `stop.ps1`

| Situation | Command |
|---|---|
| End of dev session | `.\scripts\stop.ps1` |
| Reset state, wipe logs and PID files | `.\scripts\stop.ps1 -Purge` |
| Process is hung | `.\scripts\stop.ps1 -Force` |

Stop ordering is **deliberate** — the worker is asked first (so any in-flight extraction can finish), then the API. Default grace is 60 s for the worker and 20 s for the API; `-Force` collapses both to 5 s and goes straight to `Stop-Process -Force`.

---

## How the scripts handle failures

- **Postgres / Redis / Ollama unreachable** — `start.ps1` aborts with the offending endpoint and exits 1. Add `-SkipChecks` to bypass.
- **Concurrent invocation** — a `.lifecycle.lock` file prevents two scripts running at once. Stale-PID-aware: if the holding script crashed, the next invocation cleans it up.
- **API slow to become healthy** — `start.ps1` polls `http://localhost:<API_PORT>/health` for up to 60 s. On timeout it warns (exit 0) and points you at the log file. `restart.ps1` returns exit 2 in the same situation so CI can treat it as a soft failure.
- **PID file points at a dead process** — `Stop-LgProcess` notices, removes the PID file, and returns success.

---

## Production

These scripts are dev / single-host tools. For production-grade auto-start and auto-restart, register the API and worker as Windows Services using `deploy\windows\install-services.ps1`. The dev scripts and the Windows Services don't fight each other, but don't run both at once on the same port — stop one before starting the other.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Hard failure (missing prerequisite, infra unreachable, lock held) |
| 2 | Soft failure (started but `/health` did not pass — inspect `.\logs\api.log`) |
