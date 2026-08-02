#!/usr/bin/env bash
# Explicitly gated dependency chaos drill for Klukai.
#
# Safe default:
#   scripts/chaos-kill-dep.sh <dep> [hold_seconds] --dry-run
# Live execution additionally requires:
#   CHAOS_CONFIRM=run-klukai-chaos-drill ... --execute
#
# Dependencies: postgres, redis, qdrant, voice, comfyui, llm
# `lm_studio` remains accepted as a CLI alias for `llm`; the actual Nobara
# service stopped is the canonical `llama-router` Compose service.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
cd "${REPO_ROOT}"

if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
  sed -n '2,11p' "$0"
  exit 0
fi
(( $# >= 1 )) || {
  echo "usage: $0 <postgres|redis|qdrant|voice|comfyui|llm> [hold_seconds] [--dry-run|--execute]" >&2
  exit 2
}
DEP=$1
shift
HOLD=30
if (( $# > 0 )) && [[ "$1" =~ ^[0-9]+$ ]]; then
  HOLD=$1
  shift
fi
MODE=dry-run
while (( $# > 0 )); do
  case "$1" in
    --dry-run) MODE=dry-run ;;
    --execute) MODE=execute ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *) echo "chaos-kill-dep: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if ! [[ "${HOLD}" =~ ^[0-9]+$ ]] || (( HOLD < 1 || HOLD > 300 )); then
  echo "chaos-kill-dep: hold_seconds must be between 1 and 300" >&2
  exit 2
fi

case "${DEP}" in
  lm_studio|llama-router) DEP=llm ;;
esac

HOST=amarillo
TARGET_KIND=container
TARGET=
RUNBOOK=
case "${DEP}" in
  postgres)
    TARGET="${CHAOS_CONTAINER:-infra-postgres}"
    RUNBOOK=docs/runbooks/db-down.md
    ;;
  redis)
    TARGET="${CHAOS_CONTAINER:-aichat-aichat-redis-1}"
    RUNBOOK=docs/runbooks/redis-down.md
    ;;
  qdrant)
    TARGET="${CHAOS_CONTAINER:-aichat-aichat-vector-1}"
    RUNBOOK=docs/runbooks/qdrant-down.md
    ;;
  voice)
    HOST=dominus-nobara
    TARGET_KIND=compose-service
    TARGET=companion-voice
    RUNBOOK=docs/runbooks/voice-unreachable.md
    ;;
  comfyui)
    HOST=dominus-nobara
    TARGET_KIND=compose-service
    TARGET=comfyui
    RUNBOOK=docs/runbooks/comfyui-down.md
    ;;
  llm)
    HOST=dominus-nobara
    TARGET_KIND=compose-service
    TARGET=llama-router
    RUNBOOK=docs/runbooks/lm-studio-cold.md
    ;;
  *)
    echo "usage: $0 <postgres|redis|qdrant|voice|comfyui|llm> [hold_seconds] [--dry-run|--execute]" >&2
    exit 2
    ;;
esac
[[ "${TARGET}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
  echo "chaos-kill-dep: unsafe target name: ${TARGET}" >&2
  exit 2
}
if [[ "${TARGET_KIND}" == compose-service && -n "${CHAOS_CONTAINER:-}" ]]; then
  echo "chaos-kill-dep: CHAOS_CONTAINER cannot override canonical Nobara Compose services" >&2
  exit 2
fi

SOURCE_TAILSCALE_IPV4="${SOURCE_TAILSCALE_IPV4:-100.111.198.19}"
DOMINUS_TAILSCALE_IPV4="${DOMINUS_TAILSCALE_IPV4:-100.107.121.5}"
DOMINUS_SSH_HOST="${DOMINUS_SSH_HOST:-dominus-nobara}"
DOMINUS_RAID_MOUNT="${DOMINUS_RAID_MOUNT:-/mnt/nvmer0}"
DOMINUS_COMPOSE_FILE="${DOMINUS_COMPOSE_FILE:-/mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara/compose.yaml}"
DOMINUS_STACK_ENV="${DOMINUS_STACK_ENV:-/mnt/nvmer0/services/ai-stack/config/stack.env}"
GPU_GAME_MARKER="${GPU_GAME_MARKER:-/run/user/1000/dominus-gpu/game-active}"
SSH_CONNECT_TIMEOUT_SECONDS="${SSH_CONNECT_TIMEOUT_SECONDS:-15}"

for remote_path in "${DOMINUS_RAID_MOUNT}" "${DOMINUS_COMPOSE_FILE}" "${DOMINUS_STACK_ENV}" "${GPU_GAME_MARKER}"; do
  [[ "${remote_path}" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "chaos-kill-dep: unsafe remote path: ${remote_path}" >&2
    exit 2
  }
done
[[ "${SOURCE_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "chaos-kill-dep: invalid source Tailscale IPv4" >&2
  exit 2
}
[[ "${DOMINUS_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "chaos-kill-dep: invalid dominus Tailscale IPv4" >&2
  exit 2
}
if ! [[ "${SSH_CONNECT_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || \
   (( SSH_CONNECT_TIMEOUT_SECONDS < 5 || SSH_CONNECT_TIMEOUT_SECONDS > 120 )); then
  echo "chaos-kill-dep: SSH_CONNECT_TIMEOUT_SECONDS must be between 5 and 120" >&2
  exit 2
fi

for command_name in awk curl date docker grep jq ssh tailscale; do
  command -v "${command_name}" >/dev/null || {
    echo "chaos-kill-dep: missing required command: ${command_name}" >&2
    exit 2
  }
done
tailscale ip -4 | grep -Fxq -- "${SOURCE_TAILSCALE_IPV4}" || {
  echo "chaos-kill-dep: this host is not the expected Amarillo Tailnet node" >&2
  exit 2
}

ssh_transport() {
  command ssh -T \
    -o AddressFamily=inet \
    -o BatchMode=yes \
    -o ClearAllForwardings=yes \
    -o Compression=no \
    -o ConnectTimeout="${SSH_CONNECT_TIMEOUT_SECONDS}" \
    -o ControlMaster=no \
    -o ControlPath=none \
    -o ForwardAgent=no \
    -o StrictHostKeyChecking=yes \
    "$@"
}

remote_compose() {
  local action=$1
  local service=$2
  ssh_transport "${DOMINUS_SSH_HOST}" bash -s -- \
    "${SOURCE_TAILSCALE_IPV4}" "${DOMINUS_TAILSCALE_IPV4}" \
    "${DOMINUS_RAID_MOUNT}" "${DOMINUS_COMPOSE_FILE}" \
    "${DOMINUS_STACK_ENV}" "${GPU_GAME_MARKER}" "${action}" "${service}" <<'REMOTE'
set -Eeuo pipefail
expected_client_ip=$1
expected_server_ip=$2
raid_mount=$3
compose_file=$4
env_file=$5
game_marker=$6
action=$7
service=$8
read -r client_ip _ server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${client_ip}" == "${expected_client_ip}" && "${server_ip}" == "${expected_server_ip}" ]] || {
  echo "refusing non-Tailscale SSH path" >&2
  exit 1
}
mountpoint --quiet -- "${raid_mount}"
[[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]]
[[ "$(realpath -m -- "${raid_mount}")" == "${raid_mount}" ]]
[[ -r "${compose_file}" && -r "${env_file}" ]]
[[ "${compose_file}" == "${raid_mount}/"* && "${env_file}" == "${raid_mount}/"* ]]
[[ ! -e "${game_marker}" ]] || {
  echo "refusing chaos operation while GameMode owns the GPU" >&2
  exit 1
}
compose_file=$(realpath -e -- "${compose_file}")
env_file=$(realpath -e -- "${env_file}")
[[ "${compose_file}" == "${raid_mount}/"* && "${env_file}" == "${raid_mount}/"* ]]
compose=(docker compose --env-file "${env_file}" --file "${compose_file}" --profile '*')
case "${action}" in
  inspect)
    "${compose[@]}" config --services | grep -Fxq -- "${service}"
    "${compose[@]}" ps --status running --services "${service}" | grep -Fxq -- "${service}"
    ;;
  stop)
    "${compose[@]}" stop --timeout 25 "${service}"
    ! "${compose[@]}" ps --status running --services "${service}" | grep -q .
    ;;
  start)
    "${compose[@]}" up --detach --no-build --no-deps "${service}"
    "${compose[@]}" ps --status running --services "${service}" | grep -Fxq -- "${service}"
    ;;
  *) exit 2 ;;
esac
REMOTE
}

if [[ "${TARGET_KIND}" == compose-service ]]; then
  resolved_ssh_host=$(ssh -G -o AddressFamily=inet "${DOMINUS_SSH_HOST}" 2>/dev/null \
    | awk '$1 == "hostname" {print $2; exit}')
  [[ "${resolved_ssh_host}" == "${DOMINUS_TAILSCALE_IPV4}" ]] || {
    echo "chaos-kill-dep: ${DOMINUS_SSH_HOST} resolves to ${resolved_ssh_host:-nothing}, not ${DOMINUS_TAILSCALE_IPV4}" >&2
    exit 2
  }
  remote_compose inspect "${TARGET}" || {
    echo "chaos-kill-dep: canonical service ${TARGET} is not safely reachable and running" >&2
    exit 2
  }
else
  docker ps --format '{{.Names}}' | grep -Fxq -- "${TARGET}" || {
    echo "chaos-kill-dep: local container is not running: ${TARGET}" >&2
    exit 2
  }
fi

if [[ "${DEP}" == postgres || "${DEP}" == redis || "${DEP}" == qdrant ]]; then
  echo "WARNING: ${TARGET} is shared by Klukai and other Amarillo applications."
fi

echo "Preflight passed: ${TARGET_KIND} ${TARGET} on ${HOST}; hold=${HOLD}s"
if [[ "${MODE}" == dry-run ]]; then
  echo "DRY RUN: no dependency or service was modified"
  if [[ "${TARGET_KIND}" == compose-service ]]; then
    echo "Would stop and restore canonical Compose service ${TARGET} through ${DOMINUS_SSH_HOST} (${DOMINUS_TAILSCALE_IPV4})"
  else
    echo "Would stop and restore local container ${TARGET}"
  fi
  exit 0
fi
[[ "${CHAOS_CONFIRM:-}" == 'run-klukai-chaos-drill' ]] || {
  echo "chaos-kill-dep: set CHAOS_CONFIRM=run-klukai-chaos-drill for --execute" >&2
  exit 2
}

OUT_DIR="${REPO_ROOT}/docs/chaos-drills"
mkdir -p -- "${OUT_DIR}"
STAMP=$(date -u +%Y-%m-%d-%H%M%S)
MD="${OUT_DIR}/${STAMP}-${DEP}.md"
JSON="${OUT_DIR}/${STAMP}-${DEP}.json"
BASELINE_OK=$(curl --silent --max-time 3 http://127.0.0.1:8300/health 2>/dev/null \
  | jq -r '.status // "unknown"' 2>/dev/null || echo unknown)
echo "Pre-drill /health status: ${BASELINE_OK}"

STOPPED=false
restore_dependency() {
  if [[ "${STOPPED}" != true ]]; then
    return 0
  fi
  echo "Safety restore: ${TARGET} on ${HOST}"
  set +e
  if [[ "${TARGET_KIND}" == compose-service ]]; then
    remote_compose start "${TARGET}"
    restore_status=$?
  else
    docker start "${TARGET}" >/dev/null
    restore_status=$?
  fi
  set -e
  if (( restore_status == 0 )); then
    STOPPED=false
  fi
  return "${restore_status}"
}
trap restore_dependency EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Stopping ${TARGET} on ${HOST}"
KILL_TS=$(date -u +%s)
STOPPED=true
if [[ "${TARGET_KIND}" == compose-service ]]; then
  remote_compose stop "${TARGET}"
else
  docker stop "${TARGET}" >/dev/null
fi

sleep "${HOLD}"
MIDPOINT_STATUS=$(curl --silent --max-time 3 http://127.0.0.1:8300/health 2>/dev/null \
  | jq -r '.status // "unreachable"' 2>/dev/null || echo unreachable)
echo "Mid-drill /health status: ${MIDPOINT_STATUS}"

RESTORE_START=$(date -u +%s)
restore_dependency || {
  echo "chaos-kill-dep: failed to restore ${TARGET}" >&2
  exit 1
}

dependency_healthy() {
  case "${DEP}" in
    voice)
      curl --fail --silent --max-time 3 "http://${DOMINUS_TAILSCALE_IPV4}:8301/health" \
        | jq -e '.status == "ok"' >/dev/null
      ;;
    comfyui)
      curl --fail --silent --max-time 3 "http://${DOMINUS_TAILSCALE_IPV4}:1234/health" \
        | jq -e '.comfyui_status == "ok"' >/dev/null
      ;;
    llm)
      curl --fail --silent --max-time 3 "http://${DOMINUS_TAILSCALE_IPV4}:1234/health" \
        | jq -e '.upstream == "ok"' >/dev/null
      ;;
    *)
      curl --fail --silent --max-time 3 http://127.0.0.1:8300/health \
        | jq -e '.status == "ok"' >/dev/null
      ;;
  esac
}

BUDGET=120
RECOVERY_TIME=0
RECOVERED=false
for (( attempt = 1; attempt <= BUDGET; attempt++ )); do
  if dependency_healthy; then
    RECOVERY_TIME=$(($(date -u +%s) - RESTORE_START))
    RECOVERED=true
    break
  fi
  sleep 1
done
TOTAL_OUTAGE=$((RESTORE_START - KILL_TS + RECOVERY_TIME))

cat >"${MD}" <<REPORT
# Chaos drill — ${DEP} — ${STAMP}

- **Target:** \`${TARGET}\` (${TARGET_KIND}) on ${HOST}
- **Hold duration:** ${HOLD}s
- **Recovery time after restore:** ${RECOVERY_TIME}s
- **Total outage window:** ${TOTAL_OUTAGE}s
- **Pre-drill health:** ${BASELINE_OK}
- **Mid-drill health:** ${MIDPOINT_STATUS}
- **Recovered:** ${RECOVERED}
- **Runbook:** \`${RUNBOOK}\`

## Follow-ups

- [ ] Verify the expected circuit breaker opened.
- [ ] Correlate the recovery interval in Grafana.
- [ ] Update the linked runbook with any new finding.
REPORT

jq -n \
  --arg drill_date "${STAMP}" \
  --arg dep "${DEP}" \
  --arg target "${TARGET}" \
  --arg target_kind "${TARGET_KIND}" \
  --arg host "${HOST}" \
  --arg pre_health "${BASELINE_OK}" \
  --arg mid_health "${MIDPOINT_STATUS}" \
  --argjson hold_seconds "${HOLD}" \
  --argjson recovery_seconds "${RECOVERY_TIME}" \
  --argjson total_outage_seconds "${TOTAL_OUTAGE}" \
  --argjson recovered "${RECOVERED}" \
  '{drill_date:$drill_date,dep:$dep,target:$target,target_kind:$target_kind,
    host:$host,hold_seconds:$hold_seconds,recovery_seconds:$recovery_seconds,
    total_outage_seconds:$total_outage_seconds,pre_health:$pre_health,
    mid_health:$mid_health,recovered:$recovered}' >"${JSON}"

echo "Drill report: ${MD}"
echo "JSON: ${JSON}"
[[ "${RECOVERED}" == true ]]
