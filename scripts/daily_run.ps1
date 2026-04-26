<#
.SYNOPSIS
    Daily maintenance + restart for the LeaseGenie stack (HYBRID).

.DESCRIPTION
    Designed for Windows Task Scheduler. In hybrid mode, only api+worker
    are containerised. Postgres runs on the host, so backups use host-side
    pg_dump. We do NOT touch host services (no Postgres restart, no Ollama
    restart) -- those are managed separately.

.PARAMETER DryRun
    Show what would happen, don't change anything.
.PARAMETER SkipBackup
    Skip the pg_dump step.
.PARAMETER SkipRestart
    Only run backup; no restart.
.PARAMETER Weekly
    Also run VACUUM ANALYZE and prune audit_log >90 days.
.PARAMETER Force
    Skip pre-flight; restart even if jobs are running.

.EXAMPLE
    .\scripts\daily_run.ps1
.EXAMPLE
    .\scripts\daily_run.ps1 -Weekly
.EXAMPLE
    .\scripts\daily_run.ps1 -DryRun

.NOTES
    Requires psql + pg_dump in PATH for backup and weekly tasks.
    Install PostgreSQL client tools or skip with -SkipBackup.

    Windows Task Scheduler:
        Program: powershell.exe
        Args: -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\scripts\daily_run.ps1"
        Start in: C:\path\to\leasegenie_v2
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipBackup,
    [switch]$SkipRestart,
    [switch]$Weekly,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

# ---- Tunables ----
$RetentionDays = if ($env:LEASEGENIE_BACKUP_RETENTION_DAYS) { [int]$env:LEASEGENIE_BACKUP_RETENTION_DAYS } else { 7 }
$DrainTimeout  = if ($env:LEASEGENIE_DRAIN_TIMEOUT)         { [int]$env:LEASEGENIE_DRAIN_TIMEOUT }         else { 1800 }
$HealthTimeout = if ($env:LEASEGENIE_HEALTH_TIMEOUT)        { [int]$env:LEASEGENIE_HEALTH_TIMEOUT }        else { 90 }
$DailyLogDir   = if ($env:LEASEGENIE_DAILY_LOG_DIR)         { $env:LEASEGENIE_DAILY_LOG_DIR }              else { '.\logs' }
$BackupDir     = if ($env:LEASEGENIE_BACKUP_DIR)            { $env:LEASEGENIE_BACKUP_DIR }                 else { '.\backups\daily' }

# ---- Logging ----
$null = New-Item -ItemType Directory -Path $DailyLogDir -Force -ErrorAction SilentlyContinue
$dateStamp = Get-Date -Format 'yyyy-MM-dd'
$dailyLog = Join-Path $DailyLogDir "daily_run.$dateStamp.log"
Start-Transcript -Path $dailyLog -Append | Out-Null

try {
    Write-LgBanner "LeaseGenie daily run -- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  (HYBRID)"
    Write-LgInfo "log file: $dailyLog"
    if ($DryRun) { Write-LgWarn 'DRY-RUN mode -- no changes will be made' }

    if (-not (Test-DockerInstalled)) { Stop-WithError 'Docker is not installed or not in PATH' }
    $null = Get-ComposeCommand
    Get-LifecycleLock -Action 'daily_run'

    # ---- Read host Postgres details ----
    $dbUrl = Get-EnvValue -Key 'DATABASE_URL' -Default 'postgresql+psycopg2://leasegenie:leasegenie@localhost:5432/leasegenie'
    $pgUser = 'leasegenie'; $pgPass = 'leasegenie'
    $pgHost = 'localhost'; $pgPort = 5432; $pgDb = 'leasegenie'
    if ($dbUrl -match '://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)') {
        $pgUser = $matches[1]; $pgPass = $matches[2]
        $pgHost = $matches[3]; $pgPort = [int]$matches[4]; $pgDb = $matches[5]
        if ($pgHost -eq 'host.docker.internal') { $pgHost = 'localhost' }
    }

    function Test-TcpPort {
        param([string]$ComputerHost, [int]$Port, [int]$TimeoutMs = 2000)
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $task = $tcp.ConnectAsync($ComputerHost, $Port)
            $ok = $task.Wait($TimeoutMs) -and $tcp.Connected
            $tcp.Close()
            return $ok
        } catch { return $false }
    }

    function Get-RunningJobCount {
        if (-not (Get-Command psql -ErrorAction SilentlyContinue)) { return 0 }
        $env:PGPASSWORD = $pgPass
        try {
            $output = & psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb -t -A `
                -c "SELECT COUNT(*) FROM extraction_jobs WHERE status='running'" 2>$null
            $count = 0
            if ($output) { [int]::TryParse($output.ToString().Trim(), [ref]$count) | Out-Null }
            return $count
        } finally {
            Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        }
    }

    # ---- Step 1: Pre-flight ----
    Write-LgBanner 'Step 1 / Pre-flight check'

    if ($Force) {
        Write-LgWarn '-Force set, skipping pre-flight'
    } else {
        if (-not (Test-TcpPort -ComputerHost $pgHost -Port $pgPort)) {
            Write-LgErr "Host Postgres NOT reachable at ${pgHost}:${pgPort}"
            Write-LgErr 'Start it via Windows Services (postgresql-x64-16) and re-run'
            exit 1
        }
        Write-LgOk "Host Postgres reachable at ${pgHost}:${pgPort}"

        if (-not $SkipRestart) {
            Write-LgInfo 'Checking for in-flight extraction jobs...'
            $running = Get-RunningJobCount
            if ($running -gt 0) {
                Write-LgWarn "$running extraction job(s) currently running"
                Write-LgWarn "Will drain (waiting up to ${DrainTimeout}s)"
            } else {
                Write-LgOk 'No jobs running'
            }
        }
    }

    # ---- Step 2: Drain ----
    if (-not $SkipRestart) {
        Write-LgBanner 'Step 2 / Drain'
        if ($DryRun) {
            Write-LgWarn 'DRY-RUN: would stop api, then wait for worker'
        } else {
            Write-LgInfo 'Stopping API (no new requests accepted)...'
            Invoke-Compose stop -t 30 api | Out-Null
            Write-LgOk 'API stopped'

            if (-not $Force) {
                $elapsed = 0
                $interval = 15
                while ($elapsed -lt $DrainTimeout) {
                    $running = Get-RunningJobCount
                    if ($running -eq 0) {
                        Write-LgOk 'All worker jobs drained'
                        break
                    }
                    Write-LgInfo "Waiting for $running job(s)... (${elapsed}s / ${DrainTimeout}s)"
                    Start-Sleep -Seconds $interval
                    $elapsed += $interval
                }
                if ($elapsed -ge $DrainTimeout) {
                    Write-LgWarn 'Drain timeout hit -- proceeding with restart anyway'
                }
            }
        }
    }

    # ---- Step 3: Backup ----
    if (-not $SkipBackup) {
        Write-LgBanner 'Step 3 / Backup (host pg_dump)'
        if ($DryRun) {
            Write-LgWarn "DRY-RUN: would pg_dump to $BackupDir"
        } elseif (-not (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
            Write-LgErr 'pg_dump not in PATH'
            Write-LgErr 'Install PostgreSQL client tools, or skip with -SkipBackup'
            exit 1
        } else {
            $null = New-Item -ItemType Directory -Path $BackupDir -Force
            $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
            $backupFile = Join-Path $BackupDir "leasegenie_$ts.sql"
            Write-LgInfo "Dumping ${pgHost}:${pgPort}/${pgDb} to $backupFile..."

            $env:PGPASSWORD = $pgPass
            try {
                & pg_dump -h $pgHost -p $pgPort -U $pgUser -d $pgDb --no-owner --no-privileges > $backupFile
                if ($LASTEXITCODE -ne 0) {
                    Write-LgErr 'pg_dump failed'
                    exit 1
                }
            } finally {
                Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
            }

            # Compress
            try {
                Compress-Archive -Path $backupFile -DestinationPath "$backupFile.zip" -Force
                Remove-Item $backupFile
                $finalFile = "$backupFile.zip"
            } catch {
                $finalFile = $backupFile
            }
            $sz = '{0:N1} MB' -f ((Get-Item $finalFile).Length / 1MB)
            Write-LgOk "Backup saved: $finalFile ($sz)"

            # Prune old
            $cutoff = (Get-Date).AddDays(-$RetentionDays)
            $old = Get-ChildItem -Path $BackupDir -Filter 'leasegenie_*' |
                Where-Object { $_.LastWriteTime -lt $cutoff }
            $old | Remove-Item -Force
            if ($old) {
                Write-LgOk "Pruned $($old.Count) backup(s) older than ${RetentionDays} days"
            }
        }
    }

    # ---- Step 4: Restart ----
    if (-not $SkipRestart) {
        Write-LgBanner 'Step 4 / Restart api + worker'
        if ($DryRun) {
            Write-LgWarn 'DRY-RUN: would restart api + worker'
        } else {
            foreach ($svc in @('api', 'worker')) {
                Write-LgInfo "Restarting $svc..."
                Invoke-Compose up -d --no-deps $svc | Out-Null
                if ($LASTEXITCODE -ne 0) { Write-LgWarn "$svc restart returned non-zero" }
            }
            Write-LgOk 'Restart issued'
        }
    }

    # ---- Step 5: Health check ----
    if (-not $SkipRestart) {
        Write-LgBanner 'Step 5 / Health check'
        if ($DryRun) {
            Write-LgWarn "DRY-RUN: would poll /health for ${HealthTimeout}s"
        } else {
            $apiPort = Get-EnvValue -Key 'API_PORT' -Default '8000'
            $elapsed = 0
            $interval = 5
            $apiOk = $false
            while ($elapsed -lt $HealthTimeout) {
                try {
                    $resp = Invoke-WebRequest -Uri "http://localhost:$apiPort/health" `
                        -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
                    if ($resp.StatusCode -eq 200) {
                        $apiOk = $true
                        break
                    }
                } catch { }
                Start-Sleep -Seconds $interval
                $elapsed += $interval
            }
            if ($apiOk) {
                Write-LgOk "API responding at http://localhost:$apiPort/health"
            } else {
                Write-LgErr "API did not become healthy within ${HealthTimeout}s"
                Invoke-Compose logs --tail=30 api | Out-Null
                exit 2
            }
        }
    }

    # ---- Step 6: Weekly ----
    if ($Weekly) {
        Write-LgBanner 'Step 6 / Weekly maintenance'
        if ($DryRun) {
            Write-LgWarn 'DRY-RUN: would VACUUM ANALYZE + prune audit_log'
        } elseif (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
            Write-LgWarn 'psql not in PATH -- skipping weekly tasks'
        } else {
            $env:PGPASSWORD = $pgPass
            try {
                Write-LgInfo 'Running VACUUM ANALYZE on Postgres...'
                & psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb -c 'VACUUM ANALYZE' | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-LgOk 'VACUUM ANALYZE complete'
                } else {
                    Write-LgWarn 'VACUUM ANALYZE returned non-zero'
                }

                Write-LgInfo 'Pruning audit_log entries older than 90 days...'
                $output = & psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb -t -A `
                    -c "DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '90 days' RETURNING 1" 2>$null
                $count = if ($output) { ($output -split "`n" | Where-Object { $_ -match '\S' }).Count } else { 0 }
                Write-LgOk "Pruned $count old audit_log row(s)"
            } finally {
                Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
            }
        }
    }

    Write-LgBanner 'Daily run complete'
    Write-LgOk "Finished at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-LgOk "Log: $dailyLog"
    exit 0
} finally {
    Remove-LifecycleLock
    try { Stop-Transcript | Out-Null } catch { }
}
