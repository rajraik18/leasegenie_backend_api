<#
.SYNOPSIS
    Tail logs from one or all LeaseGenie services.

.PARAMETER Service
    Service name (postgres, redis, ollama, api, worker). Omit for all.

.PARAMETER Tail
    Number of lines to show before tailing live (default 100).

.PARAMETER NoFollow
    Print last N lines and exit; don't follow.

.EXAMPLE
    .\scripts\logs.ps1
    All services, follow live.

.EXAMPLE
    .\scripts\logs.ps1 api
    Just api, follow.

.EXAMPLE
    .\scripts\logs.ps1 worker -Tail 500
    Last 500 lines of worker logs, then follow.

.EXAMPLE
    .\scripts\logs.ps1 -NoFollow
    Last 100 lines of all services, exit.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Service = '',

    [Parameter(Position = 1)]
    [int]$Tail = 100,

    [switch]$NoFollow
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

if (-not (Test-DockerInstalled)) {
    Stop-WithError 'Docker is not installed or not in PATH'
}
$null = Get-ComposeCommand

if ($Service -and $Service -notin $AllServices) {
    Stop-WithError "Unknown service '$Service'. Valid: $($AllServices -join ', ')"
}

$composeArgs = @('logs', "--tail=$Tail")
if (-not $NoFollow) { $composeArgs += '-f' }
if ($Service) { $composeArgs += $Service }

Invoke-Compose @composeArgs
