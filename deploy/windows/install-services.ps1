<#
.SYNOPSIS
    Register the LeaseGenie API and Celery worker as Windows Services.

.DESCRIPTION
    Wraps the venv's python.exe in two Windows Services so they auto-start on
    boot and auto-restart on crash. Two backends are supported:

      -UseNSSM   (default) Uses NSSM (the Non-Sucking Service Manager — MIT-
                 licensed, single-binary). NSSM handles graceful shutdown
                 (CTRL_BREAK_EVENT) and stdout/stderr file logging cleanly.
                 If nssm.exe is not found in deploy\windows\bin\ or PATH the
                 script offers to download it from https://nssm.cc.
      -UseSC     Uses the built-in sc.exe. Fewer features (no graceful
                 shutdown, no stdout redirect — services log to the Event
                 Viewer); no third-party dependency.

    Idempotent — if the services already exist, they are stopped and
    re-installed.

    Must be run from an elevated PowerShell prompt.

.PARAMETER ServiceUser
    Optional Windows account to run the services under (e.g. `.\leasegenie`).
    Default: LocalSystem. Provide -ServicePassword too if a domain account.

.EXAMPLE
    .\deploy\windows\install-services.ps1
    Default: NSSM-backed services running as LocalSystem.

.EXAMPLE
    .\deploy\windows\install-services.ps1 -UseSC
    Use sc.exe (no NSSM dependency).
#>
[CmdletBinding()]
param(
    [switch]$UseNSSM,
    [switch]$UseSC,
    [string]$ServiceUser = '',
    [SecureString]$ServicePassword = $null,
    [switch]$FetchNSSM
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $UseNSSM -and -not $UseSC) { $UseNSSM = $true }
if ($UseNSSM -and $UseSC) {
    Write-Host '[X] Pick exactly one of -UseNSSM or -UseSC' -ForegroundColor Red
    exit 1
}

# ---- Elevation check ----
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '[X] This script must be run from an elevated PowerShell prompt' -ForegroundColor Red
    exit 1
}

# ---- Paths ----
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $here '..\..')).Path
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$envFile = Join-Path $repoRoot '.env'
$logDir  = Join-Path $repoRoot 'logs'
$apiLog    = Join-Path $logDir 'api.log'
$workerLog = Join-Path $logDir 'worker.log'
$apiSvc    = 'leasegenie-api'
$workerSvc = 'leasegenie-worker'

if (-not (Test-Path $venvPython)) {
    Write-Host "[X] Virtual environment not found at $venvPython" -ForegroundColor Red
    Write-Host "    Run from the repo root:"
    Write-Host "      python -m venv .venv"
    Write-Host "      .\.venv\Scripts\Activate.ps1"
    Write-Host "      pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path $envFile)) {
    Write-Host "[X] .env not found at $envFile" -ForegroundColor Red
    Write-Host "    Copy .env.example to .env and fill in real values first."
    exit 1
}
$null = New-Item -ItemType Directory -Force -Path $logDir

# ---- NSSM resolution ----
function Resolve-NssmExe {
    $local = Join-Path $repoRoot 'deploy\windows\bin\nssm.exe'
    if (Test-Path $local) { return $local }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { return $cmd.Source }
    return $null
}

function Get-NssmExe {
    $found = Resolve-NssmExe
    if ($found) { return $found }
    if (-not $FetchNSSM) {
        Write-Host '[X] nssm.exe not found.' -ForegroundColor Red
        Write-Host '    Install options:'
        Write-Host '      1. Download nssm-2.24 from https://nssm.cc/download'
        Write-Host '      2. Copy nssm.exe to deploy\windows\bin\'
        Write-Host '      3. Re-run this script'
        Write-Host '    Or re-run with -FetchNSSM to auto-download (network required).'
        exit 1
    }
    Write-Host '[*] Downloading NSSM 2.24 ...'
    $tmp = Join-Path $env:TEMP "nssm-2.24-$(Get-Random).zip"
    Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile $tmp -UseBasicParsing
    $extract = Join-Path $env:TEMP "nssm-extract-$(Get-Random)"
    Expand-Archive -Path $tmp -DestinationPath $extract -Force
    $arch = if ([Environment]::Is64BitOperatingSystem) { 'win64' } else { 'win32' }
    $dst = Join-Path $repoRoot 'deploy\windows\bin'
    $null = New-Item -ItemType Directory -Force -Path $dst
    $candidate = Get-ChildItem -Path $extract -Recurse -Filter 'nssm.exe' | Where-Object { $_.FullName -match $arch } | Select-Object -First 1
    if (-not $candidate) {
        Write-Host '[X] Could not find nssm.exe in the downloaded archive' -ForegroundColor Red
        exit 1
    }
    Copy-Item -Path $candidate.FullName -Destination (Join-Path $dst 'nssm.exe') -Force
    Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    return Resolve-NssmExe
}

# ---- Service helpers ----
function Test-ServiceExists {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    return ($null -ne $svc)
}

function Stop-AndRemove {
    param([string]$Name)
    if (-not (Test-ServiceExists -Name $Name)) { return }
    Write-Host "[*] Stopping existing service $Name ..."
    try { Stop-Service -Name $Name -Force -ErrorAction Stop } catch {}
    Start-Sleep -Seconds 2
    if ($UseNSSM) {
        $nssm = Resolve-NssmExe
        if ($nssm) {
            & $nssm remove $Name confirm | Out-Null
        } else {
            sc.exe delete $Name | Out-Null
        }
    } else {
        sc.exe delete $Name | Out-Null
    }
    Start-Sleep -Seconds 1
}

# ---- Build env block (passed to NSSM AppEnvironmentExtra or sc.exe via setenv hack) ----
$envLines = Get-Content $envFile | Where-Object {
    $_ -and ($_ -notmatch '^\s*#') -and ($_ -match '=')
}
$envPairs = @()
foreach ($l in $envLines) {
    $parts = $l -split '=', 2
    if ($parts.Length -eq 2) {
        $k = $parts[0].Trim()
        $v = $parts[1].Trim()
        $envPairs += "$k=$v"
    }
}

# ---- Install ----
Write-Host "[*] Repo root:    $repoRoot"
Write-Host "[*] venv python:  $venvPython"
Write-Host "[*] Backend:      $(if ($UseNSSM) { 'NSSM' } else { 'sc.exe' })"

Stop-AndRemove -Name $workerSvc
Stop-AndRemove -Name $apiSvc

# --- Resolve API_PORT, API_HOST, WORKER_POOL, WORKER_CONCURRENCY from .env ---
function Get-EnvOrDefault {
    param([string]$Key, [string]$Default)
    $line = $envLines | Where-Object { $_ -match "^\s*${Key}\s*=" } | Select-Object -First 1
    if ($line) {
        return (($line -split '=', 2)[1].Trim().Trim('"').Trim("'"))
    }
    return $Default
}
$apiPort = Get-EnvOrDefault 'API_PORT' '8000'
# Bind to loopback by default. Reverse proxy (IIS / Caddy / nginx) terminates
# TLS and forwards to 127.0.0.1:$apiPort. Override with API_HOST=0.0.0.0 in
# .env if you really want to expose uvicorn directly.
$apiHost           = Get-EnvOrDefault 'API_HOST'           '127.0.0.1'
$workerPool        = Get-EnvOrDefault 'WORKER_POOL'        'threads'
$workerConcurrency = Get-EnvOrDefault 'WORKER_CONCURRENCY' '4'

if ($UseNSSM) {
    $nssm = Get-NssmExe

    & $nssm install $apiSvc $venvPython '-m' 'uvicorn' 'app.main:app' '--host' $apiHost '--port' $apiPort
    & $nssm set $apiSvc AppDirectory $repoRoot
    & $nssm set $apiSvc AppStdout $apiLog
    & $nssm set $apiSvc AppStderr $apiLog
    & $nssm set $apiSvc AppRotateFiles 1
    & $nssm set $apiSvc AppRotateBytes 52428800       # 50 MB
    & $nssm set $apiSvc AppEnvironmentExtra ([string]::Join("`n", $envPairs))
    & $nssm set $apiSvc Start SERVICE_AUTO_START
    & $nssm set $apiSvc AppExit Default Restart
    & $nssm set $apiSvc AppRestartDelay 5000
    & $nssm set $apiSvc AppStopMethodConsole 30000
    & $nssm set $apiSvc Description 'LeaseGenie FastAPI server (uvicorn).'

    # Worker service. `-B` runs the beat scheduler alongside the worker so
    # the daily cleanup tasks fire without a separate process.
    & $nssm install $workerSvc $venvPython '-m' 'celery' '-A' 'app.workers.celery_app:celery_app' 'worker' '-B' '--loglevel=info' "--concurrency=$workerConcurrency" '-P' $workerPool
    & $nssm set $workerSvc AppDirectory $repoRoot
    & $nssm set $workerSvc AppStdout $workerLog
    & $nssm set $workerSvc AppStderr $workerLog
    & $nssm set $workerSvc AppRotateFiles 1
    & $nssm set $workerSvc AppRotateBytes 52428800    # 50 MB
    & $nssm set $workerSvc AppEnvironmentExtra ([string]::Join("`n", $envPairs))
    & $nssm set $workerSvc Start SERVICE_AUTO_START
    & $nssm set $workerSvc AppExit Default Restart
    & $nssm set $workerSvc AppRestartDelay 5000
    & $nssm set $workerSvc AppStopMethodConsole 30000
    & $nssm set $workerSvc DependOnService $apiSvc
    & $nssm set $workerSvc Description 'LeaseGenie Celery worker.'

    if ($ServiceUser) {
        if ($null -eq $ServicePassword) {
            $ServicePassword = Read-Host "Password for $ServiceUser" -AsSecureString
        }
        $plainPwd = [System.Net.NetworkCredential]::new('', $ServicePassword).Password
        & $nssm set $apiSvc ObjectName $ServiceUser $plainPwd
        & $nssm set $workerSvc ObjectName $ServiceUser $plainPwd
    }

    Write-Host '[*] Starting services ...'
    Start-Service $apiSvc
    Start-Service $workerSvc
} else {
    # sc.exe — minimal install. Caveat: env vars come from .env at runtime via
    # Python's pydantic-settings, but Windows services don't inherit a
    # console environment, so the python process must read .env itself
    # (which it does via SettingsConfigDict env_file=".env"). We just need
    # to make sure the working directory is the repo root so .env is found.
    $apiBin = "`"$venvPython`" -m uvicorn app.main:app --host $apiHost --port $apiPort"
    $workerBin = "`"$venvPython`" -m celery -A app.workers.celery_app:celery_app worker -B --loglevel=info --concurrency=$workerConcurrency -P $workerPool"
    sc.exe create $apiSvc binPath= "cmd.exe /c cd /d `"$repoRoot`" && $apiBin >> `"$apiLog`" 2>&1" start= auto DisplayName= 'LeaseGenie API' | Out-Null
    sc.exe create $workerSvc binPath= "cmd.exe /c cd /d `"$repoRoot`" && $workerBin >> `"$workerLog`" 2>&1" start= auto DisplayName= 'LeaseGenie Worker' depend= $apiSvc | Out-Null
    sc.exe failure $apiSvc reset= 60 actions= restart/5000/restart/5000/restart/5000 | Out-Null
    sc.exe failure $workerSvc reset= 60 actions= restart/5000/restart/5000/restart/5000 | Out-Null

    Write-Host '[*] Starting services ...'
    Start-Service $apiSvc
    Start-Service $workerSvc
}

# --- Smoke test --------------------------------------------------------------
Write-Host ''
Write-Host '[*] Waiting for /health ...'
$healthHost = if ($apiHost -eq '0.0.0.0') { 'localhost' } else { $apiHost }
$healthUrl = "http://$healthHost`:$apiPort/health"
$deadline = (Get-Date).AddSeconds(90)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if (-not $healthy) {
    Write-Host "[X] Service started but $healthUrl did not respond 200 within 90s." -ForegroundColor Red
    Write-Host "    Inspect: Get-Content $apiLog -Tail 60"
    exit 2
}

Write-Host ''
Write-Host '[OK] Services installed, started, and /health verified.' -ForegroundColor Green
Write-Host "     Status:    Get-Service $apiSvc, $workerSvc"
Write-Host "     API logs:  Get-Content $apiLog -Tail 40"
Write-Host "     Worker:    Get-Content $workerLog -Tail 40"
Write-Host "     Uninstall: .\deploy\windows\uninstall-services.ps1"
Write-Host "     Reverse-proxy guide: .\deploy\windows\REVERSE_PROXY.md"
