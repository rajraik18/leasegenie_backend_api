<#
.SYNOPSIS
    One-screen summary of the LeaseGenie stack (HYBRID deployment).

.DESCRIPTION
    Shows Docker containers (api, worker) plus reachability of host services
    (postgres, redis, ollama).

.PARAMETER Watch
    Refresh every 5 seconds (Ctrl-C to stop).

.EXAMPLE
    .\scripts\status.ps1
.EXAMPLE
    .\scripts\status.ps1 -Watch
#>
[CmdletBinding()]
param([switch]$Watch)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

if (-not (Test-DockerInstalled)) { Stop-WithError 'Docker is not installed or not in PATH' }
$null = Get-ComposeCommand

function Test-TcpPort {
    param([string]$ComputerHost, [int]$Port, [int]$TimeoutMs = 2000)
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $task = $tcp.ConnectAsync($ComputerHost, $Port)
        $ok = $task.Wait($TimeoutMs) -and $tcp.Connected
        $tcp.Close()
        return $ok
    } catch {
        return $false
    }
}

function Show-Status {
    if ($Watch) { Clear-Host }
    Write-LgBanner "LeaseGenie Stack -- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  (HYBRID)"

    Write-Host ''
    Write-LgInfo 'Docker containers:'
    try { Invoke-Compose ps | Out-Null } catch { Write-LgWarn '  compose not running' }

    Write-Host ''
    Write-LgInfo 'Docker volumes:'
    try { & docker volume ls --filter 'name=leasegenie' --format 'table {{.Name}}`t{{.Driver}}' 2>$null } catch { }

    # ---- Host services ----
    Write-Host ''
    Write-LgInfo 'Host services:'

    $dbUrl    = Get-EnvValue -Key 'DATABASE_URL'      -Default 'postgresql+psycopg2://leasegenie:leasegenie@localhost:5432/leasegenie'
    $redisUrl = Get-EnvValue -Key 'CELERY_BROKER_URL' -Default 'redis://localhost:6379/0'
    $ollamaUrl = Get-EnvValue -Key 'OLLAMA_BASE_URL'  -Default 'http://localhost:11434'

    $pgHost = 'localhost'; $pgPort = 5432
    if ($dbUrl -match '@([^:/]+):(\d+)') {
        $pgHost = $matches[1]; $pgPort = [int]$matches[2]
        if ($pgHost -eq 'host.docker.internal') { $pgHost = 'localhost' }
    }

    $redisHost = 'localhost'; $redisPort = 6379
    if ($redisUrl -match 'redis://([^:/]+):(\d+)') {
        $redisHost = $matches[1]; $redisPort = [int]$matches[2]
        if ($redisHost -eq 'host.docker.internal') { $redisHost = 'localhost' }
    }

    $ollamaCheckUrl = $ollamaUrl -replace 'host\.docker\.internal', 'localhost'

    if (Test-TcpPort -ComputerHost $pgHost -Port $pgPort) {
        Write-LgOk "  Postgres at ${pgHost}:${pgPort}: REACHABLE"
    } else {
        Write-LgWarn "  Postgres at ${pgHost}:${pgPort}: NOT reachable"
    }

    if (Test-TcpPort -ComputerHost $redisHost -Port $redisPort) {
        Write-LgOk "  Redis at ${redisHost}:${redisPort}: REACHABLE"
    } else {
        Write-LgWarn "  Redis at ${redisHost}:${redisPort}: NOT reachable"
    }

    try {
        $resp = Invoke-WebRequest -Uri "$ollamaCheckUrl/api/tags" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        Write-LgOk "  Ollama at ${ollamaCheckUrl}: REACHABLE"
        try {
            $body = $resp.Content | ConvertFrom-Json
            if ($body.models -and $body.models.Count -gt 0) {
                Write-LgInfo '  Pulled models:'
                foreach ($m in $body.models) { Write-Host "    $($m.name)" }
            }
        } catch { }
    } catch {
        Write-LgWarn "  Ollama at ${ollamaCheckUrl}: NOT reachable"
    }

    # ---- API endpoint ----
    Write-Host ''
    Write-LgInfo 'API endpoint:'
    $apiPort = Get-EnvValue -Key 'API_PORT' -Default '8000'
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$apiPort/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { Write-LgOk "  http://localhost:$apiPort/health responding" }
    } catch {
        Write-LgWarn "  http://localhost:$apiPort/health NOT responding"
    }
}

if ($Watch) {
    while ($true) {
        Show-Status
        Write-Host ''
        Write-Host '(refreshing every 5s -- Ctrl-C to stop)'
        Start-Sleep -Seconds 5
    }
} else {
    Show-Status
}
