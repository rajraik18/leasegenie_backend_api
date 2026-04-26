<#
.SYNOPSIS
    Database management wrapper (HYBRID deployment).

.DESCRIPTION
    Postgres runs on the HOST machine. This script wraps psql on the host
    (preferred) or runs through the api container (fallback) for ORM-aware
    operations.

.PARAMETER Command
    Subcommand:
        init        Create extensions + tables (idempotent)
        migrate     Apply forward-only schema additions (via ORM)
        upgrade-sql Run pending raw-SQL migrations from scripts/db/migrations/
        drop        Drop all tables (asks confirmation)
        reset       drop + init (asks confirmation)
        sql         Run scripts/db/schema.sql directly via psql
        status      Row counts + pgvector version
        check       Verify schema matches ORM
        pgvector    Verify pgvector extension is functional
        seed        Insert demo project/property/tenant
        shell       Open psql REPL on the host
        backup      pg_dump to .\backups\leasegenie_<timestamp>.sql

.EXAMPLE
    .\scripts\db.ps1 init
    .\scripts\db.ps1 shell
    .\scripts\db.ps1 upgrade-sql
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = '',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '_Common.ps1')

if (-not $Command -or $Command -in @('-h', '--help', 'help')) {
    Get-Help $MyInvocation.MyCommand.Path -Full | Out-Host
    exit 0
}

if (-not (Test-DockerInstalled)) {
    Stop-WithError 'Docker is not installed or not in PATH'
}
$null = Get-ComposeCommand

# ---- Read host Postgres connection details from .env ----
$dbUrl = Get-EnvValue -Key 'DATABASE_URL' -Default 'postgresql+psycopg2://leasegenie:leasegenie@localhost:5432/leasegenie'

# Parse postgresql+psycopg2://USER:PASS@HOST:PORT/DB
$pgUser = 'leasegenie'
$pgPass = 'leasegenie'
$pgHost = 'localhost'
$pgPort = 5432
$pgDb   = 'leasegenie'

if ($dbUrl -match '://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)') {
    $pgUser = $matches[1]
    $pgPass = $matches[2]
    $pgHost = $matches[3]
    $pgPort = [int]$matches[4]
    $pgDb   = $matches[5]
    if ($pgHost -eq 'host.docker.internal') { $pgHost = 'localhost' }
}

# ---- Helpers ----

function Test-ApiRunning {
    $output = Invoke-ComposeQuiet ps api
    return ($output -match 'running|healthy|Up')
}

function Test-PostgresReachable {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.ConnectAsync($pgHost, $pgPort).Wait(3000) | Out-Null
        $ok = $tcp.Connected
        $tcp.Close()
        return $ok
    } catch {
        return $false
    }
}

function Test-PsqlInstalled {
    return ($null -ne (Get-Command psql -ErrorAction SilentlyContinue))
}

function Invoke-Psql {
    param(
        [string]$SqlFile = '',
        [string[]]$ExtraPsqlArgs = @()
    )
    if (-not (Test-PsqlInstalled)) {
        Stop-WithError "psql not found in PATH. Install PostgreSQL client tools."
    }
    $env:PGPASSWORD = $pgPass
    try {
        $args = @('-h', $pgHost, '-p', $pgPort, '-U', $pgUser, '-d', $pgDb, '-v', 'ON_ERROR_STOP=1')
        if ($SqlFile) { $args += @('-f', $SqlFile) }
        if ($ExtraPsqlArgs) { $args += $ExtraPsqlArgs }
        & psql @args
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Invoke-ManagePy {
    param([string[]]$ManageArgs)
    if (-not (Test-ApiRunning)) {
        Stop-WithError "API container is not running. Start it: .\scripts\start.ps1"
    }
    Write-LgInfo "Running via api container: python -m scripts.db.manage $($ManageArgs -join ' ')"
    Invoke-Compose exec -T api python -m scripts.db.manage @ManageArgs
}

# ---- Subcommand dispatch ----

switch ($Command.ToLower()) {

    { $_ -in 'init', 'migrate', 'upgrade' } {
        if (Test-ApiRunning) {
            Invoke-ManagePy -ManageArgs @($Command)
        } elseif (Test-PostgresReachable) {
            Write-LgWarn "API container not running -- falling back to direct psql for $Command"
            Write-LgInfo "Loading scripts/db/schema.sql..."
            Invoke-Psql -SqlFile 'scripts\db\schema.sql'
            if ($LASTEXITCODE -eq 0) { Write-LgOk 'Schema loaded via psql' }
        } else {
            Stop-WithError "Neither api container nor host Postgres is reachable"
        }
    }

    { $_ -in 'drop', 'reset' } {
        Write-LgWarn '============================================================='
        Write-LgWarn '  This will DELETE ALL DATA in the database.'
        Write-LgWarn '============================================================='
        $confirm = Read-Host "Type 'YES I AM SURE' to proceed"
        if ($confirm -ne 'YES I AM SURE') { Stop-WithError 'Aborted by user' }
        if (Test-ApiRunning) {
            Invoke-ManagePy -ManageArgs @($Command, '--yes')
        } else {
            Stop-WithError "API container must be running for $Command (uses ORM)"
        }
    }

    { $_ -in 'check', 'status', 'pgvector', 'seed' } {
        if (Test-ApiRunning) {
            $allArgs = @($Command) + $ExtraArgs
            Invoke-ManagePy -ManageArgs $allArgs
        } else {
            Stop-WithError "API container must be running for '$Command'. Start it: .\scripts\start.ps1"
        }
    }

    'sql' {
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        Write-LgInfo "Executing scripts\db\schema.sql against ${pgHost}:${pgPort}..."
        Invoke-Psql -SqlFile 'scripts\db\schema.sql'
        Write-LgOk 'schema.sql applied'
    }

    'upgrade-sql' {
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        $migDir = 'scripts\db\migrations'
        if (-not (Test-Path $migDir)) {
            Write-LgWarn "No migrations directory at $migDir -- nothing to do"
            exit 0
        }
        $migrations = Get-ChildItem -Path $migDir -Filter '*.sql' | Sort-Object Name
        if (-not $migrations) {
            Write-LgWarn "No .sql files in $migDir"
            exit 0
        }
        foreach ($mig in $migrations) {
            Write-LgInfo "Applying $($mig.Name)..."
            Invoke-Psql -SqlFile $mig.FullName
        }
        Write-LgOk "$($migrations.Count) migration(s) applied"
    }

    { $_ -in 'shell', 'psql' } {
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        if (-not (Test-PsqlInstalled)) {
            Stop-WithError "psql not found in PATH. Install PostgreSQL client tools."
        }
        Write-LgInfo "Opening psql shell at ${pgHost}:${pgPort}/${pgDb} (Ctrl-D to exit)..."
        $env:PGPASSWORD = $pgPass
        try {
            & psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb
        } finally {
            Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        }
    }

    'backup' {
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        if (-not (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
            Stop-WithError "pg_dump not found in PATH. Install PostgreSQL client tools."
        }
        $null = New-Item -ItemType Directory -Path 'backups' -Force
        $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
        $out = "backups\leasegenie_$ts.sql"
        Write-LgInfo "Dumping ${pgHost}:${pgPort}/${pgDb} to $out..."
        $env:PGPASSWORD = $pgPass
        try {
            & pg_dump -h $pgHost -p $pgPort -U $pgUser -d $pgDb --no-owner --no-privileges > $out
            if ($LASTEXITCODE -ne 0) { Stop-WithError 'pg_dump failed' }
        } finally {
            Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        }
        $sz = '{0:N1} MB' -f ((Get-Item $out).Length / 1MB)
        Write-LgOk "Backup saved: $out ($sz)"
    }

    default {
        Write-LgErr "Unknown command: $Command"
        Write-Host "Run 'Get-Help .\scripts\db.ps1 -Full' for usage."
        exit 1
    }
}
