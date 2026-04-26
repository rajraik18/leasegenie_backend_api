#!/usr/bin/env bash
# scripts/db.sh
#
# Database management (HYBRID deployment).
#
# Postgres runs on the HOST machine, not in Docker. This script wraps
# psql on the host (preferred) or runs through the api container
# (fallback) for ORM-aware operations.
#
# Subcommands:
#   init         Create extensions + tables (idempotent)
#   migrate      Apply forward-only schema additions (via ORM in api container)
#   upgrade-sql  Run pending raw-SQL migrations from scripts/db/migrations/
#   drop         Drop all tables (asks confirmation)
#   reset        drop + init (asks confirmation)
#   sql          Run scripts/db/schema.sql directly via psql
#   status       Row counts + pgvector version (needs api container)
#   check        Verify schema matches ORM (needs api container)
#   pgvector     Verify pgvector extension is functional
#   seed         Insert demo project/property/tenant (needs api container)
#   shell        Open psql REPL on the host
#   backup       pg_dump to ./backups/leasegenie_<timestamp>.sql
#
# Usage:
#   scripts/db.sh init
#   scripts/db.sh shell
#   scripts/db.sh upgrade-sql

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

CMD="${1:-}"
shift || true

if [[ -z "${CMD}" || "${CMD}" == "-h" || "${CMD}" == "--help" ]]; then
    sed -n '2,/^set/p' "$0" | sed -n '/^# /p' | sed 's/^# \?//'
    exit 0
fi

require_cmd docker
detect_compose

# ---- Read host Postgres connection details from .env ----
db_url=$(grep -E '^DATABASE_URL=' .env 2>/dev/null | cut -d= -f2- || echo "")
if [[ -z "${db_url}" ]]; then
    echo "ERROR: DATABASE_URL not found in .env — refusing to fall back to default credentials." >&2
    echo "Copy .env.example to .env and fill in real values before running this script." >&2
    exit 1
fi

# Parse user / pass / host / port / db from URL
# Format: postgresql+psycopg2://USER:PASS@HOST:PORT/DB
PG_USER=$(echo "${db_url}" | sed -E 's|.*://([^:]+):.*|\1|')
PG_PASS=$(echo "${db_url}" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
PG_HOST=$(echo "${db_url}" | sed -E 's|.*@([^:/]+).*|\1|')
PG_PORT=$(echo "${db_url}" | sed -E 's|.*@[^:]+:([0-9]+).*|\1|')
PG_DB=$(echo "${db_url}"   | sed -E 's|.*/([^?]+).*|\1|')

# When this script runs ON the host, host.docker.internal -> localhost
if [[ "${PG_HOST}" == "host.docker.internal" ]]; then PG_HOST="localhost"; fi

# Sanity defaults — only sanitise host/port/db, NOT user/pass.
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_DB="${PG_DB:-leasegenie}"

if [[ -z "${PG_USER}" || -z "${PG_PASS}" ]]; then
    echo "ERROR: could not parse user / password from DATABASE_URL — refusing to use defaults." >&2
    exit 1
fi

# ---- Helpers ----

api_running() {
    ${COMPOSE} ps api 2>/dev/null | grep -qE "running|healthy|Up"
}

postgres_reachable() {
    (echo > /dev/tcp/${PG_HOST}/${PG_PORT}) >/dev/null 2>&1
}

run_via_psql() {
    # Stream stdin to psql on the host. Requires psql installed locally.
    if ! command -v psql >/dev/null 2>&1; then
        die "psql not found in PATH. Install PostgreSQL client tools or use 'scripts/db.sh shell' inside Docker."
    fi
    PGPASSWORD="${PG_PASS}" psql \
        -h "${PG_HOST}" -p "${PG_PORT}" \
        -U "${PG_USER}" -d "${PG_DB}" \
        -v ON_ERROR_STOP=1 "$@"
}

run_manage_py() {
    # Run the ORM-aware management CLI inside the api container.
    if ! api_running; then
        die "API container is not running. Start it: scripts/start.sh"
    fi
    log "Running via api container: python -m scripts.db.manage $*"
    ${COMPOSE} exec -T api python -m scripts.db.manage "$@"
}

# ---- Subcommand dispatch ----

case "${CMD}" in
    init|migrate|upgrade)
        if api_running; then
            run_manage_py "${CMD}"
        elif postgres_reachable; then
            warn "API container not running -- falling back to direct psql for ${CMD}"
            log "Loading scripts/db/schema.sql..."
            run_via_psql -f scripts/db/schema.sql
            ok "Schema loaded via psql"
        else
            die "Neither api nor host Postgres is reachable. Check both."
        fi
        ;;

    drop|reset)
        warn "============================================================="
        warn "  This will DELETE ALL DATA in the database."
        warn "============================================================="
        read -r -p "Type 'YES I AM SURE' to proceed: " confirm
        [[ "${confirm}" == "YES I AM SURE" ]] || die "Aborted by user"
        if api_running; then
            run_manage_py "${CMD}" --yes
        else
            die "API container must be running for ${CMD} (uses ORM)"
        fi
        ;;

    check|status|pgvector|seed)
        if api_running; then
            run_manage_py "${CMD}" "$@"
        else
            die "API container must be running for '${CMD}'. Start it: scripts/start.sh"
        fi
        ;;

    sql)
        if ! postgres_reachable; then
            die "Host Postgres not reachable at ${PG_HOST}:${PG_PORT}. See DEPLOYMENT.md."
        fi
        log "Executing scripts/db/schema.sql against ${PG_HOST}:${PG_PORT}..."
        run_via_psql -f scripts/db/schema.sql
        ok "schema.sql applied"
        ;;

    upgrade-sql)
        if ! postgres_reachable; then
            die "Host Postgres not reachable at ${PG_HOST}:${PG_PORT}."
        fi
        migrations_dir="scripts/db/migrations"
        if [[ ! -d "${migrations_dir}" ]]; then
            warn "No migrations directory at ${migrations_dir} -- nothing to do"
            exit 0
        fi
        count=0
        for mig in $(ls "${migrations_dir}"/*.sql 2>/dev/null | sort); do
            log "Applying $(basename ${mig})..."
            run_via_psql -f "${mig}"
            count=$((count + 1))
        done
        if [[ ${count} -eq 0 ]]; then
            warn "No .sql files in ${migrations_dir}"
        else
            ok "${count} migration(s) applied"
        fi
        ;;

    shell|psql)
        if ! postgres_reachable; then
            die "Host Postgres not reachable at ${PG_HOST}:${PG_PORT}."
        fi
        if ! command -v psql >/dev/null 2>&1; then
            die "psql not found in PATH. Install PostgreSQL client tools."
        fi
        log "Opening psql shell at ${PG_HOST}:${PG_PORT}/${PG_DB} (Ctrl-D to exit)..."
        PGPASSWORD="${PG_PASS}" psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}"
        ;;

    backup)
        if ! postgres_reachable; then
            die "Host Postgres not reachable at ${PG_HOST}:${PG_PORT}."
        fi
        if ! command -v pg_dump >/dev/null 2>&1; then
            die "pg_dump not found in PATH. Install PostgreSQL client tools."
        fi
        mkdir -p backups
        ts=$(date +%Y%m%d_%H%M%S)
        out="backups/leasegenie_${ts}.sql"
        log "Dumping ${PG_HOST}:${PG_PORT}/${PG_DB} to ${out}..."
        PGPASSWORD="${PG_PASS}" pg_dump \
            -h "${PG_HOST}" -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" \
            --no-owner --no-privileges > "${out}" \
            || die "pg_dump failed"
        sz=$(du -h "${out}" | cut -f1)
        ok "Backup saved: ${out} (${sz})"
        ;;

    *)
        err "Unknown command: ${CMD}"
        echo "Run '${0} --help' for usage."
        exit 1 ;;
esac
