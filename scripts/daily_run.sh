#!/usr/bin/env bash
# scripts/daily_run.sh
#
# Daily maintenance + restart for the LeaseGenie stack (HYBRID deployment).
# Designed for cron / systemd timer.
#
# In hybrid mode, only api+worker are containerised. Postgres runs on the
# host, so backups use host-side pg_dump. We do NOT touch host services
# (no Postgres restart, no Ollama restart) -- those are managed separately.
#
# Steps:
#   1. Pre-flight    Verify host Postgres reachable; check for in-flight jobs
#   2. Drain         Stop API; wait for worker to finish in-flight extractions
#   3. Backup        pg_dump on host -> ./backups/daily/leasegenie_<TS>.sql.gz
#                    Prune backups older than RETENTION_DAYS
#   4. Restart       Rolling restart of api + worker (host services untouched)
#   5. Health check  Wait for /health to respond
#   6. Optional weekly  VACUUM ANALYZE + audit_log prune (>90 days)
#
# Modes:
#   (none)        full daily run
#   --dry-run     show what would happen
#   --skip-backup
#   --skip-restart
#   --weekly      also VACUUM + prune audit_log
#   --force       skip pre-flight; restart even if jobs running

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

# ---- Tunables ----
RETENTION_DAYS="${LEASEGENIE_BACKUP_RETENTION_DAYS:-7}"
DRAIN_TIMEOUT="${LEASEGENIE_DRAIN_TIMEOUT:-1800}"
HEALTH_TIMEOUT="${LEASEGENIE_HEALTH_TIMEOUT:-90}"
DAILY_LOG_DIR="${LEASEGENIE_DAILY_LOG_DIR:-./logs}"
BACKUP_DIR="${LEASEGENIE_BACKUP_DIR:-./backups/daily}"

# ---- Args ----
DRY_RUN=0
SKIP_BACKUP=0
SKIP_RESTART=0
WEEKLY=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)      DRY_RUN=1; shift ;;
        --skip-backup)  SKIP_BACKUP=1; shift ;;
        --skip-restart) SKIP_RESTART=1; shift ;;
        --weekly)       WEEKLY=1; shift ;;
        --force)        FORCE=1; shift ;;
        -h|--help)
            cat <<HELP
LeaseGenie daily maintenance + restart (HYBRID).

Options:
  --dry-run        show what would happen
  --skip-backup    no pg_dump
  --skip-restart   only run backup
  --weekly         also VACUUM ANALYZE + audit_log prune (>90d)
  --force          skip pre-flight checks

Tunables (env):
  LEASEGENIE_BACKUP_RETENTION_DAYS  (default: 7)
  LEASEGENIE_DRAIN_TIMEOUT          (default: 1800 sec)
  LEASEGENIE_HEALTH_TIMEOUT         (default: 90 sec)
  LEASEGENIE_BACKUP_DIR             (default: ./backups/daily)

Cron:
  0 3 * * *   /opt/leasegenie/scripts/daily_run.sh
  0 4 * * 0   /opt/leasegenie/scripts/daily_run.sh --weekly

Exit codes:
  0  success
  1  hard failure
  2  soft failure (services unhealthy after restart)
HELP
            exit 0 ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

# ---- Daily log file ----
mkdir -p "${DAILY_LOG_DIR}" 2>/dev/null || true
DATE_STAMP=$(date +%Y-%m-%d)
DAILY_LOG="${DAILY_LOG_DIR}/daily_run.${DATE_STAMP}.log"
exec > >(tee -a "${DAILY_LOG}") 2>&1

banner "LeaseGenie daily run -- $(date '+%Y-%m-%d %H:%M:%S %Z')  (HYBRID)"
log "log file: ${DAILY_LOG}"
[[ "${DRY_RUN}" -eq 1 ]] && warn "DRY-RUN mode -- no changes will be made"

require_cmd docker
detect_compose
acquire_lock "daily_run"

# ---- Read host Postgres details ----
db_url=$(grep -E '^DATABASE_URL=' .env 2>/dev/null | cut -d= -f2- || echo "")
PG_USER=$(echo "${db_url}" | sed -E 's|.*://([^:]+):.*|\1|')
PG_PASS=$(echo "${db_url}" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
PG_HOST=$(echo "${db_url}" | sed -E 's|.*@([^:/]+).*|\1|')
PG_PORT=$(echo "${db_url}" | sed -E 's|.*@[^:]+:([0-9]+).*|\1|')
PG_DB=$(echo "${db_url}"   | sed -E 's|.*/([^?]+).*|\1|')
[[ "${PG_HOST}" == "host.docker.internal" ]] && PG_HOST="localhost"
PG_USER="${PG_USER:-leasegenie}"
PG_PASS="${PG_PASS:-leasegenie}"
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_DB="${PG_DB:-leasegenie}"

# =====================================================================
# Step 1 -- Pre-flight
# =====================================================================
banner "Step 1 / Pre-flight check"

if [[ "${FORCE}" -eq 1 ]]; then
    warn "--force set, skipping pre-flight"
else
    # Postgres must be reachable
    if ! (echo > /dev/tcp/${PG_HOST}/${PG_PORT}) >/dev/null 2>&1; then
        err "Host Postgres NOT reachable at ${PG_HOST}:${PG_PORT}"
        err "Start it via Windows Services or systemctl, then re-run"
        exit 1
    fi
    ok "Host Postgres reachable at ${PG_HOST}:${PG_PORT}"

    # Check for in-flight extraction jobs (only if we'll restart)
    if [[ "${SKIP_RESTART}" -eq 0 ]] && command -v psql >/dev/null 2>&1; then
        log "Checking for in-flight extraction jobs..."
        running=$(PGPASSWORD="${PG_PASS}" psql -h "${PG_HOST}" -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" -t -A \
            -c "SELECT COUNT(*) FROM extraction_jobs WHERE status='running'" 2>/dev/null \
            | tr -d ' \n' || echo 0)
        running=${running:-0}
        if [[ "${running}" -gt 0 ]]; then
            warn "${running} extraction job(s) currently running"
            warn "Will drain (waiting up to ${DRAIN_TIMEOUT}s) before restart"
        else
            ok "No jobs running"
        fi
    fi
fi

# =====================================================================
# Step 2 -- Drain
# =====================================================================
if [[ "${SKIP_RESTART}" -eq 0 ]]; then
    banner "Step 2 / Drain (graceful stop of API)"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        warn "DRY-RUN: would stop api container, then wait for worker"
    else
        log "Stopping API (no new requests accepted)..."
        ${COMPOSE} stop -t 30 api || warn "api stop returned non-zero"
        ok "API stopped"

        if [[ "${FORCE}" -eq 0 ]] && command -v psql >/dev/null 2>&1; then
            elapsed=0
            interval=15
            while (( elapsed < DRAIN_TIMEOUT )); do
                running=$(PGPASSWORD="${PG_PASS}" psql -h "${PG_HOST}" -p "${PG_PORT}" \
                    -U "${PG_USER}" -d "${PG_DB}" -t -A \
                    -c "SELECT COUNT(*) FROM extraction_jobs WHERE status='running'" 2>/dev/null \
                    | tr -d ' \n' || echo 0)
                running=${running:-0}

                if [[ "${running}" -eq 0 ]]; then
                    ok "All worker jobs drained"
                    break
                fi
                log "Waiting for ${running} job(s)... (${elapsed}s / ${DRAIN_TIMEOUT}s)"
                sleep "${interval}"
                elapsed=$(( elapsed + interval ))
            done

            if (( elapsed >= DRAIN_TIMEOUT )); then
                warn "Drain timeout hit -- proceeding with restart anyway"
            fi
        fi
    fi
fi

# =====================================================================
# Step 3 -- Backup (host pg_dump)
# =====================================================================
if [[ "${SKIP_BACKUP}" -eq 0 ]]; then
    banner "Step 3 / Backup (host pg_dump)"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        warn "DRY-RUN: would pg_dump to ${BACKUP_DIR}/"
    elif ! command -v pg_dump >/dev/null 2>&1; then
        err "pg_dump not found in PATH"
        err "Install PostgreSQL client tools, or skip backup with --skip-backup"
        exit 1
    else
        mkdir -p "${BACKUP_DIR}"
        TS=$(date +%Y%m%d_%H%M%S)
        BACKUP_FILE="${BACKUP_DIR}/leasegenie_${TS}.sql.gz"
        log "Dumping ${PG_HOST}:${PG_PORT}/${PG_DB} to ${BACKUP_FILE}..."

        if PGPASSWORD="${PG_PASS}" pg_dump \
                -h "${PG_HOST}" -p "${PG_PORT}" \
                -U "${PG_USER}" -d "${PG_DB}" \
                --no-owner --no-privileges \
            | gzip > "${BACKUP_FILE}"; then
            sz=$(du -h "${BACKUP_FILE}" | cut -f1)
            ok "Backup saved: ${BACKUP_FILE} (${sz})"
        else
            err "pg_dump failed"
            exit 1
        fi

        # Prune old
        pruned=$(find "${BACKUP_DIR}" -name "leasegenie_*.sql.gz" -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null | wc -l)
        if [[ "${pruned}" -gt 0 ]]; then
            ok "Pruned ${pruned} backup(s) older than ${RETENTION_DAYS} days"
        fi
    fi
fi

# =====================================================================
# Step 4 -- Restart (api + worker only; host services untouched)
# =====================================================================
if [[ "${SKIP_RESTART}" -eq 0 ]]; then
    banner "Step 4 / Restart api + worker"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        warn "DRY-RUN: would restart api + worker"
    else
        for svc in api worker; do
            log "Restarting ${svc}..."
            ${COMPOSE} up -d --no-deps "${svc}" || warn "${svc} restart returned non-zero"
        done
        ok "Restart issued"
    fi
fi

# =====================================================================
# Step 5 -- Health check
# =====================================================================
if [[ "${SKIP_RESTART}" -eq 0 ]]; then
    banner "Step 5 / Health check"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        warn "DRY-RUN: would poll /health for ${HEALTH_TIMEOUT}s"
    else
        api_port=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8000)
        api_port=${api_port:-8000}

        elapsed=0
        interval=5
        api_ok=0
        while (( elapsed < HEALTH_TIMEOUT )); do
            if curl -fsS --max-time 3 "http://localhost:${api_port}/health" >/dev/null 2>&1; then
                api_ok=1
                break
            fi
            sleep "${interval}"
            elapsed=$(( elapsed + interval ))
        done

        if [[ "${api_ok}" -eq 1 ]]; then
            ok "API responding at http://localhost:${api_port}/health"
        else
            err "API did not become healthy within ${HEALTH_TIMEOUT}s"
            ${COMPOSE} logs --tail=30 api || true
            exit 2
        fi
    fi
fi

# =====================================================================
# Step 6 -- Weekly tasks
# =====================================================================
if [[ "${WEEKLY}" -eq 1 ]]; then
    banner "Step 6 / Weekly maintenance"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        warn "DRY-RUN: would VACUUM ANALYZE + prune audit_log"
    elif ! command -v psql >/dev/null 2>&1; then
        warn "psql not in PATH -- skipping weekly tasks"
    else
        log "Running VACUUM ANALYZE on Postgres..."
        PGPASSWORD="${PG_PASS}" psql -h "${PG_HOST}" -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" -c "VACUUM ANALYZE" >/dev/null 2>&1 \
            && ok "VACUUM ANALYZE complete" \
            || warn "VACUUM ANALYZE returned non-zero"

        log "Pruning audit_log entries older than 90 days..."
        pruned=$(PGPASSWORD="${PG_PASS}" psql -h "${PG_HOST}" -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" -t -A \
            -c "DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '90 days' RETURNING 1" \
            2>/dev/null | wc -l)
        ok "Pruned ${pruned:-0} old audit_log row(s)"
    fi
fi

banner "Daily run complete"
ok "Finished at $(date '+%Y-%m-%d %H:%M:%S %Z')"
ok "Log: ${DAILY_LOG}"
exit 0
