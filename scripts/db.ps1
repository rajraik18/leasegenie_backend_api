<#
.SYNOPSIS
    Database management wrapper (native deployment).

.DESCRIPTION
    Postgres runs on the HOST machine. This script wraps psql / pg_dump on
    the host for SQL operations, and the project's venv `python -m
    scripts.db.manage` for ORM-aware operations.

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

# ---- Read host Postgres connection details from .env ----
$dbUrl = Get-EnvValue -Key 'DATABASE_URL'
if (-not $dbUrl) {
    Stop-WithError "DATABASE_URL not found in .env. Copy .env.example to .env and fill in real values."
}

# Parse postgresql+psycopg2://USER:PASS@HOST:PORT/DB
$pgUser = ''
$pgPass = ''
$pgHost = 'localhost'
$pgPort = 5432
$pgDb   = 'leasegenie'

if ($dbUrl -match '://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)') {
    $pgUser = $matches[1]
    $pgPass = $matches[2]
    $pgHost = $matches[3]
    $pgPort = [int]$matches[4]
    $pgDb   = $matches[5]
}

if (-not $pgUser -or -not $pgPass) {
    Stop-WithError "Could not parse user / password from DATABASE_URL. Check .env."
}

# ---- Helpers ----

function Test-PostgresReachable {
    return Test-PortListening -HostName $pgHost -Port $pgPort
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
        $psqlArgs = @('-h', $pgHost, '-p', $pgPort, '-U', $pgUser, '-d', $pgDb, '-v', 'ON_ERROR_STOP=1')
        if ($SqlFile) { $psqlArgs += @('-f', $SqlFile) }
        if ($ExtraPsqlArgs) { $psqlArgs += $ExtraPsqlArgs }
        & psql @psqlArgs
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Invoke-ManagePy {
    param([string[]]$ManageArgs)
    $py = Get-VenvPython
    Write-LgInfo "Running: python -m scripts.db.manage $($ManageArgs -join ' ')"
    & $py -m scripts.db.manage @ManageArgs
}

# ---- Subcommand dispatch ----

switch ($Command.ToLower()) {

    { $_ -in 'init', 'migrate', 'upgrade' } {
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        Invoke-ManagePy -ManageArgs @($Command)
    }

    { $_ -in 'drop', 'reset' } {
        Write-LgWarn '============================================================='
        Write-LgWarn '  This will DELETE ALL DATA in the database.'
        Write-LgWarn '============================================================='
        $confirm = Read-Host "Type 'YES I AM SURE' to proceed"
        if ($confirm -ne 'YES I AM SURE') { Stop-WithError 'Aborted by user' }
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        Invoke-ManagePy -ManageArgs @($Command, '--yes')
    }

    { $_ -in 'check', 'status', 'pgvector', 'seed' } {
        if (-not (Test-PostgresReachable)) {
            Stop-WithError "Host Postgres not reachable at ${pgHost}:${pgPort}"
        }
        $allArgs = @($Command) + $ExtraArgs
        Invoke-ManagePy -ManageArgs $allArgs
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
