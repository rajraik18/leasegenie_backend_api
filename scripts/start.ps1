<#
.SYNOPSIS
    Start the LeaseGenie stack (HYBRID deployment).

.DESCRIPTION
    Only api+worker run in Docker. Postgres, Redis, and Ollama run as native
    installs on the host machine. This script verifies the host services are
    reachable, then starts the Docker containers.

    Modes:
        -Cold        First-time start. Verifies host infra and Ollama models.
        -Warm        Default. Quick start with minimal checks.
        -Build       Rebuild API/worker images before starting.
        -SkipChecks  Skip host-infra reachability checks (faster).

.EXAMPLE
    .\scripts\start.ps1
    Warm start (default).

.EXAMPLE
    .\scripts\start.ps1 -Cold
    First-time bring-up with full pre-flight checks.

.EXAMPLE
    .\scripts\start.ps1 -Build
    Rebuild images, then warm start.
#>
[CmdletBinding()]
param(
    [switch]$Cold,
    [switch]$Warm,
    [switch]$Build,
    [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'

# Dot-source the common module
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

# ---- Mode resolution ----
$mode = if ($Cold) { 'cold' } else { 'warm' }

Write-LgBanner "LeaseGenie API -- start ($mode mode, hybrid deployment)"

# ---- Pre-flight ----
if (-not (Test-DockerInstalled)) {
    Stop-WithError "Docker is not installed or not in PATH"
}
$null = Get-ComposeCommand
Test-EnvFile
Get-LifecycleLock -Action 'start'

try {
    # ---- Phase 1: Verify host infrastructure ----
    if (-not $SkipChecks) {
        Write-LgBanner 'Phase 1 -- verifying host infrastructure'

        # Read URLs from .env and translate host.docker.internal to localhost
        # for checks running on the host machine itself.
        $dbUrl    = Get-EnvValue -Key 'DATABASE_URL'        -Default 'postgresql+psycopg2://leasegenie:leasegenie@localhost:5432/leasegenie'
        $redisUrl = Get-EnvValue -Key 'CELERY_BROKER_URL'   -Default 'redis://localhost:6379/0'
        $ollamaUrl = Get-EnvValue -Key 'OLLAMA_BASE_URL'    -Default 'http://localhost:11434'

        # Parse Postgres host:port from URL
        $pgHost = 'localhost'
        $pgPort = 5432
        if ($dbUrl -match '@([^:/]+):(\d+)') {
            $pgHost = $matches[1]
            $pgPort = [int]$matches[2]
            if ($pgHost -eq 'host.docker.internal') { $pgHost = 'localhost' }
        }

        # Parse Redis host:port
        $redisHost = 'localhost'
        $redisPort = 6379
        if ($redisUrl -match 'redis://([^:/]+):(\d+)') {
            $redisHost = $matches[1]
            $redisPort = [int]$matches[2]
            if ($redisHost -eq 'host.docker.internal') { $redisHost = 'localhost' }
        }

        # Build Ollama check URL
        $ollamaCheckUrl = $ollamaUrl -replace 'host\.docker\.internal', 'localhost'

        # ---- Postgres check ----
        Write-LgInfo "Checking Postgres at ${pgHost}:${pgPort} ..."
        $pgOk = $false
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.ConnectAsync($pgHost, $pgPort).Wait(3000) | Out-Null
            $pgOk = $tcp.Connected
            $tcp.Close()
        } catch {
            $pgOk = $false
        }
        if ($pgOk) {
            Write-LgOk "Postgres is reachable"
        } else {
            Write-LgErr "Postgres is NOT reachable at ${pgHost}:${pgPort}"
            Write-LgErr "Make sure your local Postgres is running on this port."
            Write-LgErr "See DEPLOYMENT.md section 'Hybrid setup' for configuration."
            Stop-WithError 'Postgres unreachable'
        }

        # ---- Redis check ----
        Write-LgInfo "Checking Redis at ${redisHost}:${redisPort} ..."
        $redisOk = $false
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.ConnectAsync($redisHost, $redisPort).Wait(3000) | Out-Null
            $redisOk = $tcp.Connected
            $tcp.Close()
        } catch {
            $redisOk = $false
        }
        if ($redisOk) {
            Write-LgOk "Redis is reachable"
        } else {
            Write-LgErr "Redis is NOT reachable at ${redisHost}:${redisPort}"
            Write-LgErr "Start your local Redis (e.g. via the Windows service or 'redis-server')"
            Stop-WithError 'Redis unreachable'
        }

        # ---- Ollama check ----
        Write-LgInfo "Checking Ollama at $ollamaCheckUrl ..."
        $ollamaOk = $false
        $tagsResponse = $null
        try {
            $tagsResponse = Invoke-WebRequest -Uri "$ollamaCheckUrl/api/tags" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            $ollamaOk = ($tagsResponse.StatusCode -eq 200)
        } catch {
            $ollamaOk = $false
        }
        if ($ollamaOk) {
            Write-LgOk "Ollama is reachable"

            # On cold start, also check that the required models are pulled
            if ($mode -eq 'cold' -and $tagsResponse) {
                $llmModel   = Get-EnvValue -Key 'OLLAMA_MODEL'       -Default 'qwen2.5:14b-instruct-q5_K_M'
                $embedModel = Get-EnvValue -Key 'OLLAMA_EMBED_MODEL' -Default 'nomic-embed-text'
                $tagsBody = $tagsResponse.Content
                $llmKey   = ($llmModel   -split ':')[0]
                $embedKey = ($embedModel -split ':')[0]
                if ($tagsBody -match "`"$llmKey") {
                    Write-LgOk "Required model present: $llmModel"
                } else {
                    Write-LgWarn "Model $llmModel not pulled yet"
                    Write-LgWarn "Run on the host: ollama pull $llmModel"
                }
                if ($tagsBody -match "`"$embedKey") {
                    Write-LgOk "Required embed model present: $embedModel"
                } else {
                    Write-LgWarn "Embed model $embedModel not pulled yet"
                    Write-LgWarn "Run on the host: ollama pull $embedModel"
                }
            }
        } else {
            Write-LgErr "Ollama is NOT reachable at $ollamaCheckUrl"
            Write-LgErr "Start it on the host (Windows: launch 'Ollama' from Start menu)"
            Write-LgErr "Make sure OLLAMA_HOST=0.0.0.0 is set so containers can reach it"
            Stop-WithError 'Ollama unreachable'
        }
    }

    # ---- Phase 2: Optional rebuild ----
    if ($Build) {
        Write-LgBanner 'Phase 2 -- rebuilding application images'
        Invoke-Compose build api worker | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-WithError 'Image build failed' }
        Write-LgOk 'Build complete'
    }

    # ---- Phase 3: Start application services ----
    Write-LgBanner 'Phase 3 -- starting api + worker'
    Invoke-Compose up -d api worker | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'Failed to start api/worker' }

    if (-not (Wait-ForServiceHealth -Service 'api' -TimeoutSec 60)) {
        Write-LgWarn 'api did not report healthy -- check logs'
    }

    # ---- Final status ----
    Write-LgBanner 'Status'
    Invoke-Compose ps | Out-Null

    # Smoke test
    $apiPort = Get-EnvValue -Key 'API_PORT' -Default '8000'
    Write-Host ''
    Write-LgInfo "Smoke-testing http://localhost:$apiPort/health ..."
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$apiPort/health" -TimeoutSec 5 -UseBasicParsing
        if ($resp.StatusCode -eq 200) {
            Write-LgOk "API responding at http://localhost:$apiPort"
            Write-LgOk "Docs: http://localhost:$apiPort/docs"
        }
    } catch {
        Write-LgWarn "API not yet responding at http://localhost:$apiPort/health"
        Write-LgWarn "Run '.\scripts\logs.ps1 api' to inspect"
    }

    Write-LgBanner 'Started'
    Write-LgOk "LeaseGenie stack is up ($mode mode, hybrid)"

} finally {
    Remove-LifecycleLock
}
