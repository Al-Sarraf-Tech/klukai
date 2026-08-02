#!/usr/bin/env bash
# GameMode start hook. The marker is created first so the compatibility gateway
# rejects new inference before any GPU process is asked to stop.
set -Eeuo pipefail

dominus_ai_root=${DOMINUS_AI_ROOT:-/mnt/nvmer0/services/ai-stack}
source_dir=${DOMINUS_AI_SOURCE_DIR:-$dominus_ai_root/source/klukai/ops/dominus-nobara}
env_file=${DOMINUS_AI_ENV_FILE:-$dominus_ai_root/config/stack.env}
process_verifier=${DOMINUS_GPU_PROCESS_VERIFIER:-/home/jalsarraf/.local/bin/dominus-verify-canonical-gpu-processes}
stack_unit=dominus-ai-stack.service
if [[ ${DOMINUS_GPU_GUARD_TEST_MODE:-0} == 1 ]]; then
  guard_dir=$(realpath -m -- "${DOMINUS_GPU_GUARD_DIR:?test guard directory is required}")
  [[ "$guard_dir" == /tmp/* ]] || exit 2
else
  [[ -z ${DOMINUS_GPU_GUARD_DIR:-} ]] || exit 2
  guard_dir=/run/user/1000/dominus-gpu
fi
marker=$guard_dir/game-active

log() {
  logger -t dominus-gpu-guard -- "$*" 2>/dev/null || true
}

critical() {
  logger --priority user.crit -t dominus-gpu-guard -- "$*" 2>/dev/null || true
}

umask 022
if ! install -d -m 0755 "$guard_dir" || ! touch "$marker" || ! test -e "$marker"; then
  critical "cannot create mandatory game marker at $marker; refusing a false-success hook"
  exit 1
fi
log "game active; inference gate closed"

# Stop the canonical owner synchronously after closing the gate. This removes
# the start/reload race: a start already in flight must finish stopping before
# the lower-level container and PID verification below can declare success.
if ! systemctl --user stop "$stack_unit" >/dev/null 2>&1; then
  critical "failed to stop $stack_unit after closing the game gate"
  exit 1
fi
stack_state=$(systemctl --user is-active "$stack_unit" 2>/dev/null || true)
case "$stack_state" in
  inactive|failed) ;;
  *)
    critical "$stack_unit is not synchronously stopped (state: ${stack_state:-unverifiable})"
    exit 1
    ;;
esac

# Keep the hardened proxy alive: it observes the marker above and returns a
# prompt JSON 503 without starting the backend. The systemd condition and the
# independent watchdog are backstops against a racing start.
systemctl --user start vllm-idle-watchdog.service vllm-proxy.service \
  2>/dev/null || log "native vLLM proxy/watchdog was not available"

systemctl --user stop --no-block vllm-server.service 2>/dev/null || true
for _ in {1..40}; do
  systemctl --user is-active --quiet vllm-server.service || break
  sleep 0.25
done
if systemctl --user is-active --quiet vllm-server.service; then
  systemctl --user kill --kill-whom=all --signal=SIGKILL vllm-server.service \
    2>/dev/null || true
  systemctl --user stop vllm-server.service 2>/dev/null || true
fi

if systemctl --user is-active --quiet vllm-server.service; then
  critical "vllm-server.service remained active after forced quiesce"
  exit 1
fi

if ! mountpoint --quiet /mnt/nvmer0 || ! test -r "$source_dir/compose.yaml" || ! test -r "$env_file" || ! test -x "$process_verifier"; then
  critical "RAID, Compose file, private environment, or process verifier is unavailable; cannot verify GPU stop"
  exit 1
fi

canonical_container_id_output=$(
  docker compose --env-file "$env_file" --file "$source_dir/compose.yaml" \
    ps --all --quiet \
    llama-router comfyui speaches transcriptionsuite transcriptionsuite-bootstrap companion-voice
) || {
  critical "Docker did not provide canonical container identities"
  exit 1
}
canonical_container_ids=()
if [[ -n "$canonical_container_id_output" ]]; then
  mapfile -t canonical_container_ids <<<"$canonical_container_id_output"
fi

docker compose --env-file "$env_file" --file "$source_dir/compose.yaml" \
  stop --timeout 25 \
  llama-router comfyui speaches transcriptionsuite transcriptionsuite-bootstrap companion-voice \
  >/dev/null 2>&1 || log "one or more AI containers required post-stop verification"

running_services=$(
  docker compose --env-file "$env_file" --file "$source_dir/compose.yaml" \
    ps --status running --services \
    llama-router comfyui speaches transcriptionsuite transcriptionsuite-bootstrap companion-voice
) || {
  critical "Docker did not provide a verifiable GPU-container state"
  exit 1
}
if [[ -n "$running_services" ]]; then
  critical "GPU containers remained active: ${running_services//$'\n'/, }"
  exit 1
fi

verifier_arguments=()
for container_id in "${canonical_container_ids[@]}"; do
  [[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]] || {
    critical "Docker returned a malformed canonical container identity"
    exit 1
  }
  verifier_arguments+=(--container-id "$container_id")
done
if ! "$process_verifier" "${verifier_arguments[@]}" >/dev/null; then
  critical "canonical GPU compute remained or NVIDIA process state was unverifiable"
  exit 1
fi

log "GPU AI services quiesced; proxies remain up only to return game-active 503s"
exit 0
