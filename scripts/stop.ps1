<#
.SYNOPSIS
    Stop the LeaseGenie stack gracefully (HYBRID deployment).

.DESCRIPTION
    Only api+worker run in containers; this script does NOT touch your
    local Postgres, Redis, or Ollama installs.

    Order: drain api first (stop accepting requests), then worker (let it
    finish in-flight extractions).

    Modes:
        (default)  Graceful stop. Containers preserved. Volumes preserved.
        -Down      Remove containers, keep api_data volume.
        -Clean     Remove containers AND api_data volume (uploaded PDFs lost).
        -Force     Skip graceful drain - SIGKILL after 5s.

.EXAMPLE
    .\scripts\stop.ps1
    Graceful stop, preserves data.

.EXAMPLE
    .\scripts\stop.ps1 -Clean
    Stop, remove containers, delete api_data volume (asks confirmation).
#>
[CmdletBinding()]
param(
    [switch]$Down,
    [switch]$Clean,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

$doDown = $Down -or $Clean
$graceApi = if ($Force) { 5 } else { 20 }
$graceWorker = if ($Force) { 5 } else { 60 }

Write-LgBanner 'LeaseGenie API -- stop (hybrid deployment)'

if (-not (Test-DockerInstalled)) {
    Stop-WithError 'Docker is not installed or not in PATH'
}
$null = Get-ComposeCommand
Get-LifecycleLock -Action 'stop'

try {
    Write-LgInfo 'Current state:'
    Invoke-Compose ps | Out-Null
    Write-Host ''

    if ($Clean) {
        Write-LgWarn '============================================================='
        Write-LgWarn '  -Clean will DELETE the api_data volume:'
        Write-LgWarn '    - uploaded PDFs in /data/uploads'
        Write-LgWarn '    - generated Excel exports in /data/exports'
        Write-LgWarn ''
        Write-LgWarn '  Your local Postgres, Redis, and Ollama data are NOT touched.'
        Write-LgWarn '============================================================='
        $confirm = Read-Host "Type 'YES I AM SURE' to proceed"
        if ($confirm -ne 'YES I AM SURE') { Stop-WithError 'Aborted by user' }
    }

    # Phase 1: Drain API
    Write-LgBanner 'Phase 1 -- draining API (stop accepting new requests)'
    Stop-ServiceGracefully -Service 'api' -TimeoutSec $graceApi
    Write-LgOk 'API drained'

    # Phase 2: Stop workers
    Write-LgBanner 'Phase 2 -- stopping worker'
    Stop-ServiceGracefully -Service 'worker' -TimeoutSec $graceWorker
    Write-LgOk 'Worker stopped'

    # Optional: remove containers
    if ($doDown) {
        Write-LgBanner 'Removing containers'
        if ($Clean) {
            Invoke-Compose down -v --remove-orphans | Out-Null
            Write-LgOk 'Containers and api_data volume removed'
        } else {
            Invoke-Compose down --remove-orphans | Out-Null
            Write-LgOk 'Containers removed (api_data volume preserved)'
        }
    }

    Write-LgBanner 'Stopped'
    if ($Clean) {
        Write-LgOk 'Containers and api_data volume cleaned. Local infra untouched.'
    } elseif ($doDown) {
        Write-LgOk 'Containers removed; api_data volume preserved.'
    } else {
        Write-LgOk "Containers stopped (preserved). Restart with '.\scripts\start.ps1'."
    }
    Write-LgOk 'Note: your local Postgres, Redis, and Ollama remain running.'

} finally {
    Remove-LifecycleLock
}
