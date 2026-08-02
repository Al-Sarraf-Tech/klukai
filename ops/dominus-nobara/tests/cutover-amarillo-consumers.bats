#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
  SCRIPT="${REPO_ROOT}/ops/dominus-nobara/scripts/cutover-amarillo-consumers.sh"
  TEST_HOME="${BATS_TEST_TMPDIR}/home"
  TEST_REPO="${TEST_HOME}/git/aichat"
  TEST_KLUKAI_REPO="${TEST_HOME}/git/klukai"
  STUB_BIN="${BATS_TEST_TMPDIR}/bin"
  SYSTEMCTL_LOG="${BATS_TEST_TMPDIR}/systemctl.log"
  SYSTEMCTL_STATE="${BATS_TEST_TMPDIR}/systemctl.state"
  CURL_LOG="${BATS_TEST_TMPDIR}/curl.log"
  TEST_TOKEN="test-lm-token-never-print-0123456789abcdef"
  TEST_VOICE_TOKEN="test-voice-token-never-print-0123456789abcd"
  OAUTH_SENTINEL="oauth-value-never-print"
  ORIGINALS="${BATS_TEST_TMPDIR}/originals"

  mkdir -p \
    "${TEST_HOME}/.config/opencode/plugin" \
    "${TEST_HOME}/.config/aichat" \
    "${TEST_HOME}/.config/systemd/user" \
    "${TEST_REPO}" \
    "${TEST_KLUKAI_REPO}" \
    "$STUB_BIN" \
    "$ORIGINALS"

  cat >"${TEST_HOME}/.config/opencode/opencode.jsonc" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  // Preserve aliases and the full model catalog.
  "model": "lmstudio-dominus/gemma-4-26b-a4b-it",
  "small_model": "lmstudio-dominus/gemma-4-26b-a4b-it",
  "enabled_providers": ["lmstudio-dominus"],
  "plugin": ["${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs"],
  "provider": {
    "lmstudio-dominus": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LM Studio (192.168.50.2:1234)",
      "options": {
        "baseURL": "http://192.168.50.2:1234/v1",
        "apiKey": "{env:LMSTUDIO_API_KEY}",
      },
      "models": {
        "gemma-4-26b-a4b-it": {"name": "Gemma preserved"},
        "cognitivecomputations_dolphin-mistral-24b-venice-edition": {
          "name": "Klukai model preserved"
        },
      },
    },
  },
  "mcp": {
    "fleet": {
      "url": "http://127.0.0.1:9126/mcp",
      "oauth": {"clientSecret": "${OAUTH_SENTINEL}"},
    },
  },
}
EOF

  cat >"${TEST_HOME}/.config/opencode/opencode.jsonc.tmpl" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "lmstudio-dominus/gemma-4-26b-a4b-it",
  "small_model": "lmstudio-dominus/gemma-4-26b-a4b-it",
  "enabled_providers": ["lmstudio-dominus"],
  "plugin": ["${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs"],
  "provider": {
    "lmstudio-dominus": {
      "name": "LM Studio (192.168.50.2:1234)",
      "options": {
        "baseURL": "http://192.168.50.2:1234/v1",
        "apiKey": "{env:LMSTUDIO_API_KEY}",
      },
      __LMSTUDIO_MODELS__
    },
  },
  "mcp": {"fleet": {"oauth": {"clientSecret": "${OAUTH_SENTINEL}"}}},
}
EOF

  cat >"${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs" <<'EOF'
const DEFAULT_BASE_URL = "http://192.168.50.2:1234";
const PROVIDER_ID = "lmstudio-dominus";
function apiToken() {
  return process.env.LMSTUDIO_API_KEY || process.env.LM_API_TOKEN || "";
}
export default async function plugin() {
  return { baseURL: DEFAULT_BASE_URL, providerID: PROVIDER_ID, tokenPresent: Boolean(apiToken()) };
}
EOF

  cat >"${TEST_HOME}/.config/aichat/config.yml" <<'EOF'
active_personality: klukai
approval: AUTO
base_url: http://192.168.50.2:1234
model: cognitivecomputations_dolphin-mistral-24b-venice-edition
personalities:
  - id: klukai
    name: Klukai
    prompt: Preserve Klukai personality and relationship behavior exactly.
context_length: 35063
max_response_tokens: 4096
EOF

  cat >"${TEST_REPO}/docker-compose.yml" <<EOF
services:
  web:
    environment:
      LM_STUDIO_URL: http://100.78.39.76:1234
      OAUTH_SECRET: ${OAUTH_SENTINEL}
EOF
  cat >"${TEST_REPO}/.env" <<EOF
OAUTH_SECRET=${OAUTH_SENTINEL}
LM_STUDIO_URL=http://100.78.39.76:1234
EOF

  cat >"${TEST_KLUKAI_REPO}/.env" <<EOF
POSTGRES_PASSWORD=database-value-never-print
LM_STUDIO_URL=http://100.78.39.76:1234
LM_STUDIO_TOKEN=old-lm-value
VOICE_URL=http://100.78.39.76:8301
VOICE_API_TOKEN=old-voice-value
COMFYUI_URL=http://100.78.39.76:8388
UNRELATED_SECRET=${OAUTH_SENTINEL}
EOF
  chmod 0640 "${TEST_KLUKAI_REPO}/.env"

  cp -a "${TEST_HOME}/.config/opencode/opencode.jsonc" "${ORIGINALS}/opencode.jsonc"
  cp -a "${TEST_HOME}/.config/opencode/opencode.jsonc.tmpl" "${ORIGINALS}/opencode.jsonc.tmpl"
  cp -a "${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs" \
    "${ORIGINALS}/lmstudio-single-endpoint.mjs"
  cp -a "${TEST_HOME}/.config/aichat/config.yml" "${ORIGINALS}/aichat-config.yml"
  cp -a "${TEST_KLUKAI_REPO}/.env" "${ORIGINALS}/klukai.env"

  cat >"${STUB_BIN}/hostname" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-s" ]] || exit 2
printf 'amarillo\n'
EOF

  cat >"${STUB_BIN}/tailscale" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  status)
    printf '%s\n' '{"Peer":{"peer":{"TailscaleIPs":["100.107.121.5"]}}}'
    ;;
  ping)
    exit 0
    ;;
  ip)
    printf '100.111.198.19\n'
    ;;
  *)
    exit 2
    ;;
esac
EOF

  cat >"${STUB_BIN}/ssh" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-G" ]]; then
  printf 'hostname 100.107.121.5\nport 1227\n'
elif [[ "${STUB_BAD_SSH:-0}" == "1" ]]; then
  printf '100.111.198.19 54321 192.168.50.2 1227\n'
else
  printf '100.111.198.19 54321 100.107.121.5 1227\n'
fi
EOF

  cat >"${STUB_BIN}/curl" <<'EOF'
#!/usr/bin/env bash
output_file=""
header_file=""
url=""
while (($#)); do
  case "$1" in
    --output)
      output_file="$2"
      shift 2
      ;;
    --header)
      header_file="${2#@}"
      shift 2
      ;;
    http://*)
      url="$1"
      shift
      ;;
    --connect-timeout|--max-time|--proto)
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
[[ -n "$output_file" && -f "$header_file" ]] || exit 3
grep -Eq '^Authorization: Bearer [^[:space:]]+$' "$header_file" || exit 4
printf '%s\n' "$url" >>"$CURL_LOG"
[[ "${STUB_CURL_FAIL:-0}" != "1" ]] || exit 22
case "$url" in
  */api/v1/models) printf '%s\n' '{"models":[]}' >"$output_file" ;;
  */v1/models) printf '%s\n' '{"data":[]}' >"$output_file" ;;
  *) exit 5 ;;
esac
EOF

  cat >"${STUB_BIN}/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
source "$SYSTEMCTL_STATE"
[[ "${1:-}" == "--user" ]] || exit 2
shift
command_name="${1:-}"
shift || true
save_state() {
  cat >"$SYSTEMCTL_STATE" <<STATE
keepalive_enabled=${keepalive_enabled}
keepalive_active=${keepalive_active}
timer_active=${timer_active}
STATE
}
printf '%s\n' "$command_name $*" >>"$SYSTEMCTL_LOG"
case "$command_name" in
  is-enabled)
    [[ "$1" == "dominus-wsl-keepalive.service" ]] || exit 4
    printf '%s\n' "$keepalive_enabled"
    [[ "$keepalive_enabled" == "enabled" ]] || exit 1
    ;;
  is-active)
    case "$1" in
      dominus-wsl-keepalive.service) printf '%s\n' "$keepalive_active" ;;
      opencode-sync-models.timer) printf '%s\n' "$timer_active" ;;
      *) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  disable)
    [[ "${1:-}" == "--now" && "${2:-}" == "dominus-wsl-keepalive.service" ]] || exit 5
    [[ "${STUB_FAIL_DISABLE:-0}" != "1" ]] || exit 10
    keepalive_enabled=disabled
    keepalive_active=inactive
    save_state
    ;;
  enable)
    keepalive_enabled=enabled
    save_state
    ;;
  start)
    [[ "$1" == "dominus-wsl-keepalive.service" ]] || exit 6
    keepalive_active=active
    save_state
    ;;
  stop)
    [[ "$1" == "dominus-wsl-keepalive.service" ]] || exit 7
    keepalive_active=inactive
    save_state
    ;;
  restart)
    [[ "$1" == "opencode-sync-models.timer" ]] || exit 8
    ;;
  daemon-reload)
    ;;
  *)
    exit 9
    ;;
esac
EOF

  chmod 0755 "${STUB_BIN}/hostname" "${STUB_BIN}/tailscale" "${STUB_BIN}/ssh" \
    "${STUB_BIN}/curl" "${STUB_BIN}/systemctl"

  cat >"$SYSTEMCTL_STATE" <<'EOF'
keepalive_enabled=enabled
keepalive_active=active
timer_active=active
EOF
  : >"$SYSTEMCTL_LOG"
  : >"$CURL_LOG"

  export PATH="${STUB_BIN}:${PATH}"
  export TMPDIR="$BATS_TEST_TMPDIR"
  export CUTOVER_TEST_MODE=1
  export CUTOVER_TEST_HOME="$TEST_HOME"
  export CUTOVER_TEST_AICHAT_REPO="$TEST_REPO"
  export CUTOVER_TEST_KLUKAI_REPO="$TEST_KLUKAI_REPO"
  export CUTOVER_BACKUP_ROOT="${TEST_HOME}/backups"
  export LMSTUDIO_API_KEY="$TEST_TOKEN"
  export VOICE_API_TOKEN="$TEST_VOICE_TOKEN"
  unset LM_STUDIO_TOKEN LMSTUDIO_TOKEN_FILE VOICE_API_TOKEN_FILE \
    STUB_BAD_SSH STUB_CURL_FAIL STUB_FAIL_DISABLE
  export SYSTEMCTL_LOG SYSTEMCTL_STATE CURL_LOG
}

@test "dry run validates but does not mutate managed state or reveal credentials" {
  run "$SCRIPT"

  [ "$status" -eq 0 ]
  [[ "$output" == *"dry run passed"* ]]
  [[ "$output" == *"restart allowlist: opencode-sync-models.timer"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [[ "$output" != *"$TEST_VOICE_TOKEN"* ]]
  [[ "$output" != *"$OAUTH_SENTINEL"* ]]
  cmp -s "$ORIGINALS/opencode.jsonc" "${TEST_HOME}/.config/opencode/opencode.jsonc"
  cmp -s "$ORIGINALS/aichat-config.yml" "${TEST_HOME}/.config/aichat/config.yml"
  cmp -s "$ORIGINALS/klukai.env" "${TEST_KLUKAI_REPO}/.env"
  [ ! -e "${TEST_HOME}/.config/systemd/user/opencode-sync-models.service.d/20-dominus-nobara.conf" ]
  [ ! -d "${TEST_HOME}/backups" ]
  if grep -Eq '^(disable|restart|daemon-reload)' "$SYSTEMCTL_LOG"; then
    false
  fi
}

@test "apply backs up exact files and cuts over only the allowed consumers" {
  run "$SCRIPT" --apply

  [ "$status" -eq 0 ]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [[ "$output" != *"$TEST_VOICE_TOKEN"* ]]
  [[ "$output" != *"$OAUTH_SENTINEL"* ]]
  grep -Fq 'http://100.107.121.5:1234/v1' "${TEST_HOME}/.config/opencode/opencode.jsonc"
  grep -Fq 'lmstudio-dominus/gemma-4-26b-a4b-it' "${TEST_HOME}/.config/opencode/opencode.jsonc"
  grep -Fq 'Klukai model preserved' "${TEST_HOME}/.config/opencode/opencode.jsonc"
  grep -Fq "$OAUTH_SENTINEL" "${TEST_HOME}/.config/opencode/opencode.jsonc"
  grep -Fq 'const DEFAULT_BASE_URL = "http://100.107.121.5:1234";' \
    "${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs"
  cmp -s "$ORIGINALS/aichat-config.yml" "${TEST_HOME}/.config/aichat/config.yml"
  grep -Fq 'Preserve Klukai personality and relationship behavior exactly.' \
    "${TEST_HOME}/.config/aichat/config.yml"
  grep -Fxq 'LM_STUDIO_URL=http://100.107.121.5:1234' "${TEST_KLUKAI_REPO}/.env"
  grep -Fxq "LM_STUDIO_TOKEN=${TEST_TOKEN}" "${TEST_KLUKAI_REPO}/.env"
  grep -Fxq 'VOICE_URL=http://100.107.121.5:8301' "${TEST_KLUKAI_REPO}/.env"
  grep -Fxq "VOICE_API_TOKEN=${TEST_VOICE_TOKEN}" "${TEST_KLUKAI_REPO}/.env"
  grep -Fxq 'COMFYUI_URL=http://100.107.121.5:1234/api/v1/comfy' \
    "${TEST_KLUKAI_REPO}/.env"
  grep -Fxq "UNRELATED_SECRET=${OAUTH_SENTINEL}" "${TEST_KLUKAI_REPO}/.env"
  [ "$(stat -c '%a' "${TEST_KLUKAI_REPO}/.env")" = "600" ]

  backup_dir="$(find "${TEST_HOME}/backups" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -n "$backup_dir" ]
  cmp -s "$ORIGINALS/opencode.jsonc" "$backup_dir/files/opencode.jsonc"
  cmp -s "$ORIGINALS/opencode.jsonc.tmpl" "$backup_dir/files/opencode.jsonc.tmpl"
  cmp -s "$ORIGINALS/lmstudio-single-endpoint.mjs" \
    "$backup_dir/files/lmstudio-single-endpoint.mjs"
  cmp -s "$ORIGINALS/klukai.env" "$backup_dir/files/klukai.env"
  [ ! -e "$backup_dir/files/aichat-config.yml" ]
  [ "$(stat -c '%a' "$backup_dir")" = "700" ]

  grep -Fq 'Environment=OPENCODE_LMS_BASE_URL=http://100.107.121.5:1234' \
    "${TEST_HOME}/.config/systemd/user/opencode-sync-models.service.d/20-dominus-nobara.conf"
  grep -Fq 'disable --now dominus-wsl-keepalive.service' "$SYSTEMCTL_LOG"
  grep -Fq 'restart opencode-sync-models.timer' "$SYSTEMCTL_LOG"
  if grep -Fq 'aichat.service' "$SYSTEMCTL_LOG"; then
    false
  fi
}

@test "rollback restores exact files, absent drop-in, and prior unit state" {
  run "$SCRIPT" --apply
  [ "$status" -eq 0 ]
  backup_dir="$(find "${TEST_HOME}/backups" -mindepth 1 -maxdepth 1 -type d | head -1)"
  chmod 0600 "${TEST_HOME}/.config/opencode/opencode.jsonc"
  chmod 0600 "${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs"

  run "$SCRIPT" --rollback "$backup_dir"

  [ "$status" -eq 0 ]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [[ "$output" != *"$TEST_VOICE_TOKEN"* ]]
  [[ "$output" != *"$OAUTH_SENTINEL"* ]]
  cmp -s "$ORIGINALS/opencode.jsonc" "${TEST_HOME}/.config/opencode/opencode.jsonc"
  cmp -s "$ORIGINALS/opencode.jsonc.tmpl" "${TEST_HOME}/.config/opencode/opencode.jsonc.tmpl"
  cmp -s "$ORIGINALS/lmstudio-single-endpoint.mjs" \
    "${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs"
  cmp -s "$ORIGINALS/aichat-config.yml" "${TEST_HOME}/.config/aichat/config.yml"
  cmp -s "$ORIGINALS/klukai.env" "${TEST_KLUKAI_REPO}/.env"
  [ "$(stat -c '%a' "${TEST_HOME}/.config/opencode/opencode.jsonc")" = \
    "$(stat -c '%a' "$ORIGINALS/opencode.jsonc")" ]
  [ "$(stat -c '%a' "${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs")" = \
    "$(stat -c '%a' "$ORIGINALS/lmstudio-single-endpoint.mjs")" ]
  [ ! -e "${TEST_HOME}/.config/systemd/user/opencode-sync-models.service.d/20-dominus-nobara.conf" ]
  grep -Fq 'keepalive_enabled=enabled' "$SYSTEMCTL_STATE"
  grep -Fq 'keepalive_active=active' "$SYSTEMCTL_STATE"
  grep -Fq 'enable dominus-wsl-keepalive.service' "$SYSTEMCTL_LOG"
  grep -Fq 'start dominus-wsl-keepalive.service' "$SYSTEMCTL_LOG"
}

@test "wrong remote SSH_CONNECTION fails before backup or mutation" {
  export STUB_BAD_SSH=1
  run "$SCRIPT" --apply

  [ "$status" -ne 0 ]
  [[ "$output" == *"SSH_CONNECTION did not terminate"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [[ "$output" != *"$TEST_VOICE_TOKEN"* ]]
  [[ "$output" != *"$OAUTH_SENTINEL"* ]]
  cmp -s "$ORIGINALS/opencode.jsonc" "${TEST_HOME}/.config/opencode/opencode.jsonc"
  cmp -s "$ORIGINALS/klukai.env" "${TEST_KLUKAI_REPO}/.env"
  [ ! -d "${TEST_HOME}/backups" ]
  if grep -Eq '^(disable|restart|daemon-reload)' "$SYSTEMCTL_LOG"; then
    false
  fi
}

@test "failed authenticated gateway probe fails before backup or mutation" {
  export STUB_CURL_FAIL=1
  run "$SCRIPT" --apply

  [ "$status" -ne 0 ]
  [[ "$output" == *"authenticated OpenAI-compatible gateway probe failed"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [[ "$output" != *"$TEST_VOICE_TOKEN"* ]]
  [[ "$output" != *"$OAUTH_SENTINEL"* ]]
  cmp -s "$ORIGINALS/aichat-config.yml" "${TEST_HOME}/.config/aichat/config.yml"
  cmp -s "$ORIGINALS/klukai.env" "${TEST_KLUKAI_REPO}/.env"
  [ ! -d "${TEST_HOME}/backups" ]
  if grep -Eq '^(disable|restart|daemon-reload)' "$SYSTEMCTL_LOG"; then
    false
  fi
}

@test "dedicated token file with non-private mode is rejected" {
  token_file="${TEST_HOME}/lm-token"
  printf '%s\n' "$TEST_TOKEN" >"$token_file"
  chmod 0640 "$token_file"
  unset LMSTUDIO_API_KEY LM_STUDIO_TOKEN
  export LMSTUDIO_TOKEN_FILE="$token_file"

  run "$SCRIPT" --apply

  [ "$status" -ne 0 ]
  [[ "$output" == *"mode must be exactly 0600"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [ ! -d "${TEST_HOME}/backups" ]
}

@test "dedicated token symlink is rejected" {
  token_target="${TEST_HOME}/lm-token-target"
  token_link="${TEST_HOME}/lm-token-link"
  printf '%s\n' "$TEST_TOKEN" >"$token_target"
  chmod 0600 "$token_target"
  ln -s "$token_target" "$token_link"
  unset LMSTUDIO_API_KEY LM_STUDIO_TOKEN
  export LMSTUDIO_TOKEN_FILE="$token_link"

  run "$SCRIPT" --apply

  [ "$status" -ne 0 ]
  [[ "$output" == *"regular non-symlink"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [ ! -d "${TEST_HOME}/backups" ]
}

@test "dedicated token owned by another uid is rejected" {
  token_file="${TEST_HOME}/lm-token"
  printf '%s\n' "$TEST_TOKEN" >"$token_file"
  chmod 0600 "$token_file"
  unset LMSTUDIO_API_KEY LM_STUDIO_TOKEN
  export LMSTUDIO_TOKEN_FILE="$token_file"
  real_stat="$(type -P stat)"
  wrong_uid="$(( $(id -u) + 1 ))"
  cat >"${STUB_BIN}/stat" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "-c" && "\${2:-}" == "%u" && "\${4:-}" == "$token_file" ]]; then
  printf '%s\\n' '$wrong_uid'
  exit 0
fi
exec '$real_stat' "\$@"
EOF
  chmod 0755 "${STUB_BIN}/stat"

  run "$SCRIPT" --apply

  [ "$status" -ne 0 ]
  [[ "$output" == *"must be owned by the cutover user"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [ ! -d "${TEST_HOME}/backups" ]
}

@test "post-install unit failure automatically restores exact pre-cutover state" {
  export STUB_FAIL_DISABLE=1
  run "$SCRIPT" --apply

  [ "$status" -ne 0 ]
  [[ "$output" == *"apply failed; restoring the pre-cutover backup"* ]]
  [[ "$output" != *"$TEST_TOKEN"* ]]
  [[ "$output" != *"$TEST_VOICE_TOKEN"* ]]
  [[ "$output" != *"$OAUTH_SENTINEL"* ]]
  cmp -s "$ORIGINALS/opencode.jsonc" "${TEST_HOME}/.config/opencode/opencode.jsonc"
  cmp -s "$ORIGINALS/opencode.jsonc.tmpl" "${TEST_HOME}/.config/opencode/opencode.jsonc.tmpl"
  cmp -s "$ORIGINALS/lmstudio-single-endpoint.mjs" \
    "${TEST_HOME}/.config/opencode/plugin/lmstudio-single-endpoint.mjs"
  cmp -s "$ORIGINALS/aichat-config.yml" "${TEST_HOME}/.config/aichat/config.yml"
  cmp -s "$ORIGINALS/klukai.env" "${TEST_KLUKAI_REPO}/.env"
  [ ! -e "${TEST_HOME}/.config/systemd/user/opencode-sync-models.service.d/20-dominus-nobara.conf" ]
  grep -Fq 'keepalive_enabled=enabled' "$SYSTEMCTL_STATE"
  grep -Fq 'keepalive_active=active' "$SYSTEMCTL_STATE"
}
