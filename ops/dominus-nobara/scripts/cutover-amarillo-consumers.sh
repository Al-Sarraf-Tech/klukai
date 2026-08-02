#!/usr/bin/env bash
# Transactionally repoint Amarillo OpenCode and Klukai at dominus-nobara.
#
# Safe default: a no-write dry run.  --apply is required for mutation.
# The separate aichat code, config, and service are deliberately report-only
# until its ownership policy and Bearer-auth blockers are resolved.

set -Eeuo pipefail
umask 077

readonly TARGET_HOST="dominus-nobara"
readonly TARGET_IP="100.107.121.5"
readonly TARGET_PORT="1234"
readonly TARGET_BASE_URL="http://${TARGET_IP}:${TARGET_PORT}"
readonly TARGET_OPENAI_URL="${TARGET_BASE_URL}/v1"
readonly TARGET_VOICE_URL="http://${TARGET_IP}:8301"
readonly TARGET_COMFYUI_URL="${TARGET_BASE_URL}/api/v1/comfy"
readonly KEEPALIVE_UNIT="dominus-wsl-keepalive.service"
readonly OPENCODE_SYNC_UNIT="opencode-sync-models.service"
readonly OPENCODE_TIMER_UNIT="opencode-sync-models.timer"
export TARGET_BASE_URL

MODE="dry-run"
MODE_EXPLICIT=0
ROLLBACK_SOURCE=""
WORK_DIR=""
AUTH_HEADER_FILE=""
LM_TOKEN_VALUE_FILE=""
VOICE_TOKEN_VALUE_FILE=""
ACTIVE_BACKUP=""
APPLY_STARTED=0
COMMITTED=0

# Capture only the dedicated LM credential, then remove it from the inherited
# environment before invoking any child process. These globals are not exported.
LM_TOKEN_ENV_PRIMARY="${LMSTUDIO_API_KEY:-}"
LM_TOKEN_ENV_SECONDARY="${LM_STUDIO_TOKEN:-}"
VOICE_TOKEN_ENV_VALUE="${VOICE_API_TOKEN:-}"
unset LMSTUDIO_API_KEY LM_STUDIO_TOKEN VOICE_API_TOKEN

info() { printf '[amarillo-cutover] %s\n' "$*"; }
warn() { printf '[amarillo-cutover] WARNING: %s\n' "$*" >&2; }
die() {
  printf '[amarillo-cutover] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  cutover-amarillo-consumers.sh [--dry-run]
  cutover-amarillo-consumers.sh --apply
  cutover-amarillo-consumers.sh --rollback BACKUP_DIRECTORY

Modes:
  --dry-run   Verify topology, authenticated APIs, candidates, and the plan.
              This is the default and does not mutate managed configuration.
  --apply     Back up exact files, atomically install validated OpenCode and
              Klukai .env candidates, disable the obsolete WSL keepalive, and
              restart only the explicitly enumerated OpenCode sync timer when
              already active.
  --rollback  Atomically restore an exact backup and recorded unit state.

The LM credential is read only from LMSTUDIO_API_KEY, LM_STUDIO_TOKEN, or its
dedicated token file. The voice credential is read only from VOICE_API_TOKEN
or its dedicated token file. They are never included in arguments or output.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run)
      [[ "$MODE_EXPLICIT" -eq 0 ]] || die "choose exactly one mode"
      MODE="dry-run"
      MODE_EXPLICIT=1
      shift
      ;;
    --apply)
      [[ "$MODE_EXPLICIT" -eq 0 ]] || die "choose exactly one mode"
      MODE="apply"
      MODE_EXPLICIT=1
      shift
      ;;
    --rollback)
      [[ "$MODE_EXPLICIT" -eq 0 && $# -ge 2 ]] \
        || die "--rollback requires one backup directory and no other mode"
      MODE="rollback"
      MODE_EXPLICIT=1
      ROLLBACK_SOURCE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [[ "${CUTOVER_TEST_MODE:-0}" == "1" ]]; then
  [[ -n "${CUTOVER_TEST_HOME:-}" ]] || die "CUTOVER_TEST_HOME is required in test mode"
  EFFECTIVE_HOME="$(realpath -m -- "$CUTOVER_TEST_HOME")"
  case "$EFFECTIVE_HOME" in
    /|"$(realpath -e -- "$HOME")"|"$(realpath -e -- "$HOME")"/*)
      die "test home must be an isolated non-live directory"
      ;;
  esac
  AICHAT_REPO="$(realpath -m -- "${CUTOVER_TEST_AICHAT_REPO:-$EFFECTIVE_HOME/git/aichat}")"
  KLUKAI_REPO="$(realpath -m -- "${CUTOVER_TEST_KLUKAI_REPO:-$EFFECTIVE_HOME/git/klukai}")"
  BACKUP_ROOT_REQUESTED="${CUTOVER_BACKUP_ROOT:-${EFFECTIVE_HOME}/.local/state/dominus-nobara-cutover/backups}"
else
  [[ -z "${CUTOVER_TEST_HOME:-}" && -z "${CUTOVER_TEST_AICHAT_REPO:-}" \
    && -z "${CUTOVER_TEST_KLUKAI_REPO:-}" && -z "${CUTOVER_BACKUP_ROOT:-}" ]] \
    || die "test path overrides require CUTOVER_TEST_MODE=1"
  EFFECTIVE_HOME="$(realpath -e -- "$HOME")"
  AICHAT_REPO="${EFFECTIVE_HOME}/git/aichat"
  KLUKAI_REPO="${EFFECTIVE_HOME}/git/klukai"
  BACKUP_ROOT_REQUESTED="${EFFECTIVE_HOME}/.local/state/dominus-nobara-cutover/backups"
fi

BACKUP_ROOT="$(realpath -m -- "$BACKUP_ROOT_REQUESTED")"
case "$BACKUP_ROOT" in
  "${EFFECTIVE_HOME}"/*) ;;
  *) die "backup root must remain inside the effective home" ;;
esac
readonly EFFECTIVE_HOME AICHAT_REPO KLUKAI_REPO BACKUP_ROOT
case "$KLUKAI_REPO" in
  "${EFFECTIVE_HOME}"/*) ;;
  *) die "Klukai repository must remain inside the effective home" ;;
esac
readonly OPENCODE_DIR="${EFFECTIVE_HOME}/.config/opencode"
readonly OPENCODE_JSONC="${OPENCODE_DIR}/opencode.jsonc"
readonly OPENCODE_TEMPLATE="${OPENCODE_DIR}/opencode.jsonc.tmpl"
readonly OPENCODE_PLUGIN="${OPENCODE_DIR}/plugin/lmstudio-single-endpoint.mjs"
readonly AICHAT_CONFIG="${EFFECTIVE_HOME}/.config/aichat/config.yml"
readonly KLUKAI_ENV="${KLUKAI_REPO}/.env"
readonly USER_UNIT_DIR="${EFFECTIVE_HOME}/.config/systemd/user"
readonly OPENCODE_DROPIN_DIR="${USER_UNIT_DIR}/${OPENCODE_SYNC_UNIT}.d"
readonly OPENCODE_DROPIN="${OPENCODE_DROPIN_DIR}/20-dominus-nobara.conf"
readonly TOKEN_FILE="${LMSTUDIO_TOKEN_FILE:-${EFFECTIVE_HOME}/.config/agents/lmstudio-dominus-inference.token}"
readonly VOICE_TOKEN_FILE="${VOICE_API_TOKEN_FILE:-${EFFECTIVE_HOME}/.config/agents/voice-dominus-inference.token}"

declare -a REQUIRED_FILES=(
  "$OPENCODE_JSONC"
  "$OPENCODE_TEMPLATE"
  "$OPENCODE_PLUGIN"
  "$KLUKAI_ENV"
)

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

check_host_identity() {
  local short_hostname
  short_hostname="$(hostname -s)"
  [[ "$short_hostname" == "amarillo" ]] \
    || die "this tool may run only on amarillo"
  [[ "$(id -u)" -ne 0 ]] || die "run as the Amarillo user, never root"
}

check_dependencies() {
  local common=(basename cat chmod cp cmp date dirname grep hostname id mkdir mktemp mv realpath rm rmdir sha256sum stat sync systemctl unlink)
  local command_name
  for command_name in "${common[@]}"; do
    require_command "$command_name"
  done

  if [[ "$MODE" != "rollback" ]]; then
    local cutover=(awk curl jq node python3 rg sed ssh tailscale yq)
    for command_name in "${cutover[@]}"; do
      require_command "$command_name"
    done
  fi
}

unit_enabled_state() {
  local state
  state="$(systemctl --user is-enabled "$1" 2>/dev/null || true)"
  [[ -n "$state" ]] || state="not-found"
  printf '%s\n' "$state"
}

unit_active_state() {
  local state
  state="$(systemctl --user is-active "$1" 2>/dev/null || true)"
  [[ -n "$state" ]] || state="unknown"
  printf '%s\n' "$state"
}

meta_value() {
  local meta_file="$1"
  local key="$2"
  local line
  line="$(grep -E "^${key}=[A-Za-z0-9_./:@+-]+$" "$meta_file" || true)"
  [[ -n "$line" ]] || return 1
  printf '%s\n' "${line#*=}"
}

atomic_replace() {
  local source_file="$1"
  local target_file="$2"
  local target_dir temporary
  target_dir="$(dirname -- "$target_file")"
  [[ -f "$target_file" && ! -L "$target_file" ]] \
    || die "atomic replacement target is not a regular file: $target_file"
  temporary="$(mktemp --tmpdir="$target_dir" ".$(basename -- "$target_file").cutover.XXXXXX")"
  cp --archive --reflink=auto -- "$target_file" "$temporary"
  command cat -- "$source_file" >"$temporary"
  sync -f "$temporary"
  mv -fT -- "$temporary" "$target_file"
  sync -f "$target_dir"
}

atomic_create() {
  local source_file="$1"
  local target_file="$2"
  local target_dir temporary
  target_dir="$(dirname -- "$target_file")"
  mkdir -p -- "$target_dir"
  temporary="$(mktemp --tmpdir="$target_dir" ".$(basename -- "$target_file").cutover.XXXXXX")"
  chmod 0644 "$temporary"
  command cat -- "$source_file" >"$temporary"
  sync -f "$temporary"
  mv -fT -- "$temporary" "$target_file"
  sync -f "$target_dir"
}

atomic_restore_exact() {
  local source_file="$1"
  local target_file="$2"
  local target_dir temporary
  [[ -f "$source_file" && ! -L "$source_file" ]] \
    || die "exact restore source is not a regular backup file"
  if [[ -e "$target_file" || -L "$target_file" ]]; then
    [[ -f "$target_file" && ! -L "$target_file" ]] \
      || die "exact restore target is not a safe regular file: $target_file"
  fi
  target_dir="$(dirname -- "$target_file")"
  mkdir -p -- "$target_dir"
  temporary="$(mktemp --tmpdir="$target_dir" ".$(basename -- "$target_file").restore.XXXXXX")"
  cp --archive --reflink=auto -- "$source_file" "$temporary"
  sync -f "$temporary"
  mv -fT -- "$temporary" "$target_file"
  sync -f "$target_dir"
}

restore_unit_state() {
  local enabled_state="$1"
  local active_state="$2"

  case "$enabled_state" in
    enabled|linked|alias)
      systemctl --user enable "$KEEPALIVE_UNIT" >/dev/null
      ;;
    enabled-runtime|linked-runtime)
      systemctl --user enable --runtime "$KEEPALIVE_UNIT" >/dev/null
      ;;
    masked)
      systemctl --user mask "$KEEPALIVE_UNIT" >/dev/null
      ;;
    masked-runtime)
      systemctl --user mask --runtime "$KEEPALIVE_UNIT" >/dev/null
      ;;
    disabled|static|indirect|generated|transient|not-found)
      systemctl --user disable "$KEEPALIVE_UNIT" >/dev/null 2>&1 || true
      ;;
    *)
      warn "unknown saved enablement state for ${KEEPALIVE_UNIT}; leaving disabled"
      systemctl --user disable "$KEEPALIVE_UNIT" >/dev/null 2>&1 || true
      ;;
  esac

  if [[ "$active_state" == "active" || "$active_state" == "activating" ]]; then
    systemctl --user start "$KEEPALIVE_UNIT" >/dev/null
  else
    systemctl --user stop "$KEEPALIVE_UNIT" >/dev/null 2>&1 || true
  fi
}

restore_backup() {
  local requested="$1"
  local backup_dir meta_file format_version saved_home keepalive_enabled keepalive_active timer_active
  backup_dir="$(realpath -e -- "$requested")" \
    || die "backup directory does not exist"
  [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || die "backup path is not a real directory"
  meta_file="${backup_dir}/meta"
  [[ -f "$meta_file" ]] || die "backup metadata is missing"
  format_version="$(meta_value "$meta_file" FORMAT_VERSION)" \
    || die "backup format metadata is invalid"
  [[ "$format_version" == "1" || "$format_version" == "2" ]] \
    || die "unsupported backup format"
  saved_home="$(meta_value "$meta_file" EFFECTIVE_HOME)" \
    || die "backup home metadata is invalid"
  [[ "$saved_home" == "$EFFECTIVE_HOME" ]] \
    || die "backup belongs to a different home directory"

  (
    cd "$backup_dir"
    sha256sum --check --quiet SHA256SUMS
  ) || die "backup integrity verification failed"

  atomic_restore_exact "${backup_dir}/files/opencode.jsonc" "$OPENCODE_JSONC"
  atomic_restore_exact "${backup_dir}/files/opencode.jsonc.tmpl" "$OPENCODE_TEMPLATE"
  atomic_restore_exact "${backup_dir}/files/lmstudio-single-endpoint.mjs" "$OPENCODE_PLUGIN"
  if [[ "$format_version" == "2" ]]; then
    atomic_restore_exact "${backup_dir}/files/klukai.env" "$KLUKAI_ENV"
  fi

  if [[ -f "${backup_dir}/files/opencode-sync-dropin.conf" ]]; then
    atomic_restore_exact "${backup_dir}/files/opencode-sync-dropin.conf" "$OPENCODE_DROPIN"
  elif [[ -f "${backup_dir}/opencode-sync-dropin.was-absent" ]]; then
    if [[ -e "$OPENCODE_DROPIN" || -L "$OPENCODE_DROPIN" ]]; then
      [[ -f "$OPENCODE_DROPIN" && ! -L "$OPENCODE_DROPIN" ]] \
        || die "refusing to remove unexpected drop-in object"
      unlink -- "$OPENCODE_DROPIN"
    fi
    rmdir --ignore-fail-on-non-empty -- "$OPENCODE_DROPIN_DIR" 2>/dev/null || true
  else
    die "backup does not record the OpenCode drop-in state"
  fi

  systemctl --user daemon-reload
  keepalive_enabled="$(meta_value "$meta_file" KEEPALIVE_ENABLED)" \
    || die "saved keepalive enablement is invalid"
  keepalive_active="$(meta_value "$meta_file" KEEPALIVE_ACTIVE)" \
    || die "saved keepalive activity is invalid"
  timer_active="$(meta_value "$meta_file" OPENCODE_TIMER_ACTIVE)" \
    || die "saved timer activity is invalid"
  restore_unit_state "$keepalive_enabled" "$keepalive_active"

  if [[ "$timer_active" == "active" || "$timer_active" == "activating" ]]; then
    systemctl --user restart "$OPENCODE_TIMER_UNIT"
  fi
  info "rollback restored exact consumer files and recorded unit state"
}

cleanup() {
  local exit_code=$?
  local sensitive_file
  trap - EXIT
  set +e

  if ((exit_code != 0 && APPLY_STARTED == 1 && COMMITTED == 0)) \
    && [[ -n "$ACTIVE_BACKUP" && -d "$ACTIVE_BACKUP" ]]; then
    warn "apply failed; restoring the pre-cutover backup"
    restore_backup "$ACTIVE_BACKUP" \
      || warn "automatic rollback failed; use --rollback with the reported backup"
  fi

  for sensitive_file in "$AUTH_HEADER_FILE" "$LM_TOKEN_VALUE_FILE" "$VOICE_TOKEN_VALUE_FILE"; do
    if [[ -n "$sensitive_file" && -f "$sensitive_file" ]]; then
      chmod 0600 "$sensitive_file" 2>/dev/null || true
      : >"$sensitive_file"
      unlink -- "$sensitive_file" 2>/dev/null || true
    fi
  done
  if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
    case "$WORK_DIR" in
      /tmp/*|"${TMPDIR:-/tmp}"/*) rm -rf -- "$WORK_DIR" ;;
      *) warn "refusing to remove unexpected temporary directory: $WORK_DIR" ;;
    esac
  fi
  unset GATEWAY_TOKEN VOICE_TOKEN LM_TOKEN_ENV_PRIMARY LM_TOKEN_ENV_SECONDARY \
    VOICE_TOKEN_ENV_VALUE LMSTUDIO_API_KEY LM_STUDIO_TOKEN VOICE_API_TOKEN
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

check_host_identity
check_dependencies

if [[ "$MODE" == "rollback" ]]; then
  restore_backup "$ROLLBACK_SOURCE"
  COMMITTED=1
  exit 0
fi

for managed_file in "${REQUIRED_FILES[@]}"; do
  [[ -f "$managed_file" && ! -L "$managed_file" && -r "$managed_file" ]] \
    || die "required managed file is missing or unsafe: $managed_file"
  [[ -w "$managed_file" ]] || die "managed file is not writable: $managed_file"
done
[[ -f "$AICHAT_CONFIG" && ! -L "$AICHAT_CONFIG" && -r "$AICHAT_CONFIG" ]] \
  || die "report-only aichat config is missing or unsafe: $AICHAT_CONFIG"
[[ -d "$USER_UNIT_DIR" && ! -L "$USER_UNIT_DIR" ]] \
  || die "user unit directory is missing or unsafe"
if [[ -e "$OPENCODE_DROPIN_DIR" || -L "$OPENCODE_DROPIN_DIR" ]]; then
  [[ -d "$OPENCODE_DROPIN_DIR" && ! -L "$OPENCODE_DROPIN_DIR" ]] \
    || die "OpenCode drop-in directory is unsafe"
fi
[[ "$(unit_enabled_state "$KEEPALIVE_UNIT")" != "not-found" ]] \
  || die "obsolete keepalive unit is missing, so its disablement cannot be verified"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/amarillo-consumer-cutover.XXXXXX")"
chmod 0700 "$WORK_DIR"

verify_tailscale_and_ssh() {
  local status_file local_ips ssh_config configured_host configured_port remote_connection
  local remote_source remote_source_port remote_target remote_target_port extra

  status_file="${WORK_DIR}/tailscale-status.json"
  tailscale status --json >"$status_file" 2>/dev/null \
    || die "unable to read Tailscale peer state"
  jq -e --arg ip "$TARGET_IP" '
    [.Peer[]? | select(any(.TailscaleIPs[]?; . == $ip))] | length == 1
  ' "$status_file" >/dev/null \
    || die "target IP is not a unique Tailscale peer"
  tailscale ping --c 1 --timeout 5s "$TARGET_IP" >/dev/null 2>&1 \
    || die "target is not reachable over Tailscale"

  local_ips="$(tailscale ip -4 2>/dev/null)"
  [[ -n "$local_ips" ]] || die "Amarillo has no Tailscale IPv4 address"

  ssh_config="$(ssh -G "$TARGET_HOST" 2>/dev/null)" \
    || die "unable to resolve the target SSH configuration"
  configured_host="$(awk '$1 == "hostname" {print $2; exit}' <<<"$ssh_config")"
  configured_port="$(awk '$1 == "port" {print $2; exit}' <<<"$ssh_config")"
  [[ "$configured_host" == "$TARGET_IP" ]] \
    || die "SSH alias does not resolve to the required Tailscale IP"
  [[ "$configured_port" =~ ^[0-9]+$ ]] \
    || die "SSH alias has an invalid target port"

  remote_connection="$(
    ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$TARGET_HOST" \
      'printf "%s\n" "${SSH_CONNECTION:-}"'
  )" || die "target SSH_CONNECTION probe failed"
  read -r remote_source remote_source_port remote_target remote_target_port extra \
    <<<"$remote_connection"
  [[ -n "$remote_source" && -n "$remote_source_port" && -n "$remote_target_port" && -z "${extra:-}" ]] \
    || die "target returned an invalid SSH_CONNECTION"
  [[ "$remote_target" == "$TARGET_IP" ]] \
    || die "SSH_CONNECTION did not terminate on the target Tailscale IP"
  [[ "$remote_target_port" == "$configured_port" ]] \
    || die "SSH_CONNECTION target port does not match the SSH alias"
  grep -Fxq -- "$remote_source" <<<"$local_ips" \
    || die "SSH_CONNECTION did not originate from Amarillo's Tailscale IP"
}

validate_private_token_file() {
  local token_file="$1"
  local label="$2"
  local owner mode expected_owner
  [[ -f "$token_file" && ! -L "$token_file" && -r "$token_file" ]] \
    || die "dedicated ${label} token must be a readable regular non-symlink file"
  owner="$(stat -c '%u' -- "$token_file")" \
    || die "dedicated ${label} token owner is unverifiable"
  mode="$(stat -c '%a' -- "$token_file")" \
    || die "dedicated ${label} token mode is unverifiable"
  expected_owner="$(id -u)"
  [[ "$owner" == "$expected_owner" ]] \
    || die "dedicated ${label} token must be owned by the cutover user"
  [[ "$mode" == "600" ]] \
    || die "dedicated ${label} token mode must be exactly 0600"
}

load_cutover_tokens() {
  local primary="$LM_TOKEN_ENV_PRIMARY"
  local secondary="$LM_TOKEN_ENV_SECONDARY"
  local -a token_lines=()

  if [[ -n "$primary" && -n "$secondary" && "$primary" != "$secondary" ]]; then
    die "LM token environment variables disagree"
  fi
  GATEWAY_TOKEN="${primary:-$secondary}"

  if [[ -z "$GATEWAY_TOKEN" ]]; then
    validate_private_token_file "$TOKEN_FILE" "LM"
    mapfile -t token_lines <"$TOKEN_FILE"
    [[ "${#token_lines[@]}" -eq 1 ]] \
      || die "dedicated LM token file must contain exactly one line"
    GATEWAY_TOKEN="${token_lines[0]}"
  fi

  [[ "$GATEWAY_TOKEN" =~ ^[A-Za-z0-9._~+/=-]{32,512}$ ]] \
    || die "dedicated LM token is empty or malformed"
  unset LMSTUDIO_API_KEY LM_STUDIO_TOKEN primary secondary token_lines

  VOICE_TOKEN="$VOICE_TOKEN_ENV_VALUE"
  token_lines=()
  if [[ -z "$VOICE_TOKEN" ]]; then
    validate_private_token_file "$VOICE_TOKEN_FILE" "voice"
    mapfile -t token_lines <"$VOICE_TOKEN_FILE"
    [[ "${#token_lines[@]}" -eq 1 ]] \
      || die "dedicated voice token file must contain exactly one line"
    VOICE_TOKEN="${token_lines[0]}"
  fi
  [[ "$VOICE_TOKEN" =~ ^[A-Za-z0-9._~+/=-]{32,512}$ ]] \
    || die "dedicated voice token is empty or malformed"

  AUTH_HEADER_FILE="${WORK_DIR}/gateway-auth-header"
  LM_TOKEN_VALUE_FILE="${WORK_DIR}/lm-token-value"
  VOICE_TOKEN_VALUE_FILE="${WORK_DIR}/voice-token-value"
  (
    umask 077
    printf 'Authorization: Bearer %s\n' "$GATEWAY_TOKEN" >"$AUTH_HEADER_FILE"
    printf '%s\n' "$GATEWAY_TOKEN" >"$LM_TOKEN_VALUE_FILE"
    printf '%s\n' "$VOICE_TOKEN" >"$VOICE_TOKEN_VALUE_FILE"
  )
  unset GATEWAY_TOKEN VOICE_TOKEN VOICE_TOKEN_ENV_VALUE token_lines
}

probe_gateway() {
  local openai_body native_body
  openai_body="${WORK_DIR}/openai-models.json"
  native_body="${WORK_DIR}/native-models.json"

  curl --fail-with-body --silent --show-error --proto '=http' \
    --connect-timeout 5 --max-time 20 \
    --header "@${AUTH_HEADER_FILE}" \
    --output "$openai_body" \
    "${TARGET_BASE_URL}/v1/models" \
    || die "authenticated OpenAI-compatible gateway probe failed"
  jq -e '.data | type == "array"' "$openai_body" >/dev/null \
    || die "gateway returned an invalid OpenAI model catalog"

  curl --fail-with-body --silent --show-error --proto '=http' \
    --connect-timeout 5 --max-time 20 \
    --header "@${AUTH_HEADER_FILE}" \
    --output "$native_body" \
    "${TARGET_BASE_URL}/api/v1/models" \
    || die "authenticated native model gateway probe failed"
  jq -e '.models | type == "array"' "$native_body" >/dev/null \
    || die "gateway returned an invalid native model catalog"
}

transform_endpoint_file() {
  local source_file="$1"
  local destination_file="$2"
  sed \
    -e "s|http://192\\.168\\.50\\.2:1234|${TARGET_BASE_URL}|g" \
    -e "s|http://100\\.78\\.39\\.76:1234|${TARGET_BASE_URL}|g" \
    -e "s|http://dominus-lan:1234|${TARGET_BASE_URL}|g" \
    -e "s|http://dominus:1234|${TARGET_BASE_URL}|g" \
    -e "s|192\\.168\\.50\\.2:1234|${TARGET_IP}:${TARGET_PORT}|g" \
    -e "s|100\\.78\\.39\\.76:1234|${TARGET_IP}:${TARGET_PORT}|g" \
    -- "$source_file" >"$destination_file"
}

validate_jsonc() {
  local candidate="$1"
  local is_template="$2"
  python3 - "$candidate" "$is_template" "$TARGET_OPENAI_URL" >/dev/null 2>&1 <<'PY'
import json
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_text()
if sys.argv[2] == "template":
    if source.count("__LMSTUDIO_MODELS__") != 1:
        raise SystemExit(1)
    source = source.replace("__LMSTUDIO_MODELS__", '"models": {}')

out = []
i = 0
in_string = False
while i < len(source):
    char = source[i]
    following = source[i + 1] if i + 1 < len(source) else ""
    if in_string:
        out.append(char)
        if char == "\\" and i + 1 < len(source):
            out.append(following)
            i += 2
            continue
        if char == '"':
            in_string = False
        i += 1
        continue
    if char == '"':
        in_string = True
        out.append(char)
        i += 1
        continue
    if char == "/" and following == "/":
        while i < len(source) and source[i] != "\n":
            i += 1
        continue
    if char == "/" and following == "*":
        i += 2
        while i + 1 < len(source) and source[i : i + 2] != "*/":
            i += 1
        i += 2
        continue
    out.append(char)
    i += 1

parsed = json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
provider = parsed["provider"]
if set(provider) != {"lmstudio-dominus"}:
    raise SystemExit(1)
lm = provider["lmstudio-dominus"]
if lm["options"]["baseURL"] != sys.argv[3]:
    raise SystemExit(1)
if lm["options"].get("apiKey") != "{env:LMSTUDIO_API_KEY}":
    raise SystemExit(1)
if parsed.get("enabled_providers") != ["lmstudio-dominus"]:
    raise SystemExit(1)
for key in ("model", "small_model"):
    if not str(parsed.get(key, "")).startswith("lmstudio-dominus/"):
        raise SystemExit(1)
PY
}

transform_klukai_env() {
  local destination_file="$1"
  python3 - \
    "$KLUKAI_ENV" "$destination_file" \
    "$LM_TOKEN_VALUE_FILE" "$VOICE_TOKEN_VALUE_FILE" \
    "$TARGET_BASE_URL" "$TARGET_VOICE_URL" "$TARGET_COMFYUI_URL" <<'PY'
from pathlib import Path
import re
import sys

source_path, destination_path, lm_path, voice_path, lm_url, voice_url, comfy_url = (
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    sys.argv[5],
    sys.argv[6],
    sys.argv[7],
)

def one_line(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0]:
        raise SystemExit(1)
    return lines[0]

updates = {
    "LM_STUDIO_URL": lm_url,
    "LM_STUDIO_TOKEN": one_line(lm_path),
    "VOICE_URL": voice_url,
    "VOICE_API_TOKEN": one_line(voice_path),
    "COMFYUI_URL": comfy_url,
}
assignment = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
seen: set[str] = set()
output: list[str] = []
for line in source_path.read_text(encoding="utf-8").splitlines(keepends=True):
    match = assignment.match(line)
    key = match.group(1) if match else None
    if key not in updates:
        output.append(line)
        continue
    if key in seen:
        raise SystemExit(1)
    seen.add(key)
    newline = "\r\n" if line.endswith("\r\n") else "\n"
    output.append(f"{key}={updates[key]}{newline}")

missing = [key for key in updates if key not in seen]
if missing:
    if output and not output[-1].endswith(("\n", "\r")):
        output[-1] += "\n"
    if output and output[-1].strip():
        output.append("\n")
    output.extend(f"{key}={updates[key]}\n" for key in missing)

destination_path.write_text("".join(output), encoding="utf-8")
PY
}

validate_klukai_env() {
  local candidate="$1"
  python3 - \
    "$candidate" "$LM_TOKEN_VALUE_FILE" "$VOICE_TOKEN_VALUE_FILE" \
    "$TARGET_BASE_URL" "$TARGET_VOICE_URL" "$TARGET_COMFYUI_URL" >/dev/null 2>&1 <<'PY'
from pathlib import Path
import re
import sys

candidate, lm_path, voice_path = map(Path, sys.argv[1:4])
expected = {
    "LM_STUDIO_URL": sys.argv[4],
    "LM_STUDIO_TOKEN": lm_path.read_text(encoding="utf-8").rstrip("\n"),
    "VOICE_URL": sys.argv[5],
    "VOICE_API_TOKEN": voice_path.read_text(encoding="utf-8").rstrip("\n"),
    "COMFYUI_URL": sys.argv[6],
}
assignment = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)
found: dict[str, list[str]] = {key: [] for key in expected}
for line in candidate.read_text(encoding="utf-8").splitlines():
    match = assignment.match(line)
    if match and match.group(1) in found:
        found[match.group(1)].append(match.group(2))
if any(values != [expected[key]] for key, values in found.items()):
    raise SystemExit(1)
PY
}

build_and_validate_candidates() {
  transform_endpoint_file "$OPENCODE_JSONC" "${WORK_DIR}/opencode.jsonc"
  transform_endpoint_file "$OPENCODE_TEMPLATE" "${WORK_DIR}/opencode.jsonc.tmpl"
  transform_endpoint_file "$OPENCODE_PLUGIN" "${WORK_DIR}/lmstudio-single-endpoint.mjs"
  transform_klukai_env "${WORK_DIR}/klukai.env" \
    || die "candidate Klukai environment could not be built safely"

  cat >"${WORK_DIR}/opencode-sync-dropin.conf" <<EOF
[Service]
Environment=OPENCODE_LMS_BASE_URL=${TARGET_BASE_URL}
EOF

  validate_jsonc "${WORK_DIR}/opencode.jsonc" live \
    || die "candidate OpenCode JSONC is invalid or violates provider/auth policy"
  validate_jsonc "${WORK_DIR}/opencode.jsonc.tmpl" template \
    || die "candidate OpenCode template is invalid or violates provider/auth policy"
  node --check "${WORK_DIR}/lmstudio-single-endpoint.mjs" >/dev/null 2>&1 \
    || die "candidate OpenCode plugin has invalid JavaScript syntax"
  grep -Fq "const DEFAULT_BASE_URL = \"${TARGET_BASE_URL}\";" \
    "${WORK_DIR}/lmstudio-single-endpoint.mjs" \
    || die "candidate OpenCode plugin default is not the target"
  grep -Fq 'const PROVIDER_ID = "lmstudio-dominus";' \
    "${WORK_DIR}/lmstudio-single-endpoint.mjs" \
    || die "candidate OpenCode plugin changed the provider alias"
  grep -Fq 'process.env.LMSTUDIO_API_KEY' "${WORK_DIR}/lmstudio-single-endpoint.mjs" \
    || die "candidate OpenCode plugin lost Bearer-token support"
  validate_klukai_env "${WORK_DIR}/klukai.env" \
    || die "candidate Klukai environment is invalid or lost a rotated token"

  if rg -q '192\.168\.50\.2:1234|100\.78\.39\.76:1234|http://dominus(-lan)?:1234' \
    "${WORK_DIR}/opencode.jsonc" \
    "${WORK_DIR}/opencode.jsonc.tmpl" \
    "${WORK_DIR}/lmstudio-single-endpoint.mjs" \
    "${WORK_DIR}/klukai.env"; then
    die "a managed candidate still contains a dead LM endpoint"
  fi
}

report_aichat_repo_blockers() {
  local matches_file="${WORK_DIR}/aichat-repo-blockers"
  local rg_status=0 relative_path count=0 configured_base
  configured_base="$(yq -r '.base_url // ""' "$AICHAT_CONFIG" 2>/dev/null)" \
    || die "unable to parse report-only aichat config"
  warn "aichat config remains unchanged at ${configured_base:-an unset endpoint}"
  if [[ ! -d "$AICHAT_REPO" ]]; then
    warn "aichat repository is unavailable; aichat.service restart remains blocked"
    return
  fi

  rg -l -0 --hidden \
    --glob '!.git/**' --glob '!node_modules/**' --glob '!aipictures/**' \
    --glob '!*.lock' --glob '!*.log' \
    '192\.168\.50\.2:1234|100\.78\.39\.76:1234|http://dominus(-lan)?:1234' \
    "$AICHAT_REPO" >"$matches_file" || rg_status=$?
  [[ "$rg_status" -eq 0 || "$rg_status" -eq 1 ]] \
    || die "unable to inventory unresolved aichat repository endpoints"

  while IFS= read -r -d '' matched_path; do
    relative_path="${matched_path#"$AICHAT_REPO"/}"
    printf '[amarillo-cutover] aichat unresolved path: %s\n' "$relative_path" >&2
    ((count += 1))
  done <"$matches_file"

  if ((count > 0)); then
    warn "aichat repository still has ${count} dead-endpoint file(s); repository is report-only"
  fi
  warn "aichat Bearer compatibility is unresolved; aichat.service will not be restarted"
}

create_backup() {
  local timestamp files_dir canonical_backup_root dropin_state="absent"
  local keepalive_enabled keepalive_active timer_active
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p -- "$BACKUP_ROOT"
  canonical_backup_root="$(realpath -e -- "$BACKUP_ROOT")"
  [[ "$canonical_backup_root" == "$BACKUP_ROOT" \
    && -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]] \
    || die "backup root is not a real directory"
  chmod 0700 "$BACKUP_ROOT"
  ACTIVE_BACKUP="$(mktemp -d "${BACKUP_ROOT}/${timestamp}.XXXXXX")"
  chmod 0700 "$ACTIVE_BACKUP"
  files_dir="${ACTIVE_BACKUP}/files"
  mkdir -m 0700 -- "$files_dir"

  cp --archive --reflink=auto -- "$OPENCODE_JSONC" "${files_dir}/opencode.jsonc"
  cp --archive --reflink=auto -- "$OPENCODE_TEMPLATE" "${files_dir}/opencode.jsonc.tmpl"
  cp --archive --reflink=auto -- "$OPENCODE_PLUGIN" "${files_dir}/lmstudio-single-endpoint.mjs"
  cp --archive --reflink=auto -- "$KLUKAI_ENV" "${files_dir}/klukai.env"
  if [[ -f "$OPENCODE_DROPIN" && ! -L "$OPENCODE_DROPIN" ]]; then
    cp --archive --reflink=auto -- "$OPENCODE_DROPIN" "${files_dir}/opencode-sync-dropin.conf"
    dropin_state="present"
  elif [[ -e "$OPENCODE_DROPIN" || -L "$OPENCODE_DROPIN" ]]; then
    die "existing OpenCode sync drop-in is not a safe regular file"
  else
    : >"${ACTIVE_BACKUP}/opencode-sync-dropin.was-absent"
  fi

  if ! cmp -s -- "$OPENCODE_JSONC" "${files_dir}/opencode.jsonc" \
    || ! cmp -s -- "$OPENCODE_TEMPLATE" "${files_dir}/opencode.jsonc.tmpl" \
    || ! cmp -s -- "$OPENCODE_PLUGIN" "${files_dir}/lmstudio-single-endpoint.mjs" \
    || ! cmp -s -- "$KLUKAI_ENV" "${files_dir}/klukai.env"; then
    die "exact backup verification failed"
  fi
  if [[ "$dropin_state" == "present" ]] \
    && ! cmp -s -- "$OPENCODE_DROPIN" "${files_dir}/opencode-sync-dropin.conf"; then
    die "exact OpenCode drop-in backup verification failed"
  fi

  keepalive_enabled="$(unit_enabled_state "$KEEPALIVE_UNIT")"
  keepalive_active="$(unit_active_state "$KEEPALIVE_UNIT")"
  timer_active="$(unit_active_state "$OPENCODE_TIMER_UNIT")"
  cat >"${ACTIVE_BACKUP}/meta" <<EOF
FORMAT_VERSION=2
EFFECTIVE_HOME=${EFFECTIVE_HOME}
TARGET_IP=${TARGET_IP}
KEEPALIVE_ENABLED=${keepalive_enabled}
KEEPALIVE_ACTIVE=${keepalive_active}
OPENCODE_TIMER_ACTIVE=${timer_active}
OPENCODE_DROPIN_STATE=${dropin_state}
EOF

  (
    cd "$ACTIVE_BACKUP"
    sha256sum files/opencode.jsonc \
      files/opencode.jsonc.tmpl \
      files/lmstudio-single-endpoint.mjs \
      files/klukai.env \
      meta \
      >SHA256SUMS
    if [[ -f files/opencode-sync-dropin.conf ]]; then
      sha256sum files/opencode-sync-dropin.conf >>SHA256SUMS
    else
      sha256sum opencode-sync-dropin.was-absent >>SHA256SUMS
    fi
  )
  chmod 0600 "${ACTIVE_BACKUP}/meta" "${ACTIVE_BACKUP}/SHA256SUMS"
}

install_candidates() {
  APPLY_STARTED=1
  atomic_replace "${WORK_DIR}/opencode.jsonc" "$OPENCODE_JSONC"
  atomic_replace "${WORK_DIR}/opencode.jsonc.tmpl" "$OPENCODE_TEMPLATE"
  atomic_replace "${WORK_DIR}/lmstudio-single-endpoint.mjs" "$OPENCODE_PLUGIN"
  atomic_replace "${WORK_DIR}/klukai.env" "$KLUKAI_ENV"
  chmod 0600 "$KLUKAI_ENV"
  if [[ -f "$OPENCODE_DROPIN" ]]; then
    atomic_replace "${WORK_DIR}/opencode-sync-dropin.conf" "$OPENCODE_DROPIN"
  else
    atomic_create "${WORK_DIR}/opencode-sync-dropin.conf" "$OPENCODE_DROPIN"
  fi

  validate_jsonc "$OPENCODE_JSONC" live \
    || die "installed OpenCode JSONC failed validation"
  validate_jsonc "$OPENCODE_TEMPLATE" template \
    || die "installed OpenCode template failed validation"
  node --check "$OPENCODE_PLUGIN" >/dev/null 2>&1 \
    || die "installed OpenCode plugin failed validation"
  validate_klukai_env "$KLUKAI_ENV" \
    || die "installed Klukai environment failed validation"
  [[ "$(stat -c '%a' "$KLUKAI_ENV")" == "600" ]] \
    || die "installed Klukai environment permissions are not private"
  if ! grep -Fxq '[Service]' "$OPENCODE_DROPIN" \
    || ! grep -Fxq "Environment=OPENCODE_LMS_BASE_URL=${TARGET_BASE_URL}" "$OPENCODE_DROPIN"; then
    die "installed OpenCode sync drop-in failed validation"
  fi
}

apply_unit_changes() {
  local keepalive_state timer_state
  systemctl --user daemon-reload
  keepalive_state="$(unit_enabled_state "$KEEPALIVE_UNIT")"
  if [[ "$keepalive_state" != "not-found" ]]; then
    systemctl --user disable --now "$KEEPALIVE_UNIT" >/dev/null
  fi

  # Explicit restart allowlist. aichat.service is intentionally absent.
  timer_state="$(unit_active_state "$OPENCODE_TIMER_UNIT")"
  if [[ "$timer_state" == "active" || "$timer_state" == "activating" ]]; then
    systemctl --user restart "$OPENCODE_TIMER_UNIT"
  fi
}

verify_tailscale_and_ssh
load_cutover_tokens
probe_gateway
build_and_validate_candidates
report_aichat_repo_blockers

if [[ "$MODE" == "dry-run" ]]; then
  info "dry run passed: topology, SSH_CONNECTION, authenticated APIs, and candidates are valid"
  info "would update three OpenCode files and the private Klukai .env, install one token-free unit drop-in, and disable ${KEEPALIVE_UNIT}"
  info "restart allowlist: ${OPENCODE_TIMER_UNIT} (only when already active)"
  exit 0
fi

create_backup
info "exact pre-cutover backup created: ${ACTIVE_BACKUP}"
install_candidates
apply_unit_changes
COMMITTED=1
info "Amarillo OpenCode now targets ${TARGET_IP} over Tailscale"
info "Amarillo Klukai .env now contains the Tailscale LM/voice/facade endpoints and rotated tokens"
info "obsolete ${KEEPALIVE_UNIT} is disabled"
info "aichat code, config, and service were untouched; resolve its reported auth blocker first"
info "rollback command: $0 --rollback ${ACTIVE_BACKUP}"
