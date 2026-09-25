#!/usr/bin/env bash
# Recover the Tailscale-pinned publishes on the RTX 3090 host.
#
# Turning that computer off is normal. On the next power-on, dockerd restores
# restart=unless-stopped containers before tailscale0 has 100.107.121.5.
# Docker logs "cannot assign requested address", leaves the container running
# and internally healthy, and does not retry the bind. `docker restart` keeps
# the empty publish. Only a recreate after the address exists opens the port.
#
# --wait-for-address is the dockerd ExecStartPre: block briefly so the first
# restore can bind, then exit 0 anyway. A Tailscale outage must not wedge
# every other container on this machine.
set -euo pipefail

TAILSCALE_IP=${DOMINUS_TAILSCALE_IPV4:-100.107.121.5}
GAME_MARKER=${DOMINUS_GAME_MARKER:-/run/user/1000/dominus-gpu/game-active}
SOURCE_DIR=${DOMINUS_AI_SOURCE_DIR:-/mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara}
ENV_FILE=${DOMINUS_AI_ENV_FILE:-/mnt/nvmer0/services/ai-stack/config/stack.env}
COMPOSE_FILE=${DOMINUS_COMPOSE_FILE:-$SOURCE_DIR/compose.yaml}
DRY_RUN=${DOMINUS_PUBLISH_REPAIR_DRY_RUN:-0}
WAIT_SECONDS=${DOMINUS_TAILSCALE_WAIT_SECONDS:-60}

# Host port -> compose service. Order is the recreate order.
PORTS=(1234 8301 8390)
SERVICES=(lmstudio-compat companion-voice speaches)

log() {
  logger -t dominus-publish-repair -- "$*" 2>/dev/null || true
}

finish() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '%s\n' "$1"
  fi
  exit 0
}

from_unit=0
if [[ "${1:-}" == "--from-unit" ]]; then
  from_unit=1
  shift
fi

if [[ "${1:-}" == "--wait-for-address" ]]; then
  attempts=$WAIT_SECONDS
  if [[ "$attempts" -lt 0 ]]; then
    attempts=0
  fi
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if tailscale ip -4 2>/dev/null | grep -Fxq "$TAILSCALE_IP"; then
      exit 0
    fi
    sleep 1
  done
  echo "dominus: Tailscale address ${TAILSCALE_IP} not ready after ${attempts}s; continuing" >&2
  exit 0
fi

if [[ -e "$GAME_MARKER" ]]; then
  finish noop-game
fi

if ! tailscale ip -4 2>/dev/null | grep -Fxq "$TAILSCALE_IP"; then
  finish noop-no-ip
fi

if ! mountpoint -q /mnt/nvmer0; then
  finish noop-no-raid
fi

if ! docker info >/dev/null 2>&1; then
  finish noop-no-docker
fi

# The stack unit calls this as its own ExecStart, while it is still
# activating. The timer must stay out of that window so it does not recreate
# containers the unit is already starting (including the game-end restore).
unit_state=$(systemctl --user is-active dominus-ai-stack.service 2>/dev/null || true)
if [[ "$from_unit" != 1 && "$unit_state" == activating ]]; then
  finish noop-activating
fi

if ! listeners=$(ss -H -lnt 2>/dev/null); then
  finish noop-unobservable
fi

missing_services=()
for index in "${!PORTS[@]}"; do
  port=${PORTS[$index]}
  if ! grep -F -w -q "${TAILSCALE_IP}:${port}" <<<"$listeners"; then
    missing_services+=("${SERVICES[$index]}")
  fi
done

if [[ ${#missing_services[@]} -eq 0 ]]; then
  finish noop-healthy
fi

if [[ "$DRY_RUN" == 1 ]]; then
  printf 'repair %s\n' "${missing_services[*]}"
  exit 0
fi

lock_path=/run/user/$(id -u)/dominus-publish-repair.lock
if ! mkdir -p -- "$(dirname "$lock_path")" 2>/dev/null; then
  lock_path=/tmp/dominus-publish-repair.lock
fi
exec 9>"$lock_path"
if ! flock -n 9; then
  log "repair already running"
  exit 0
fi

log "republishing ${missing_services[*]} on ${TAILSCALE_IP}"
# Checks above can take time. A game may have started since the entry guard;
# recheck immediately before the mutation, including after acquiring the lock.
if [[ -e "$GAME_MARKER" ]]; then
  finish noop-game
fi
docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" \
  up --detach --no-build --force-recreate --no-deps "${missing_services[@]}"

if ! listeners=$(ss -H -lnt 2>/dev/null); then
  log "republish finished but listeners could not be read"
  exit 1
fi
still_closed=()
for index in "${!PORTS[@]}"; do
  port=${PORTS[$index]}
  service=${SERVICES[$index]}
  for wanted in "${missing_services[@]}"; do
    if [[ "$wanted" == "$service" ]] && ! grep -F -w -q "${TAILSCALE_IP}:${port}" <<<"$listeners"; then
      still_closed+=("$port")
    fi
  done
done
if [[ ${#still_closed[@]} -gt 0 ]]; then
  log "republish incomplete; still closed: ${still_closed[*]}"
  exit 1
fi
log "republish complete: ${missing_services[*]}"
