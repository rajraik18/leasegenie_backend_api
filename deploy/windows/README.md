# LeaseGenie — Windows Services

Production deployment on Windows. Registers the API (`uvicorn`) and Celery worker as Windows Services so they auto-start on boot and auto-restart if they crash.

For dev / single-host usage you do **not** need these — the lifecycle scripts in `..\..\scripts\` (`start.ps1` / `restart.ps1` / `stop.ps1`) are simpler and don't touch the Service Control Manager.

## Prerequisites

- PostgreSQL 16+ with pgvector, Redis 7+, and Ollama already installed and running on the host as services.
- Project virtual environment at `.venv\` from the repo root, with `pip install -r requirements.txt` complete.
- `.env` filled in (copy from `.env.example`).
- An **elevated PowerShell prompt** (Run as Administrator).

## Install

```powershell
# default — uses NSSM (downloads it the first time if -FetchNSSM is passed)
.\deploy\windows\install-services.ps1 -FetchNSSM

# alternative — uses the built-in sc.exe (no third-party binary)
.\deploy\windows\install-services.ps1 -UseSC
```

Two services get registered:

| Service | What it runs |
|---|---|
| `leasegenie-api` | `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| `leasegenie-worker` | `.\.venv\Scripts\python.exe -m celery -A app.workers.celery_app:celery_app worker --loglevel=info --concurrency=2 -P solo` |

Both start automatically on boot, restart 5 s after any crash, and read environment from your repo `.env`. The worker depends on the API, so Windows starts the API first.

## Verify

```powershell
Get-Service leasegenie-api, leasegenie-worker
Get-Content .\logs\api.log -Tail 40
Get-Content .\logs\worker.log -Tail 40
Invoke-WebRequest http://localhost:8000/health -UseBasicParsing
```

## Day-2 operations

```powershell
# Stop / start / restart manually
Stop-Service leasegenie-worker, leasegenie-api
Start-Service leasegenie-api
Start-Service leasegenie-worker
Restart-Service leasegenie-api

# Code changes — pip install + bounce the services
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Restart-Service leasegenie-api, leasegenie-worker
```

## Run as a dedicated account

By default both services run as `LocalSystem`. To run under a service account:

```powershell
.\deploy\windows\install-services.ps1 -ServiceUser '.\leasegenie' -FetchNSSM
# the script will prompt for the password (SecureString)
```

The account needs **Log on as a service** rights (`secpol.msc` → Local Policies → User Rights Assignment) and read+write access to the repo root.

## Uninstall

```powershell
.\deploy\windows\uninstall-services.ps1
```

Stops and removes both services. Does **not** touch your local Postgres / Redis / Ollama — they keep running.

## NSSM vs sc.exe

| | NSSM (default) | sc.exe |
|---|---|---|
| Graceful shutdown | CTRL_BREAK_EVENT, 30 s grace | TerminateProcess (hard kill) |
| stdout / stderr logging | Built-in file rotation (10 MB) | Wrapped in `cmd.exe /c … >> log` |
| Auto-restart | Built-in `AppExit Default Restart` | `sc.exe failure restart/5000` |
| Third-party binary | Yes (~1 MB, MIT) | No |

NSSM is recommended for production because Celery handles CTRL_BREAK_EVENT cleanly (warm shutdown finishes in-flight tasks). Pick `-UseSC` only if your environment forbids third-party binaries.
