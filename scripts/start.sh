#!/usr/bin/env bash
# scripts/start.sh
#
# Start the LeaseGenie stack (HYBRID deployment).
#
# In this layout, only api+worker run in Docker. Postgres, Redis, and Ollama
# run as native installs on the host machine and must be running BEFORE this
# script is invoked.
#
# Modes:
#   --cold    First-time start. Verifies host infra and required Ollama
#             models are available.
#   --warm    Default. Quick start, fewer pre-flight checks.
#   --build   Rebuild API/worker images before starting.
#   --skip-checks   Skip the host-infra reachability checks.
#
# Usage:
#   scripts/start.sh                  # warm start
#   scripts/start.sh --cold           # first-time bring-up with full checks
#   scripts/start.sh --build          # after code changes

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

# ---- Argument parsing ----
MODE="warm"
DO_BUILD=0
SKIP_CHECKS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cold)         MODE="cold"; shift ;;
        --warm)         MODE="warm"; shift ;;
        --build)        DO_BUILD=1; shift ;;
        --skip-checks)  SKIP_CHECKS=1; shift ;;
        -h|--help)
            sed -n '2,/^set/p' "$0" | sed -n '/^# /p' | sed 's/^# \?//'
            exit 0 ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

banner "LeaseGenie API -- start (${MODE} mode, hybrid deployment)"

# ---- Pre-flight ----
require_cmd docker
detect_compose
ensure_env
acquire_lock "start"

# ---- Phase 1: Verify host infrastructure ----
if [[ "${SKIP_CHECKS}" -eq 0 ]]; then
    banner "Phase 1 -- verifying host infrastructure"

    # Read host details from .env
    pg_host_port=$(grep -E '^DATABASE_URL=' .env 2>/dev/null | sed -E 's/.*@([^/]+):([0-9]+).*/\1 \2/' || true)
    pg_host=$(echo "${pg_host_port}" | awk '{print $1}')
    pg_port=$(echo "${pg_host_port}" | awk '{print $2}')
    # When checking from the bash script (running on the host), localhost
    # is what we want regardless of what host.docker.internal would mean
    # to a container.
    if [[ "${pg_host}" == "host.docker.internal" ]]; then pg_host="localhost"; fi
    pg_host="${pg_host:-localhost}"
    pg_port="${pg_port:-5432}"

    redis_host_port=$(grep -E '^CELERY_BROKER_URL=' .env 2>/dev/null | sed -E 's|redis://([^/:]+):([0-9]+).*|\1 \2|' || true)
    redis_host=$(echo "${redis_host_port}" | awk '{print $1}')
    redis_port=$(echo "${redis_host_port}" | awk '{print $2}')
    if [[ "${redis_host}" == "host.docker.internal" ]]; then redis_host="localhost"; fi
    redis_host="${redis_host:-localhost}"
    redis_port="${redis_port:-6379}"

    ollama_url=$(grep -E '^OLLAMA_BASE_URL=' .env 2>/dev/null | cut -d= -f2 || echo "http://localhost:11434")
    ollama_check_url=$(echo "${ollama_url}" | sed 's|host.docker.internal|localhost|')

    log "Checking Postgres at ${pg_host}:${pg_port} ..."
    if (echo > /dev/tcp/${pg_host}/${pg_port}) >/dev/null 2>&1; then
        ok "Postgres is reachable"
    else
        err "Postgres is NOT reachable at ${pg_host}:${pg_port}"
        err "Make sure your local Postgres is running and listening on this port."
        err "See DEPLOYMENT.md section 'Hybrid setup' for configuration steps."
        exit 1
    fi

    log "Checking Redis at ${redis_host}:${redis_port} ..."
    if (echo > /dev/tcp/${redis_host}/${redis_port}) >/dev/null 2>&1; then
        ok "Redis is reachable"
    else
        err "Redis is NOT reachable at ${redis_host}:${redis_port}"
        err "Start your local Redis: redis-server (or via the Windows service)"
        exit 1
    fi

    log "Checking Ollama at ${ollama_check_url} ..."
    if curl -fsS --max-time 3 "${ollama_check_url}/api/tags" >/dev/null 2>&1; then
        ok "Ollama is reachable"

        if [[ "${MODE}" == "cold" ]]; then
            llm_model=$(grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2 || echo "qwen2.5:14b-instruct-q5_K_M")
            embed_model=$(grep -E '^OLLAMA_EMBED_MODEL=' .env 2>/dev/null | cut -d= -f2 || echo "nomic-embed-text")
            tags=$(curl -fsS --max-time 5 "${ollama_check_url}/api/tags" 2>/dev/null || echo "")
            llm_key="${llm_model%%:*}"
            embed_key="${embed_model%%:*}"
            if echo "${tags}" | grep -q "\"${llm_key}"; then
                ok "Required model present: ${llm_model}"
            else
                warn "Model ${llm_model} not pulled yet"
                warn "Run on the host: ollama pull ${llm_model}"
            fi
            if echo "${tags}" | grep -q "\"${embed_key}"; then
                ok "Required embed model present: ${embed_model}"
            else
                warn "Embed model ${embed_model} not pulled yet"
                warn "Run on the host: ollama pull ${embed_model}"
            fi
        fi
    else
        err "Ollama is NOT reachable at ${ollama_check_url}"
        err "Start it on the host (Windows: launch 'Ollama' from Start menu)"
        err "Make sure OLLAMA_HOST=0.0.0.0 is set so containers can reach it"
        exit 1
    fi
fi

# ---- Phase 2: Optional rebuild ----
if [[ "${DO_BUILD}" -eq 1 ]]; then
    banner "Phase 2 -- rebuilding application images"
    ${COMPOSE} build api worker || die "Image build failed"
    ok "Build complete"
fi

# ---- Phase 3: Start application services ----
banner "Phase 3 -- starting api + worker"
${COMPOSE} up -d api worker || die "Failed to start api/worker"
wait_for_health api 60 || warn "api did not report healthy -- check logs"

# ---- Final status ----
banner "Status"
${COMPOSE} ps

# Smoke test
api_port=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8000)
api_port=${api_port:-8000}
echo
log "Smoke-testing http://localhost:${api_port}/health ..."
if curl -fsS --max-time 5 "http://localhost:${api_port}/health" >/dev/null 2>&1; then
    ok "API responding at http://localhost:${api_port}"
    ok "Docs: http://localhost:${api_port}/docs"
else
    warn "API not yet responding at http://localhost:${api_port}/health"
    warn "Run 'scripts/logs.sh api' to inspect"
fi

banner "Started"
ok "LeaseGenie stack is up (${MODE} mode, hybrid)"
