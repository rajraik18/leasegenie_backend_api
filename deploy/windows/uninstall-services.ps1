<#
.SYNOPSIS
    Stop and remove the LeaseGenie Windows Services.

.DESCRIPTION
    Counterpart to install-services.ps1. Stops `leasegenie-api` and
    `leasegenie-worker`, then deletes them. Tries NSSM first; falls back to
    sc.exe. Run from an elevated PowerShell prompt.

.EXAMPLE
    .\deploy\windows\uninstall-services.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '[X] This script must be run from an elevated PowerShell prompt' -ForegroundColor Red
    exit 1
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $here '..\..')).Path
$nssmLocal = Join-Path $repoRoot 'deploy\windows\bin\nssm.exe'
$nssmCmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
$nssm = if (Test-Path $nssmLocal) { $nssmLocal } elseif ($nssmCmd) { $nssmCmd.Source } else { $null }

function Test-ServiceExists {
    param([string]$Name)
    return ($null -ne (Get-Service -Name $Name -ErrorAction SilentlyContinue))
}

function Remove-Lg {
    param([string]$Name)
    if (-not (Test-ServiceExists -Name $Name)) {
        Write-Host "[*] $Name is not registered -- skipping"
        return
    }
    Write-Host "[*] Stopping $Name ..."
    try { Stop-Service -Name $Name -Force -ErrorAction Stop } catch {}
    Start-Sleep -Seconds 2
    if ($nssm) {
        & $nssm remove $Name confirm | Out-Null
    } else {
        sc.exe delete $Name | Out-Null
    }
    Write-Host "[OK] $Name removed" -ForegroundColor Green
}

# Worker first (it depends on api)
Remove-Lg -Name 'leasegenie-worker'
Remove-Lg -Name 'leasegenie-api'

Write-Host ''
Write-Host '[OK] Done. Local Postgres / Redis / Ollama services were not touched.' -ForegroundColor Green
