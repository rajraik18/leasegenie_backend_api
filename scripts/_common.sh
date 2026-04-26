#!/usr/bin/env bash
# scripts/_common.sh
# Shared helpers for start/stop/restart scripts. Sourced, not executed.

set -uo pipefail

# Resolve project root regardless of where the script is called from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# ---- Colours (auto-disable if not a TTY) ----
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'
    C_GREEN=$'\033[0;32m'
    C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

log()    { printf '%s[%s]%s %s\n' "${C_BLUE}"  "$(date +%H:%M:%S)" "${C_RESET}" "$*"; }
ok()     { printf '%s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn()   { printf '%s⚠%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"; }
err()    { printf '%s✗%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; }
banner() { printf '\n%s%s── %s ──%s\n' "${C_BOLD}" "${C_BLUE}" "$*" "${C_RESET}"; }

die() { err "$*"; exit 1; }

# ---- Service names from docker-compose.yml ----
# HYBRID deployment: only api+worker run in Docker. The infrastructure
# (postgres, redis, ollama) runs natively on the host machine.
ALL_SERVICES=(api worker)
INFRA_SERVICES=()       # empty - infra is on the host now
APP_SERVICES=(api worker)

# ---- Required tooling ----
require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

# Pick the right Compose binary. New installs use `docker compose`; older
# environments still ship `docker-compose` standalone. Either is fine.
detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        die "Neither 'docker compose' nor 'docker-compose' is installed"
    fi
}

# ---- .env handling ----
ensure_env() {
    if [[ ! -f .env ]]; then
        if [[ -f .env.example ]]; then
            warn ".env not found — copying from .env.example"
            cp .env.example .env
            warn "Edit .env before running in production (passwords, secrets)"
        else
            die ".env file is missing and no .env.example found"
        fi
    fi
}

# ---- Health checks ----
# Wait for a single service to report healthy. Bounded by timeout (seconds).
wait_for_health() {
    local service=$1
    local timeout=${2:-120}
    local elapsed=0
    local interval=3

    log "Waiting for ${C_BOLD}${service}${C_RESET} to become healthy (timeout ${timeout}s)..."
    while (( elapsed < timeout )); do
        local status
        status=$(${COMPOSE} ps --format json "${service}" 2>/dev/null \
            | python3 -c "import sys, json
try:
    for line in sys.stdin:
        line=line.strip()
        if not line: continue
        d=json.loads(line)
        print(d.get('Health') or d.get('State') or 'unknown')
        break
except Exception:
    print('unknown')" 2>/dev/null || echo unknown)

        case "${status}" in
            healthy)  ok "${service} is healthy"; return 0 ;;
            running)  ok "${service} is running (no healthcheck declared)"; return 0 ;;
            unhealthy) err "${service} reports UNHEALTHY"; ${COMPOSE} logs --tail=30 "${service}"; return 1 ;;
        esac
        sleep "${interval}"
        elapsed=$(( elapsed + interval ))
    done
    err "${service} did not become healthy within ${timeout}s"
    ${COMPOSE} logs --tail=30 "${service}" || true
    return 1
}

# ---- Ollama model warm-up ----
ensure_ollama_model() {
    local model=${1:-qwen2.5:32b}
    local embed_model=${2:-nomic-embed-text}

    log "Checking Ollama models..."
    local existing
    existing=$(${COMPOSE} exec -T ollama ollama list 2>/dev/null || true)

    if echo "${existing}" | grep -q "^${model%%:*}"; then
        ok "Model ${model} already pulled"
    else
        warn "Model ${model} not present — pulling (this may take 10-30 minutes for ~20GB)"
        ${COMPOSE} exec -T ollama ollama pull "${model}" \
            || { err "Ollama pull failed for ${model}"; return 1; }
        ok "Pulled ${model}"
    fi

    if echo "${existing}" | grep -q "^${embed_model%%:*}"; then
        ok "Embed model ${embed_model} already pulled"
    else
        warn "Pulling embedding model ${embed_model} (small, ~270MB)..."
        ${COMPOSE} exec -T ollama ollama pull "${embed_model}" \
            || { err "Ollama pull failed for ${embed_model}"; return 1; }
        ok "Pulled ${embed_model}"
    fi
}

# ---- Stop helpers ----
graceful_stop() {
    local service=$1
    local timeout=${2:-30}
    log "Stopping ${service} gracefully (SIGTERM, ${timeout}s grace)..."
    ${COMPOSE} stop -t "${timeout}" "${service}" || true
}

# ---- Lock file (prevents concurrent start/stop) ----
LOCK_FILE="${PROJECT_ROOT}/.lifecycle.lock"

acquire_lock() {
    local action=$1
    if [[ -f "${LOCK_FILE}" ]]; then
        local pid
        pid=$(cat "${LOCK_FILE}" 2>/dev/null || echo "")
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            die "Another lifecycle script is running (PID ${pid}). If this is wrong, delete ${LOCK_FILE}"
        fi
        warn "Stale lock file found — removing"
        rm -f "${LOCK_FILE}"
    fi
    echo "$$" > "${LOCK_FILE}"
    # shellcheck disable=SC2064
    trap "rm -f '${LOCK_FILE}'" EXIT
    log "Acquired lifecycle lock for: ${action}"
}
