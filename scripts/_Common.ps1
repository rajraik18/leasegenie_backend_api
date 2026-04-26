# scripts/_Common.ps1
# Shared helpers for the .ps1 lifecycle scripts. Dot-source, don't execute.
# Mirrors scripts/_common.sh as closely as PowerShell allows.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Pre-initialize every script-scoped variable.
# `Set-StrictMode -Version Latest` errors on reads of uninitialized variables,
# so all script-scoped state must be created here before any function runs.
# ---------------------------------------------------------------------------
$Script:ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script:ProjectRoot   = (Resolve-Path (Join-Path $Script:ScriptDir '..')).Path
$Script:LockFile      = Join-Path $Script:ProjectRoot '.lifecycle.lock'
$Script:ComposeCmd    = $null   # populated lazily by Get-ComposeCommand
# HYBRID deployment: only api+worker run in Docker. The infrastructure
# (postgres, redis, ollama) runs natively on the host machine.
$Script:AllServices   = @('api', 'worker')
$Script:InfraServices = @()
$Script:AppServices   = @('api', 'worker')

Set-Location $Script:ProjectRoot

# ---------------------------------------------------------------------------
# Logging helpers (ASCII-only output for cross-terminal compatibility)
# ---------------------------------------------------------------------------
function Write-LgInfo {
    param([string]$Message)
    $ts = Get-Date -Format 'HH:mm:ss'
    Write-Host "[$ts] $Message" -ForegroundColor Cyan
}

function Write-LgOk {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-LgWarn {
    param([string]$Message)
    Write-Host "[!] $Message" -ForegroundColor Yellow
}

function Write-LgErr {
    param([string]$Message)
    Write-Host "[X] $Message" -ForegroundColor Red
}

function Write-LgBanner {
    param([string]$Message)
    Write-Host ''
    Write-Host "-- $Message --" -ForegroundColor Cyan
}

function Stop-WithError {
    param([string]$Message)
    Write-LgErr $Message
    exit 1
}

# ---------------------------------------------------------------------------
# Required tooling
# ---------------------------------------------------------------------------
function Test-DockerInstalled {
    try {
        $null = & docker --version 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-ComposeCommand {
    # Returns either @('docker', 'compose') (v2) or @('docker-compose') (v1).
    # Result is cached in $Script:ComposeCmd for the rest of the session.
    if ($null -ne $Script:ComposeCmd) {
        return $Script:ComposeCmd
    }

    # Try Docker Compose v2 first (preferred)
    $null = & docker compose version 2>$null
    if ($LASTEXITCODE -eq 0) {
        $Script:ComposeCmd = @('docker', 'compose')
        return $Script:ComposeCmd
    }

    # Fall back to standalone docker-compose v1
    $legacy = Get-Command docker-compose -ErrorAction SilentlyContinue
    if ($null -ne $legacy) {
        $Script:ComposeCmd = @('docker-compose')
        return $Script:ComposeCmd
    }

    Stop-WithError "Neither 'docker compose' nor 'docker-compose' is installed"
}

function Invoke-Compose {
    # Wrapper that runs `docker compose ARGS...` (or `docker-compose ARGS...`)
    # and returns the exit code. Output goes through to the console.
    $cmd = Get-ComposeCommand
    if ($cmd.Length -eq 1) {
        & $cmd[0] @args
    } else {
        & $cmd[0] $cmd[1] @args
    }
    return $LASTEXITCODE
}

function Invoke-ComposeQuiet {
    # Same as Invoke-Compose but captures output as a single string.
    $cmd = Get-ComposeCommand
    if ($cmd.Length -eq 1) {
        $output = & $cmd[0] @args 2>&1 | Out-String
    } else {
        $output = & $cmd[0] $cmd[1] @args 2>&1 | Out-String
    }
    return $output
}

# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------
function Test-EnvFile {
    if (-not (Test-Path '.env')) {
        if (Test-Path '.env.example') {
            Write-LgWarn ".env not found - copying from .env.example"
            Copy-Item '.env.example' '.env'
            Write-LgWarn "Edit .env before running in production (passwords, secrets)"
        } else {
            Stop-WithError ".env file is missing and no .env.example found"
        }
    }
}

function Get-EnvValue {
    param(
        [string]$Key,
        [string]$Default = ''
    )
    if (-not (Test-Path '.env')) { return $Default }

    $line = Get-Content '.env' | Where-Object { $_ -match "^\s*${Key}\s*=" } | Select-Object -First 1
    if (-not $line) { return $Default }

    $parts = $line -split '=', 2
    if ($parts.Length -lt 2) { return $Default }

    $val = $parts[1].Trim().Trim('"').Trim("'")
    if ([string]::IsNullOrEmpty($val)) { return $Default }
    return $val
}

# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
function Wait-ForServiceHealth {
    param(
        [string]$Service,
        [int]$TimeoutSec = 120
    )
    $elapsed = 0
    $interval = 3

    Write-LgInfo "Waiting for $Service to become healthy (timeout ${TimeoutSec}s)..."
    while ($elapsed -lt $TimeoutSec) {
        $cmd = Get-ComposeCommand
        if ($cmd.Length -eq 1) {
            $json = & $cmd[0] ps --format json $Service 2>$null
        } else {
            $json = & $cmd[0] $cmd[1] ps --format json $Service 2>$null
        }

        if ($LASTEXITCODE -eq 0 -and $json) {
            $lines = $json -split "`n" | Where-Object { $_ -match '\S' }
            foreach ($line in $lines) {
                try {
                    $d = $line | ConvertFrom-Json -ErrorAction Stop
                    $status = 'unknown'
                    if ($null -ne $d.PSObject.Properties['Health'] -and $d.Health) {
                        $status = $d.Health
                    } elseif ($null -ne $d.PSObject.Properties['State'] -and $d.State) {
                        $status = $d.State
                    }
                    switch ($status) {
                        'healthy' {
                            Write-LgOk "$Service is healthy"
                            return $true
                        }
                        'running' {
                            Write-LgOk "$Service is running (no healthcheck declared)"
                            return $true
                        }
                        'unhealthy' {
                            Write-LgErr "$Service reports UNHEALTHY"
                            Invoke-Compose logs --tail=30 $Service | Out-Null
                            return $false
                        }
                    }
                    break
                } catch {
                    # Ignore parse errors and keep polling
                }
            }
        }
        Start-Sleep -Seconds $interval
        $elapsed += $interval
    }

    Write-LgErr "$Service did not become healthy within ${TimeoutSec}s"
    Invoke-Compose logs --tail=30 $Service | Out-Null
    return $false
}

# ---------------------------------------------------------------------------
# Ollama model warm-up
# ---------------------------------------------------------------------------
function Confirm-OllamaModels {
    param(
        [string]$LlmModel = 'qwen2.5:32b',
        [string]$EmbedModel = 'nomic-embed-text'
    )

    Write-LgInfo "Checking Ollama models..."
    $cmd = Get-ComposeCommand
    if ($cmd.Length -eq 1) {
        $existing = & $cmd[0] exec -T ollama ollama list 2>$null
    } else {
        $existing = & $cmd[0] $cmd[1] exec -T ollama ollama list 2>$null
    }
    if (-not $existing) { $existing = '' }

    $llmKey = ($LlmModel -split ':')[0]
    if ($existing -match "(?m)^$llmKey") {
        Write-LgOk "Model $LlmModel already pulled"
    } else {
        Write-LgWarn "Model $LlmModel not present - pulling (~20GB, may take 10-30 minutes)"
        if ($cmd.Length -eq 1) {
            & $cmd[0] exec -T ollama ollama pull $LlmModel
        } else {
            & $cmd[0] $cmd[1] exec -T ollama ollama pull $LlmModel
        }
        if ($LASTEXITCODE -ne 0) {
            Write-LgErr "Ollama pull failed for $LlmModel"
            return $false
        }
        Write-LgOk "Pulled $LlmModel"
    }

    $embedKey = ($EmbedModel -split ':')[0]
    if ($existing -match "(?m)^$embedKey") {
        Write-LgOk "Embed model $EmbedModel already pulled"
    } else {
        Write-LgWarn "Pulling embedding model $EmbedModel (~270MB)..."
        if ($cmd.Length -eq 1) {
            & $cmd[0] exec -T ollama ollama pull $EmbedModel
        } else {
            & $cmd[0] $cmd[1] exec -T ollama ollama pull $EmbedModel
        }
        if ($LASTEXITCODE -ne 0) {
            Write-LgErr "Ollama pull failed for $EmbedModel"
            return $false
        }
        Write-LgOk "Pulled $EmbedModel"
    }
    return $true
}

# ---------------------------------------------------------------------------
# Stop helpers
# ---------------------------------------------------------------------------
function Stop-ServiceGracefully {
    param(
        [string]$Service,
        [int]$TimeoutSec = 30
    )
    Write-LgInfo "Stopping $Service gracefully (SIGTERM, ${TimeoutSec}s grace)..."
    Invoke-Compose stop -t $TimeoutSec $Service | Out-Null
}

# ---------------------------------------------------------------------------
# Lock file (prevents concurrent start/stop)
# ---------------------------------------------------------------------------
function Get-LifecycleLock {
    param([string]$Action)

    if (Test-Path $Script:LockFile) {
        $existingPidStr = Get-Content $Script:LockFile -ErrorAction SilentlyContinue
        $existingPid = 0
        if ($existingPidStr) {
            $null = [int]::TryParse($existingPidStr, [ref]$existingPid)
        }
        if ($existingPid -gt 0) {
            $proc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
            if ($null -ne $proc) {
                Stop-WithError "Another lifecycle script is running (PID $existingPid). If this is wrong, delete $Script:LockFile"
            }
        }
        Write-LgWarn "Stale lock file found - removing"
        Remove-Item $Script:LockFile -Force
    }

    $PID | Out-File $Script:LockFile -Encoding ASCII
    Write-LgInfo "Acquired lifecycle lock for: $Action"
}

function Remove-LifecycleLock {
    if (Test-Path $Script:LockFile) {
        Remove-Item $Script:LockFile -Force -ErrorAction SilentlyContinue
    }
}
