<#
.SYNOPSIS
    Restart the LeaseGenie API and Celery worker (native processes).

.DESCRIPTION
    Stops both processes, then starts them again. Postgres, Redis, and
    Ollama stay up — they run on the host as native services.

    -Foreground   Restart in foreground mode (new console per process).
                  Default is background + log file.
    -Full         Run `pip install -r requirements.txt` before starting,
                  e.g. after editing requirements.

.EXAMPLE
    .\scripts\restart.ps1
.EXAMPLE
    .\scripts\restart.ps1 -Full
.EXAMPLE
    .\scripts\restart.ps1 -Foreground
#>
[CmdletBinding()]
param(
    [switch]$Foreground,
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

$mode = if ($Foreground) { 'Foreground' } else { 'Background' }
Write-LgBanner "LeaseGenie -- restart ($mode mode, native)"
Test-EnvFile
Get-LifecycleLock -Action 'restart'

try {
    # ---- Stop ----
    Write-LgBanner 'Stopping current processes'
    Stop-LgProcess -Name 'worker' -TimeoutSec 60
    Stop-LgProcess -Name 'api'    -TimeoutSec 20

    # ---- Optional dependency reinstall ----
    if ($Full) {
        Write-LgBanner 'pip install -r requirements.txt'
        $py = Get-VenvPython
        & $py -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { Stop-WithError 'pip install failed' }
        Write-LgOk 'Dependencies installed'
    }

    if (-not (Test-VenvReady)) {
        Stop-WithError "Virtual environment not found at .\.venv. Run: python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt"
    }

    # ---- Start ----
    Write-LgBanner 'Launching api + worker'
    $py = Get-VenvPython
    $apiPort = Get-EnvValue -Key 'API_PORT' -Default '8000'
    $apiHost = Get-EnvValue -Key 'API_HOST' -Default '127.0.0.1'
    $workerPool        = Get-EnvValue -Key 'WORKER_POOL'        -Default 'threads'
    $workerConcurrency = Get-EnvValue -Key 'WORKER_CONCURRENCY' -Default '4'

    $apiArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $apiHost, '--port', $apiPort)
    Start-LgProcess -Name 'api' -FilePath $py -ArgumentList $apiArgs -Mode $mode | Out-Null

    $workerArgs = @('-m', 'celery', '-A', 'app.workers.celery_app:celery_app', 'worker', '--loglevel=info', "--concurrency=$workerConcurrency", '-P', $workerPool)
    Start-LgProcess -Name 'worker' -FilePath $py -ArgumentList $workerArgs -Mode $mode | Out-Null

    # ---- Smoke ----
    Write-LgBanner 'Smoke test'
    $healthUrl = "http://localhost:$apiPort/health"
    if (Wait-ForHttpHealth -Url $healthUrl -TimeoutSec 60) {
        Write-LgBanner 'Restarted'
        Write-LgOk "API healthy at $healthUrl"
    } else {
        Write-LgBanner 'Restarted (with warnings)'
        Write-LgWarn 'API did not pass health check -- inspect .\logs\api.log'
        exit 2
    }
} finally {
    Remove-LifecycleLock
}
