# scripts/_Common.ps1
# Shared helpers for the .ps1 lifecycle scripts. Dot-source, don't execute.
#
# This module manages two native processes — the API (uvicorn) and the Celery
# worker — as plain Windows processes. There is no Docker dependency.
#
# Two run modes:
#   Background (default) — Start-Process -WindowStyle Hidden, stdout/stderr
#                          redirected to .\logs\<name>.log, PID stored in
#                          .\run\<name>.pid.
#   Foreground           — Start-Process powershell -NoExit -Command "..."
#                          opens a new console per process so live output is
#                          visible. PID still recorded so Stop-LgProcess works.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Pre-initialise every script-scoped variable.
# `Set-StrictMode -Version Latest` errors on reads of uninitialised variables,
# so all script-scoped state must be created here before any function runs.
# ---------------------------------------------------------------------------
$Script:ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script:ProjectRoot = (Resolve-Path (Join-Path $Script:ScriptDir '..')).Path
$Script:LockFile    = Join-Path $Script:ProjectRoot '.lifecycle.lock'
$Script:RunDir      = Join-Path $Script:ProjectRoot 'run'
$Script:LogDir      = Join-Path $Script:ProjectRoot 'logs'
$Script:VenvDir     = Join-Path $Script:ProjectRoot '.venv'
$Script:AllServices = @('api', 'worker')

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
# Virtual environment
# ---------------------------------------------------------------------------
function Get-VenvPython {
    # Returns the absolute path to the venv's python.exe, or stops if missing.
    $py = Join-Path $Script:VenvDir 'Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Stop-WithError "Virtual environment not found at $($Script:VenvDir). Run: python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt"
    }
    return $py
}

function Test-VenvReady {
    return (Test-Path (Join-Path $Script:VenvDir 'Scripts\python.exe'))
}

# ---------------------------------------------------------------------------
# TCP / process probes
# ---------------------------------------------------------------------------
function Test-PortListening {
    param(
        [string]$HostName = 'localhost',
        [Parameter(Mandatory)] [int]$Port,
        [int]$TimeoutMs = 1500
    )
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.ConnectAsync($HostName, $Port).Wait($TimeoutMs) | Out-Null
        $ok = $tcp.Connected
        $tcp.Close()
        return $ok
    } catch {
        return $false
    }
}

function Get-LgPidFile {
    param([Parameter(Mandatory)] [string]$Name)
    return Join-Path $Script:RunDir "$Name.pid"
}

function Get-LgLogFile {
    param([Parameter(Mandatory)] [string]$Name)
    return Join-Path $Script:LogDir "$Name.log"
}

function Test-LgProcessAlive {
    param([Parameter(Mandatory)] [string]$Name)
    $pidFile = Get-LgPidFile -Name $Name
    if (-not (Test-Path $pidFile)) { return $false }
    $procPidStr = Get-Content $pidFile -ErrorAction SilentlyContinue
    if (-not $procPidStr) { return $false }
    $procPid = 0
    if (-not [int]::TryParse($procPidStr, [ref]$procPid)) { return $false }
    if ($procPid -le 0) { return $false }
    $proc = Get-Process -Id $procPid -ErrorAction SilentlyContinue
    return ($null -ne $proc)
}

# ---------------------------------------------------------------------------
# Process management — Start / Stop a managed background or foreground proc.
# ---------------------------------------------------------------------------
function Start-LgProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [ValidateSet('Background', 'Foreground')] [string]$Mode = 'Background',
        [string]$WindowTitle = ''
    )

    if (Test-LgProcessAlive -Name $Name) {
        $existingPid = Get-Content (Get-LgPidFile -Name $Name)
        Stop-WithError "$Name already running (PID $existingPid). Run stop.ps1 first."
    }

    $null = New-Item -ItemType Directory -Force -Path $Script:RunDir, $Script:LogDir
    $pidFile = Get-LgPidFile -Name $Name
    $logFile = Get-LgLogFile -Name $Name

    if ($Mode -eq 'Background') {
        # Write a stamped header so log restarts are obvious.
        $header = "`n=== $Name started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
        Add-Content -Path $logFile -Value $header
        $proc = Start-Process -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -WorkingDirectory $Script:ProjectRoot `
            -RedirectStandardOutput $logFile `
            -RedirectStandardError $logFile `
            -WindowStyle Hidden `
            -PassThru
    } else {
        # Foreground: open a new console so the operator sees live output.
        # We launch powershell.exe with -NoExit and run the target inline.
        # Recording the PID of the spawned powershell.exe is sufficient — when
        # we Stop-Process it, the child target dies with it.
        if (-not $WindowTitle) { $WindowTitle = "leasegenie-$Name" }
        $quotedArgs = ($ArgumentList | ForEach-Object {
            if ($_ -match '\s|"') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
        $cmd = "`$Host.UI.RawUI.WindowTitle = '$WindowTitle'; & `"$FilePath`" $quotedArgs"
        $proc = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoLogo', '-NoExit', '-Command', $cmd) `
            -WorkingDirectory $Script:ProjectRoot `
            -PassThru
    }

    Set-Content -Path $pidFile -Value $proc.Id -Encoding ASCII
    if ($Mode -eq 'Background') {
        Write-LgOk "Started $Name (PID $($proc.Id), background) -- logs: $logFile"
    } else {
        Write-LgOk "Started $Name (PID $($proc.Id), foreground -- new console window)"
    }
    return $proc.Id
}

function Stop-LgProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$Name,
        [int]$TimeoutSec = 30
    )

    $pidFile = Get-LgPidFile -Name $Name
    if (-not (Test-Path $pidFile)) {
        Write-LgWarn "$Name has no PID file - nothing to stop"
        return
    }

    $procPidStr = Get-Content $pidFile -ErrorAction SilentlyContinue
    $procPid = 0
    if (-not $procPidStr -or -not [int]::TryParse($procPidStr, [ref]$procPid) -or $procPid -le 0) {
        Write-LgWarn "$Name PID file is unreadable - removing"
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return
    }

    $proc = Get-Process -Id $procPid -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        Write-LgWarn "$Name (PID $procPid) is already gone - cleaning up PID file"
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return
    }

    Write-LgInfo "Stopping $Name (PID $procPid, grace ${TimeoutSec}s)..."
    try {
        # Polite close — does NOT throw if process has no main window.
        $null = $proc.CloseMainWindow()
    } catch {}

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { break }
        Start-Sleep -Milliseconds 500
        $proc.Refresh()
    }

    if (-not $proc.HasExited) {
        Write-LgWarn "$Name did not exit gracefully -- forcing"
        try {
            Stop-Process -Id $procPid -Force -ErrorAction Stop
        } catch {
            Write-LgWarn "Stop-Process -Force failed: $_"
        }
    }

    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-LgOk "Stopped $Name"
}

# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
function Wait-ForHttpHealth {
    param(
        [Parameter(Mandatory)] [string]$Url,
        [int]$TimeoutSec = 60,
        [int]$IntervalMs = 1000
    )
    Write-LgInfo "Waiting for $Url (timeout ${TimeoutSec}s)..."
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                Write-LgOk "$Url responded 200"
                return $true
            }
        } catch {}
        Start-Sleep -Milliseconds $IntervalMs
    }
    Write-LgErr "$Url did not respond 200 within ${TimeoutSec}s"
    return $false
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
