<#
.SYNOPSIS
    Restart the LeaseGenie containers (HYBRID deployment).

.DESCRIPTION
    Only api+worker run in Docker. Postgres, Redis, Ollama are local installs
    on the host and are NOT restarted by this script.

    Strategies:
        -Rolling   (Default) docker compose restart api worker. ~15s.
        -Full      Stop + up. ~30s. Use after compose.yml changes.
        -Hard      Down + up. ~60s. Use after Dockerfile changes.

    -Build can be combined with any strategy to rebuild images first.

.EXAMPLE
    .\scripts\restart.ps1
.EXAMPLE
    .\scripts\restart.ps1 -Build
.EXAMPLE
    .\scripts\restart.ps1 -Hard
#>
[CmdletBinding()]
param(
    [switch]$Rolling,
    [switch]$Full,
    [switch]$Hard,
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

$strategy = if ($Hard) { 'hard' } elseif ($Full) { 'full' } else { 'rolling' }

Write-LgBanner "LeaseGenie API -- restart (strategy=$strategy, hybrid)"

if (-not (Test-DockerInstalled)) { Stop-WithError 'Docker is not installed or not in PATH' }
$null = Get-ComposeCommand
Test-EnvFile
Get-LifecycleLock -Action 'restart'

try {
    $targets = @('api', 'worker')
    Write-LgInfo "Targets: $($targets -join ', ')"

    if ($Build) {
        Write-LgBanner 'Rebuilding images'
        Invoke-Compose build @targets | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-WithError 'Build failed' }
        Write-LgOk "Rebuilt: $($targets -join ', ')"
    }

    switch ($strategy) {
        'rolling' {
            Write-LgBanner 'Rolling restart'
            foreach ($svc in $targets) {
                Write-LgInfo "Restarting $svc..."
                Invoke-Compose restart $svc | Out-Null
                if ($LASTEXITCODE -ne 0) { Write-LgWarn "$svc restart returned non-zero" }
            }
        }
        'full' {
            Write-LgBanner 'Full restart'
            Write-LgInfo 'Stopping target services...'
            foreach ($svc in $targets) {
                Stop-ServiceGracefully -Service $svc -TimeoutSec 30
            }
            Write-LgInfo 'Starting target services...'
            Invoke-Compose up -d @targets | Out-Null
            if ($LASTEXITCODE -ne 0) { Stop-WithError 'Failed to start services' }
        }
        'hard' {
            Write-LgBanner 'Hard restart (container recreation)'
            Invoke-Compose up -d --force-recreate @targets | Out-Null
            if ($LASTEXITCODE -ne 0) { Stop-WithError 'Recreate failed' }
        }
    }

    Write-LgBanner 'Verifying'
    $allHealthy = $true
    foreach ($svc in $targets) {
        if (-not (Wait-ForServiceHealth -Service $svc -TimeoutSec 60)) {
            $allHealthy = $false
        }
    }

    Write-LgBanner 'Status'
    Invoke-Compose ps | Out-Null

    $apiPort = Get-EnvValue -Key 'API_PORT' -Default '8000'
    Write-Host ''
    Write-LgInfo "Smoke-testing http://localhost:$apiPort/health ..."
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$apiPort/health" -TimeoutSec 5 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { Write-LgOk "API responding at http://localhost:$apiPort" }
    } catch {
        Write-LgWarn 'API not responding yet (may still be booting)'
    }

    if ($allHealthy) {
        Write-LgBanner 'Restarted'
        Write-LgOk 'All target services are healthy'
    } else {
        Write-LgBanner 'Restarted (with warnings)'
        Write-LgWarn 'Some services did not pass health checks -- check logs'
        exit 2
    }
} finally {
    Remove-LifecycleLock
}
