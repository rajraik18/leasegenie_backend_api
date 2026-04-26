#!/usr/bin/env bash
# scripts/status.sh
#
# One-screen summary of the LeaseGenie stack (HYBRID deployment).
# Shows Docker containers (api, worker) plus reachability of host services
# (postgres, redis, ollama).
#
# Usage:
#   scripts/status.sh
#   scripts/status.sh --watch    # auto-refresh every 5s

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

WATCH=0
[[ "${1:-}" == "--watch" ]] && WATCH=1

require_cmd docker
detect_compose

show_status() {
    [[ "${WATCH}" -eq 1 ]] && clear
    banner "LeaseGenie Stack -- $(date '+%Y-%m-%d %H:%M:%S')  (HYBRID)"

    echo
    log "Docker containers:"
    ${COMPOSE} ps 2>/dev/null || warn "  compose not running"

    echo
    log "Docker volumes:"
    docker volume ls --filter "name=leasegenie" --format "table {{.Name}}\t{{.Driver}}" 2>/dev/null

    # ---- Host services ----
    echo
    log "Host services:"

    # Read connection details from .env
    db_url=$(grep -E '^DATABASE_URL=' .env 2>/dev/null | cut -d= -f2- || echo "")
    pg_host=$(echo "${db_url}" | sed -E 's|.*@([^:/]+).*|\1|')
    pg_port=$(echo "${db_url}" | sed -E 's|.*@[^:]+:([0-9]+).*|\1|')
    [[ "${pg_host}" == "host.docker.internal" ]] && pg_host="localhost"
    pg_host="${pg_host:-localhost}"
    pg_port="${pg_port:-5432}"

    redis_url=$(grep -E '^CELERY_BROKER_URL=' .env 2>/dev/null | cut -d= -f2- || echo "")
    redis_host=$(echo "${redis_url}" | sed -E 's|redis://([^:/]+).*|\1|')
    redis_port=$(echo "${redis_url}" | sed -E 's|.*://[^:]+:([0-9]+).*|\1|')
    [[ "${redis_host}" == "host.docker.internal" ]] && redis_host="localhost"
    redis_host="${redis_host:-localhost}"
    redis_port="${redis_port:-6379}"

    ollama_url=$(grep -E '^OLLAMA_BASE_URL=' .env 2>/dev/null | cut -d= -f2- || echo "http://localhost:11434")
    ollama_check=$(echo "${ollama_url}" | sed 's|host.docker.internal|localhost|')

    if (echo > /dev/tcp/${pg_host}/${pg_port}) >/dev/null 2>&1; then
        ok "  Postgres at ${pg_host}:${pg_port}: REACHABLE"
    else
        warn "  Postgres at ${pg_host}:${pg_port}: NOT reachable"
    fi

    if (echo > /dev/tcp/${redis_host}/${redis_port}) >/dev/null 2>&1; then
        ok "  Redis at ${redis_host}:${redis_port}: REACHABLE"
    else
        warn "  Redis at ${redis_host}:${redis_port}: NOT reachable"
    fi

    if curl -fsS --max-time 3 "${ollama_check}/api/tags" >/dev/null 2>&1; then
        ok "  Ollama at ${ollama_check}: REACHABLE"
        models=$(curl -fsS --max-time 3 "${ollama_check}/api/tags" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'    {m[\"name\"]}') for m in d.get('models', [])]" 2>/dev/null)
        if [[ -n "${models}" ]]; then
            log "  Pulled models:"
            echo "${models}"
        fi
    else
        warn "  Ollama at ${ollama_check}: NOT reachable"
    fi

    # ---- API endpoint ----
    echo
    log "API endpoint:"
    api_port=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8000)
    api_port=${api_port:-8000}
    if curl -fsS --max-time 3 "http://localhost:${api_port}/health" >/dev/null 2>&1; then
        ok "  http://localhost:${api_port}/health responding"
    else
        warn "  http://localhost:${api_port}/health NOT responding"
    fi
}

if [[ "${WATCH}" -eq 1 ]]; then
    while true; do
        show_status
        echo
        echo "(refreshing every 5s -- Ctrl-C to stop)"
        sleep 5
    done
else
    show_status
fi
