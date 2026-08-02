#!/usr/bin/env bash
# GameMode end hook. Reopen the inference gate and restore only lightweight
# routers/proxies and lightweight service shells. No model is loaded until a
# later real inference, speech, voice, or image request.
set -Eeuo pipefail

dominus_ai_root=${DOMINUS_AI_ROOT:-/mnt/nvmer0/services/ai-stack}
source_dir=${DOMINUS_AI_SOURCE_DIR:-$dominus_ai_root/source/klukai/ops/dominus-nobara}
env_file=${DOMINUS_AI_ENV_FILE:-$dominus_ai_root/config/stack.env}
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

if ! rm -f -- "$marker" || test -e "$marker"; then
  log "failed to remove game marker; inference remains fail-closed"
  exit 1
fi
log "game ended; inference gate reopened"

if ! mountpoint --quiet /mnt/nvmer0 \
  || ! test -r "$source_dir/compose.yaml" \
  || ! test -r "$env_file"; then
  log "RAID or canonical stack configuration is unavailable; services remain stopped"
  exit 1
fi
if ! systemctl --user start vllm-idle-watchdog.service vllm-proxy.service \
  >/dev/null 2>&1; then
  log "failed to restore native vLLM guards"
  exit 1
fi
if ! systemctl --user start "$stack_unit" >/dev/null 2>&1; then
  log "failed to restore $stack_unit through its canonical preflights"
  exit 1
fi
log "canonical lazy stack restored through systemd; GPU models remain unloaded"
exit 0
