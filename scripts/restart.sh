#!/usr/bin/env bash
# scripts/restart.sh
#
# Restart the LeaseGenie containers (HYBRID deployment).
#
# Only api+worker run in Docker. Postgres, Redis, Ollama are local installs
# on the host and are NOT restarted by this script. Manage them separately
# via Windows Services (postgresql-x64-16, Redis) or by relaunching Ollama.
#
# Strategies:
#   --rolling  (Default) Restart api+worker via `docker compose restart`. ~15s.
#              Use after code changes to .env or playbook JSON files.
#   --full     Stop + up. ~30s. Use after docker-compose.yml changes.
#   --hard     Down + up. ~60s. Use after Dockerfile changes.
#
#   --build    Rebuild images first (combinable with any strategy).
#
# Usage:
#   scripts/restart.sh                 # rolling restart
#   scripts/restart.sh --build         # rebuild + rolling restart
#   scripts/restart.sh --hard          # full container recreation

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

STRATEGY="rolling"
DO_BUILD=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rolling) STRATEGY="rolling"; shift ;;
        --full)    STRATEGY="full"; shift ;;
        --hard)    STRATEGY="hard"; shift ;;
        --build)   DO_BUILD=1; shift ;;
        -h|--help)
            sed -n '2,/^set/p' "$0" | sed -n '/^# /p' | sed 's/^# \?//'
            exit 0 ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

banner "LeaseGenie API -- restart (strategy=${STRATEGY}, hybrid)"

require_cmd docker
detect_compose
ensure_env
acquire_lock "restart"

TARGETS=(api worker)
log "Targets: ${TARGETS[*]}"

# Optional rebuild
if [[ "${DO_BUILD}" -eq 1 ]]; then
    banner "Rebuilding images"
    ${COMPOSE} build "${TARGETS[@]}" || die "Build failed"
    ok "Rebuilt: ${TARGETS[*]}"
fi

# Apply strategy
case "${STRATEGY}" in
    rolling)
        banner "Rolling restart"
        for svc in "${TARGETS[@]}"; do
            log "Restarting ${svc}..."
            ${COMPOSE} restart "${svc}" || warn "${svc} restart returned non-zero"
        done
        ;;
    full)
        banner "Full restart"
        log "Stopping target services..."
        for svc in "${TARGETS[@]}"; do
            graceful_stop "${svc}" 30
        done
        log "Starting target services..."
        ${COMPOSE} up -d "${TARGETS[@]}" || die "Failed to start services"
        ;;
    hard)
        banner "Hard restart (container recreation)"
        log "Recreating target services..."
        ${COMPOSE} up -d --force-recreate "${TARGETS[@]}" || die "Recreate failed"
        ;;
esac

# Wait for health
banner "Verifying"
all_healthy=1
for svc in "${TARGETS[@]}"; do
    if ! wait_for_health "${svc}" 60; then
        all_healthy=0
    fi
done

banner "Status"
${COMPOSE} ps

# Smoke test
api_port=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8000)
api_port=${api_port:-8000}
echo
log "Smoke-testing http://localhost:${api_port}/health ..."
if curl -fsS --max-time 5 "http://localhost:${api_port}/health" >/dev/null 2>&1; then
    ok "API responding at http://localhost:${api_port}"
else
    warn "API not responding yet (may still be booting)"
fi

if [[ "${all_healthy}" -eq 1 ]]; then
    banner "Restarted"
    ok "All target services are healthy"
else
    banner "Restarted (with warnings)"
    warn "Some services did not pass health checks -- check logs"
    exit 2
fi
