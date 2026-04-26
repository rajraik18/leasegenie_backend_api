#!/usr/bin/env bash
# scripts/stop.sh
#
# Stop the LeaseGenie stack gracefully (HYBRID deployment).
#
# Only api+worker run in containers; this script does NOT touch your
# local Postgres, Redis, or Ollama installs.
#
# Order: drain api first (stop accepting requests), then worker (let it
# finish in-flight extractions).
#
# Modes:
#   (no args)   Graceful stop. Containers preserved. Volumes preserved.
#   --down      docker compose down — removes containers, keeps volumes.
#   --clean     Removes containers AND the api_data volume (uploaded PDFs lost).
#   --force     Skip the graceful drain — SIGKILL after 10s.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

DO_DOWN=0
DO_CLEAN=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --down)   DO_DOWN=1; shift ;;
        --clean)  DO_DOWN=1; DO_CLEAN=1; shift ;;
        --force)  FORCE=1; shift ;;
        -h|--help)
            sed -n '2,/^set/p' "$0" | sed -n '/^# /p' | sed 's/^# \?//'
            exit 0 ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

GRACE_API=$([[ "${FORCE}" -eq 1 ]] && echo 5 || echo 20)
GRACE_WORKER=$([[ "${FORCE}" -eq 1 ]] && echo 5 || echo 60)

banner "LeaseGenie API -- stop (hybrid deployment)"

require_cmd docker
detect_compose
acquire_lock "stop"

log "Current state:"
${COMPOSE} ps
echo

if [[ "${DO_CLEAN}" -eq 1 ]]; then
    warn "============================================================="
    warn "  --clean will DELETE the api_data volume:"
    warn "    - uploaded PDFs in /data/uploads"
    warn "    - generated Excel exports in /data/exports"
    warn ""
    warn "  Your local Postgres, Redis, and Ollama data are NOT touched."
    warn "============================================================="
    read -r -p "Type 'YES I AM SURE' to proceed: " confirm
    [[ "${confirm}" == "YES I AM SURE" ]] || die "Aborted by user"
fi

# Phase 1: Drain API
banner "Phase 1 -- draining API (stop accepting new requests)"
graceful_stop api "${GRACE_API}"
ok "API drained"

# Phase 2: Stop workers (let in-flight extractions finish)
banner "Phase 2 -- stopping worker"
graceful_stop worker "${GRACE_WORKER}"
ok "Worker stopped"

# Optional: remove containers
if [[ "${DO_DOWN}" -eq 1 ]]; then
    banner "Removing containers"
    if [[ "${DO_CLEAN}" -eq 1 ]]; then
        ${COMPOSE} down -v --remove-orphans
        ok "Containers and api_data volume removed"
    else
        ${COMPOSE} down --remove-orphans
        ok "Containers removed (api_data volume preserved)"
    fi
fi

banner "Stopped"
if [[ "${DO_CLEAN}" -eq 1 ]]; then
    ok "Containers and api_data volume cleaned. Local infra untouched."
elif [[ "${DO_DOWN}" -eq 1 ]]; then
    ok "Containers removed; api_data volume preserved."
else
    ok "Containers stopped (preserved). Restart with 'scripts/start.sh'."
fi
ok "Note: your local Postgres, Redis, and Ollama remain running."
