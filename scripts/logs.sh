#!/usr/bin/env bash
# scripts/logs.sh
#
# Tail logs from one or all services.
#
# Usage:
#   scripts/logs.sh              # all services, follow
#   scripts/logs.sh api          # just api, follow
#   scripts/logs.sh api 200      # last 200 lines, then follow
#   scripts/logs.sh --no-follow  # last 100 lines of all services, exit

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

FOLLOW=1
SERVICE=""
TAIL=100

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-follow) FOLLOW=0; shift ;;
        -h|--help)
            sed -n '2,/^set/p' "$0" | sed -n '/^# /p' | sed 's/^# \?//'
            exit 0 ;;
        *)
            if [[ -z "${SERVICE}" ]]; then
                SERVICE="$1"
            elif [[ "$1" =~ ^[0-9]+$ ]]; then
                TAIL="$1"
            else
                die "Unknown argument: $1"
            fi
            shift ;;
    esac
done

require_cmd docker
detect_compose

# Validate service name if given
if [[ -n "${SERVICE}" ]]; then
    valid=0
    for s in "${ALL_SERVICES[@]}"; do
        if [[ "${SERVICE}" == "${s}" ]]; then valid=1; break; fi
    done
    [[ "${valid}" -eq 1 ]] || die "Unknown service '${SERVICE}'. Valid: ${ALL_SERVICES[*]}"
fi

ARGS=(--tail="${TAIL}")
[[ "${FOLLOW}" -eq 1 ]] && ARGS+=(-f)
[[ -n "${SERVICE}" ]] && ARGS+=("${SERVICE}")

${COMPOSE} logs "${ARGS[@]}"
