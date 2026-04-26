<#
.SYNOPSIS
    Start the LeaseGenie API and Celery worker as native processes.

.DESCRIPTION
    Launches uvicorn (API) and the Celery worker from the project's virtual
    environment. Postgres, Redis, and Ollama are expected to be running on
    the host already; this script verifies them but does not start them.

    Run modes:
        (default)     Background. Hidden window per process; stdout/stderr
                      go to .\logs\api.log and .\logs\worker.log; PIDs to
                      .\run\api.pid and .\run\worker.pid.
        -Foreground   Open a new console window per process. Live output is
                      visible. PID files still written so stop.ps1 works.

    Other switches:
        -SkipChecks   Skip the Postgres/Redis/Ollama reachability checks.
        -Build        Run `pip install -r requirements.txt` before starting
                      (use after pulling code or editing requirements).

.EXAMPLE
    .\scripts\start.ps1
    Background start (default).

.EXAMPLE
    .\scripts\start.ps1 -Foreground
    Open two new console windows so you can watch live output.

.EXAMPLE
    .\scripts\start.ps1 -Build
    Reinstall requirements, then background start.
#>
[CmdletBinding()]
param(
    [switch]$Foreground,
    [switch]$SkipChecks,
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

$mode = if ($Foreground) { 'Foreground' } else { 'Background' }
Write-LgBanner "LeaseGenie -- start ($mode mode, native)"

Test-EnvFile
Get-LifecycleLock -Action 'start'

try {
    # ---- Phase 1: Verify host infrastructure ----
    if (-not $SkipChecks) {
        Write-LgBanner 'Phase 1 -- verifying host infrastructure'

        $dbUrl     = Get-EnvValue -Key 'DATABASE_URL'        -Default 'postgresql+psycopg2://leasegenie:leasegenie@localhost:5432/leasegenie'
        $redisUrl  = Get-EnvValue -Key 'CELERY_BROKER_URL'   -Default 'redis://localhost:6379/0'
        $ollamaUrl = Get-EnvValue -Key 'OLLAMA_BASE_URL'     -Default 'http://localhost:11434'

        $pgHost = 'localhost'; $pgPort = 5432
        if ($dbUrl -match '@([^:/]+):(\d+)') {
            $pgHost = $matches[1]; $pgPort = [int]$matches[2]
        }
        $redisHost = 'localhost'; $redisPort = 6379
        if ($redisUrl -match 'redis://([^:/]+):(\d+)') {
            $redisHost = $matches[1]; $redisPort = [int]$matches[2]
        }

        Write-LgInfo "Checking Postgres at ${pgHost}:${pgPort} ..."
        if (Test-PortListening -HostName $pgHost -Port $pgPort) {
            Write-LgOk 'Postgres is reachable'
        } else {
            Stop-WithError "Postgres NOT reachable at ${pgHost}:${pgPort}. Start your local Postgres service."
        }

        Write-LgInfo "Checking Redis at ${redisHost}:${redisPort} ..."
        if (Test-PortListening -HostName $redisHost -Port $redisPort) {
            Write-LgOk 'Redis is reachable'
        } else {
            Stop-WithError "Redis NOT reachable at ${redisHost}:${redisPort}. Start your local Redis service."
        }

        Write-LgInfo "Checking Ollama at $ollamaUrl ..."
        $ollamaOk = $false
        try {
            $resp = Invoke-WebRequest -Uri "$ollamaUrl/api/tags" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            $ollamaOk = ($resp.StatusCode -eq 200)
        } catch {}
        if ($ollamaOk) {
            Write-LgOk 'Ollama is reachable'
        } else {
            Stop-WithError "Ollama NOT reachable at $ollamaUrl. Start the Ollama application."
        }
    }

    # ---- Phase 2: Optional dependency reinstall ----
    if ($Build) {
        Write-LgBanner 'Phase 2 -- pip install -r requirements.txt'
        $py = Get-VenvPython
        & $py -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { Stop-WithError 'pip install failed' }
        Write-LgOk 'Dependencies installed'
    }

    if (-not (Test-VenvReady)) {
        Stop-WithError "Virtual environment not found at .\.venv. Run: python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt"
    }

    # ---- Phase 3: Launch processes ----
    Write-LgBanner 'Phase 3 -- launching api + worker'
    $py = Get-VenvPython
    $apiPort = Get-EnvValue -Key 'API_PORT' -Default '8000'
    # Bind to loopback by default. A reverse proxy (IIS / Caddy / nginx)
    # terminates TLS and forwards to 127.0.0.1:$apiPort. Set API_HOST=0.0.0.0
    # in .env only if you intentionally want to expose uvicorn directly.
    $apiHost = Get-EnvValue -Key 'API_HOST' -Default '127.0.0.1'
    $workerPool        = Get-EnvValue -Key 'WORKER_POOL'        -Default 'threads'
    $workerConcurrency = Get-EnvValue -Key 'WORKER_CONCURRENCY' -Default '4'

    $apiArgs = @(
        '-m', 'uvicorn',
        'app.main:app',
        '--host', $apiHost,
        '--port', $apiPort
    )
    Start-LgProcess -Name 'api' -FilePath $py -ArgumentList $apiArgs -Mode $mode | Out-Null

    # `-B` runs the beat scheduler embedded in the worker so daily
    # cleanup tasks fire without a separate process. Safe because the
    # project deploys exactly one worker per host.
    $workerArgs = @(
        '-m', 'celery',
        '-A', 'app.workers.celery_app:celery_app',
        'worker',
        '-B',
        '--loglevel=info',
        "--concurrency=$workerConcurrency",
        '-P', $workerPool
    )
    Start-LgProcess -Name 'worker' -FilePath $py -ArgumentList $workerArgs -Mode $mode | Out-Null

    # ---- Phase 4: Smoke test ----
    Write-LgBanner 'Phase 4 -- smoke test'
    $healthUrl = "http://localhost:$apiPort/health"
    if (Wait-ForHttpHealth -Url $healthUrl -TimeoutSec 60) {
        Write-LgOk "Docs:   http://localhost:$apiPort/docs"
        Write-LgOk "Health: $healthUrl"
    } else {
        Write-LgWarn 'API did not respond healthy in 60s -- inspect logs:'
        Write-LgWarn "  Get-Content .\logs\api.log -Tail 40"
    }

    Write-LgBanner 'Started'
    Write-LgOk "LeaseGenie is up ($mode mode, native)"
    Write-LgInfo "Stop with: .\scripts\stop.ps1"

} finally {
    Remove-LifecycleLock
}
