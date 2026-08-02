#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
LOCK_FILE=${MODELS_LOCK_FILE:-"${SCRIPT_DIR}/../models.lock.json"}
STAGING_INPUT=${STAGING_ROOT:-${1:-}}
SSH_CONNECT_TIMEOUT_SECONDS=${SSH_CONNECT_TIMEOUT_SECONDS:-15}
SSH_KEEPALIVE_INTERVAL_SECONDS=${SSH_KEEPALIVE_INTERVAL_SECONDS:-30}
SSH_KEEPALIVE_COUNT_MAX=${SSH_KEEPALIVE_COUNT_MAX:-12}
RSYNC_IO_TIMEOUT_SECONDS=${RSYNC_IO_TIMEOUT_SECONDS:-300}
RSYNC_MAX_ATTEMPTS=${RSYNC_MAX_ATTEMPTS:-8}
RSYNC_RETRY_DELAY_SECONDS=${RSYNC_RETRY_DELAY_SECONDS:-20}

die() {
  echo "transfer-models: ERROR: $*" >&2
  exit 1
}

for command_name in jq sha256sum realpath rsync ssh tailscale cmp comm cut find sort mktemp; do
  command -v "${command_name}" >/dev/null || die "missing required command: ${command_name}"
done

[[ -r "${LOCK_FILE}" ]] || die "lock file is not readable: ${LOCK_FILE}"
jq empty "${LOCK_FILE}" || die "invalid JSON lock: ${LOCK_FILE}"
[[ -n "${STAGING_INPUT}" ]] || die "STAGING_ROOT or the first argument must name the dedicated staging root"

validate_bounded_integer() {
  local name=$1
  local value=$2
  local minimum=$3
  local maximum=$4
  [[ "${value}" =~ ^[0-9]+$ ]] || die "${name} must be an integer"
  (( value >= minimum && value <= maximum )) || \
    die "${name} must be between ${minimum} and ${maximum}"
}

validate_bounded_integer SSH_CONNECT_TIMEOUT_SECONDS "${SSH_CONNECT_TIMEOUT_SECONDS}" 5 120
validate_bounded_integer SSH_KEEPALIVE_INTERVAL_SECONDS "${SSH_KEEPALIVE_INTERVAL_SECONDS}" 5 300
validate_bounded_integer SSH_KEEPALIVE_COUNT_MAX "${SSH_KEEPALIVE_COUNT_MAX}" 1 30
validate_bounded_integer RSYNC_IO_TIMEOUT_SECONDS "${RSYNC_IO_TIMEOUT_SECONDS}" 60 1800
validate_bounded_integer RSYNC_MAX_ATTEMPTS "${RSYNC_MAX_ATTEMPTS}" 1 20
validate_bounded_integer RSYNC_RETRY_DELAY_SECONDS "${RSYNC_RETRY_DELAY_SECONDS}" 1 300

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
    -o ServerAliveCountMax="${SSH_KEEPALIVE_COUNT_MAX}" \
    -o ServerAliveInterval="${SSH_KEEPALIVE_INTERVAL_SECONDS}" \
    -o StrictHostKeyChecking=yes \
    "$@"
}

retry_rsync() {
  local description=$1
  shift
  local attempt status
  local rsync_rsh
  rsync_rsh="ssh -T -o AddressFamily=inet -o BatchMode=yes -o ClearAllForwardings=yes"
  rsync_rsh+=" -o Compression=no -o ConnectTimeout=${SSH_CONNECT_TIMEOUT_SECONDS}"
  rsync_rsh+=" -o ControlMaster=no -o ControlPath=none -o ForwardAgent=no"
  rsync_rsh+=" -o ServerAliveCountMax=${SSH_KEEPALIVE_COUNT_MAX}"
  rsync_rsh+=" -o ServerAliveInterval=${SSH_KEEPALIVE_INTERVAL_SECONDS}"
  rsync_rsh+=" -o StrictHostKeyChecking=yes"

  for (( attempt = 1; attempt <= RSYNC_MAX_ATTEMPTS; attempt++ )); do
    echo "transfer-models: ${description}: rsync attempt ${attempt}/${RSYNC_MAX_ATTEMPTS}"
    if command rsync \
      --rsh="${rsync_rsh}" \
      --timeout="${RSYNC_IO_TIMEOUT_SECONDS}" \
      "$@"; then
      return 0
    else
      status=$?
    fi
    if (( attempt == RSYNC_MAX_ATTEMPTS )); then
      echo "transfer-models: ${description}: rsync exhausted retries (exit ${status})" >&2
      return "${status}"
    fi
    echo "transfer-models: ${description}: retrying in ${RSYNC_RETRY_DELAY_SECONDS}s" >&2
    sleep "${RSYNC_RETRY_DELAY_SECONDS}"
  done
}

STAGING_ROOT=$(realpath -m -- "${STAGING_INPUT}")
TARGET_SSH_HOST=${TARGET_SSH_HOST:-$(jq -er '.target.ssh_host' "${LOCK_FILE}")}
TARGET_TAILSCALE_IPV4=$(jq -er '.target.tailscale_ipv4' "${LOCK_FILE}")
TARGET_RAID_MOUNT=${TARGET_RAID_MOUNT:-$(jq -er '.target.raid_mountpoint' "${LOCK_FILE}")}
TARGET_RELEASE_ROOT=${TARGET_RELEASE_ROOT:-$(jq -er '.target.release_root' "${LOCK_FILE}")}

TARGET_RAID_MOUNT=$(realpath -m -- "${TARGET_RAID_MOUNT}")
TARGET_RELEASE_ROOT=$(realpath -m -- "${TARGET_RELEASE_ROOT}")

[[ "${TARGET_SSH_HOST}" =~ ^[A-Za-z0-9._-]+$ ]] || die "unsafe TARGET_SSH_HOST"
[[ "${TARGET_TAILSCALE_IPV4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid locked target Tailscale IPv4"
[[ "${TARGET_RAID_MOUNT}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "unsafe TARGET_RAID_MOUNT"
[[ "${TARGET_RELEASE_ROOT}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "unsafe TARGET_RELEASE_ROOT"
[[ "${TARGET_RELEASE_ROOT}" == "${TARGET_RAID_MOUNT}/"* ]] || die "target release must be below the locked RAID mount"
[[ "${TARGET_RELEASE_ROOT}" != "${TARGET_RAID_MOUNT}" ]] || die "target release cannot be the RAID mount itself"

RESOLVED_SSH_HOST=$(ssh -G \
  -o AddressFamily=inet \
  -o BatchMode=yes \
  -o Compression=no \
  -o ConnectTimeout="${SSH_CONNECT_TIMEOUT_SECONDS}" \
  -o ControlMaster=no \
  -o ControlPath=none \
  "${TARGET_SSH_HOST}" 2>/dev/null | awk '$1 == "hostname" {print $2; exit}')
[[ "${RESOLVED_SSH_HOST}" == "${TARGET_TAILSCALE_IPV4}" ]] || \
  die "SSH host ${TARGET_SSH_HOST} resolves to ${RESOLVED_SSH_HOST:-nothing}, not locked Tailscale IP ${TARGET_TAILSCALE_IPV4}"
SOURCE_TAILSCALE_IPV4=$(tailscale ip -4 | head -n 1)
[[ "${SOURCE_TAILSCALE_IPV4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
  die "cannot determine Amarillo Tailscale IPv4"

# Re-hash locally immediately before transfer. This also refreshes the manifests.
STAGING_ROOT="${STAGING_ROOT}" MODELS_LOCK_FILE="${LOCK_FILE}" \
  "${SCRIPT_DIR}/verify-models.sh"

for required_file in SHA256SUMS FILE_SIZES.tsv models.lock.json models.lock.sha256 LOCAL-VERIFIED; do
  [[ -f "${STAGING_ROOT}/${required_file}" ]] || die "missing staged manifest: ${required_file}"
done

LOCK_SHA=$(<"${STAGING_ROOT}/models.lock.sha256")
[[ "${LOCK_SHA}" =~ ^[0-9a-f]{64}$ ]] || die "invalid staged lock SHA-256"

echo "transfer-models: preflighting ${TARGET_SSH_HOST}:${TARGET_RELEASE_ROOT}"
ssh_transport "${TARGET_SSH_HOST}" bash -s -- \
  "${TARGET_RAID_MOUNT}" "${TARGET_RELEASE_ROOT}" "${LOCK_SHA}" \
  "${SOURCE_TAILSCALE_IPV4}" "${TARGET_TAILSCALE_IPV4}" <<'REMOTE_PREFLIGHT'
set -Eeuo pipefail
raid_mount=$1
release_root=$2
lock_sha=$3
expected_client_ip=$4
expected_server_ip=$5

read -r connected_client_ip _ connected_server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${connected_client_ip}" == "${expected_client_ip}" && "${connected_server_ip}" == "${expected_server_ip}" ]] || {
  echo "refusing non-Tailscale SSH path: ${connected_client_ip:-unknown} -> ${connected_server_ip:-unknown}" >&2
  exit 1
}

mountpoint -q -- "${raid_mount}" || {
  echo "target RAID is not mounted: ${raid_mount}" >&2
  exit 1
}
[[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]] || {
  echo "target path is not the expected mountpoint: ${raid_mount}" >&2
  exit 1
}

if [[ -d "${release_root}" && ! -f "${release_root}/.manifest/models.lock.sha256" ]]; then
  if [[ -n "$(find "${release_root}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    echo "refusing non-empty unmarked target release: ${release_root}" >&2
    exit 1
  fi
fi

mkdir -p -- "${release_root}/.manifest"
if [[ -f "${release_root}/.manifest/models.lock.sha256" ]]; then
  [[ "$(<"${release_root}/.manifest/models.lock.sha256")" == "${lock_sha}" ]] || {
    echo "target release belongs to a different lock" >&2
    exit 1
  }
else
  printf '%s\n' "${lock_sha}" >"${release_root}/.manifest/models.lock.sha256"
fi
REMOTE_PREFLIGHT

echo "transfer-models: rsyncing payload without --delete"
retry_rsync "locked payload" \
  --archive --hard-links --checksum --partial --append-verify --protect-args \
  --human-readable --info=progress2 \
  "${STAGING_ROOT}/payload/" \
  "${TARGET_SSH_HOST}:${TARGET_RELEASE_ROOT}/"

retry_rsync "verification manifests" \
  --archive --checksum --partial --append-verify --protect-args \
  "${STAGING_ROOT}/SHA256SUMS" \
  "${STAGING_ROOT}/FILE_SIZES.tsv" \
  "${STAGING_ROOT}/models.lock.json" \
  "${STAGING_ROOT}/models.lock.sha256" \
  "${STAGING_ROOT}/LOCAL-VERIFIED" \
  "${TARGET_SSH_HOST}:${TARGET_RELEASE_ROOT}/.manifest/"

echo "transfer-models: verifying every remote size and SHA-256"
ssh_transport "${TARGET_SSH_HOST}" bash -s -- \
  "${TARGET_RELEASE_ROOT}" "${LOCK_SHA}" \
  "${SOURCE_TAILSCALE_IPV4}" "${TARGET_TAILSCALE_IPV4}" <<'REMOTE_VERIFY'
set -Eeuo pipefail
release_root=$1
expected_lock_sha=$2
expected_client_ip=$3
expected_server_ip=$4
read -r connected_client_ip _ connected_server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${connected_client_ip}" == "${expected_client_ip}" && "${connected_server_ip}" == "${expected_server_ip}" ]] || {
  echo "refusing non-Tailscale SSH path during verification" >&2
  exit 1
}
cd -- "${release_root}"

[[ "$(<.manifest/models.lock.sha256)" == "${expected_lock_sha}" ]] || {
  echo "remote lock hash marker mismatch" >&2
  exit 1
}
[[ "$(sha256sum .manifest/models.lock.json | awk '{print $1}')" == "${expected_lock_sha}" ]] || {
  echo "remote lock file hash mismatch" >&2
  exit 1
}

while IFS=$'\t' read -r expected_size relative_path; do
  [[ -f "${relative_path}" ]] || {
    echo "remote file missing: ${relative_path}" >&2
    exit 1
  }
  actual_size=$(stat -c '%s' -- "${relative_path}")
  [[ "${actual_size}" == "${expected_size}" ]] || {
    echo "remote size mismatch: ${relative_path}; expected ${expected_size}, got ${actual_size}" >&2
    exit 1
  }
done <.manifest/FILE_SIZES.tsv

sha256sum --check --strict .manifest/SHA256SUMS

expected_paths=$(mktemp)
actual_paths=$(mktemp)
trap 'rm -f -- "${expected_paths}" "${actual_paths}"' EXIT
cut -f 2- .manifest/FILE_SIZES.tsv | LC_ALL=C sort >"${expected_paths}"
find . -path './.manifest' -prune -o -type f -printf '%P\n' | LC_ALL=C sort >"${actual_paths}"
if ! cmp --silent "${expected_paths}" "${actual_paths}"; then
  echo "remote release contains missing or unlocked extra files:" >&2
  comm -3 "${expected_paths}" "${actual_paths}" >&2
  exit 1
fi
printf '%s\t%s\n' "${expected_lock_sha}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >.manifest/REMOTE-VERIFIED
REMOTE_VERIFY

echo "transfer-models: remote release verified: ${TARGET_SSH_HOST}:${TARGET_RELEASE_ROOT}"
echo "transfer-models: no stable symlink was changed and staging was retained for owner acceptance"
