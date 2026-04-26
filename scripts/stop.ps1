<#
.SYNOPSIS
    Stop the LeaseGenie API and Celery worker (native processes).

.DESCRIPTION
    Sends a graceful close to the worker first (so any in-flight extraction
    can finish), then to the API. Falls back to Stop-Process -Force after
    the per-service timeout. Does NOT touch your local Postgres, Redis, or
    Ollama installs.

    -Purge   After stopping, also delete .\run and .\logs.
    -Force   Skip the graceful close window — kill immediately after 5s.

.EXAMPLE
    .\scripts\stop.ps1
.EXAMPLE
    .\scripts\stop.ps1 -Purge
#>
[CmdletBinding()]
param(
    [switch]$Purge,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

$workerGrace = if ($Force) { 5 } else { 60 }
$apiGrace    = if ($Force) { 5 } else { 20 }

Write-LgBanner 'LeaseGenie -- stop (native)'
Get-LifecycleLock -Action 'stop'

try {
    # Stop worker first so it can drain in-flight tasks.
    Stop-LgProcess -Name 'worker' -TimeoutSec $workerGrace
    Stop-LgProcess -Name 'api'    -TimeoutSec $apiGrace

    if ($Purge) {
        Write-LgWarn 'Removing .\run and .\logs ...'
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path (Get-Location) 'run')
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path (Get-Location) 'logs')
        Write-LgOk 'Run / log dirs removed'
    }

    Write-LgBanner 'Stopped'
    Write-LgOk "LeaseGenie processes are down. Restart with: .\scripts\start.ps1"
    Write-LgInfo 'Note: your local Postgres, Redis, and Ollama remain running.'

} finally {
    Remove-LifecycleLock
}
