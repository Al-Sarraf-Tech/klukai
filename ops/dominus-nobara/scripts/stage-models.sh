#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
LOCK_FILE=${MODELS_LOCK_FILE:-"${SCRIPT_DIR}/../models.lock.json"}
STAGING_INPUT=${STAGING_ROOT:-${1:-}}
DOWNLOAD_JOBS=${DOWNLOAD_JOBS:-4}
SSH_CONNECT_TIMEOUT_SECONDS=${SSH_CONNECT_TIMEOUT_SECONDS:-15}
SSH_KEEPALIVE_INTERVAL_SECONDS=${SSH_KEEPALIVE_INTERVAL_SECONDS:-30}
SSH_KEEPALIVE_COUNT_MAX=${SSH_KEEPALIVE_COUNT_MAX:-12}
RSYNC_IO_TIMEOUT_SECONDS=${RSYNC_IO_TIMEOUT_SECONDS:-300}
RSYNC_MAX_ATTEMPTS=${RSYNC_MAX_ATTEMPTS:-6}
RSYNC_RETRY_DELAY_SECONDS=${RSYNC_RETRY_DELAY_SECONDS:-20}

usage() {
  echo "usage: STAGING_ROOT=/dedicated/path [DOWNLOAD_JOBS=1..20] $0" >&2
}

die() {
  echo "stage-models: ERROR: $*" >&2
  exit 1
}

for command_name in jq curl sha256sum stat realpath rsync base64 xargs ssh tailscale awk; do
  command -v "${command_name}" >/dev/null || die "missing required command: ${command_name}"
done

[[ -r "${LOCK_FILE}" ]] || die "lock file is not readable: ${LOCK_FILE}"
jq empty "${LOCK_FILE}" || die "invalid JSON lock: ${LOCK_FILE}"
[[ -n "${STAGING_INPUT}" ]] || { usage; die "STAGING_ROOT must be explicit"; }
[[ "${DOWNLOAD_JOBS}" =~ ^[0-9]+$ ]] || die "DOWNLOAD_JOBS must be an integer"
(( DOWNLOAD_JOBS >= 1 && DOWNLOAD_JOBS <= 20 )) || die "DOWNLOAD_JOBS must be between 1 and 20"

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

STAGING_ROOT=$(realpath -m -- "${STAGING_INPUT}")
case "${STAGING_ROOT}" in
  /|/home|/home/jalsarraf|/mnt|/mnt/nvmer0|/mnt/satar0)
    die "refusing broad or live-model staging path: ${STAGING_ROOT}"
    ;;
esac

LOCK_ID=$(jq -er '.lock_id' "${LOCK_FILE}")
MARKER_NAME=$(jq -er '.staging.marker' "${LOCK_FILE}")
LOCKED_TARGET_SSH_HOST=$(jq -er '.target.ssh_host' "${LOCK_FILE}")
LOCKED_TARGET_TAILSCALE_IPV4=$(jq -er '.target.tailscale_ipv4' "${LOCK_FILE}")
SOURCE_TAILSCALE_IPV4=$(tailscale ip -4 | head -n 1)
[[ "${MARKER_NAME}" != */* ]] || die "unsafe staging marker in lock"
[[ "${LOCKED_TARGET_TAILSCALE_IPV4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid locked target Tailscale IPv4"
[[ "${SOURCE_TAILSCALE_IPV4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "cannot determine Amarillo Tailscale IPv4"

if [[ -d "${STAGING_ROOT}" && ! -f "${STAGING_ROOT}/${MARKER_NAME}" ]]; then
  if [[ -n "$(find "${STAGING_ROOT}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    die "existing non-empty directory lacks ${MARKER_NAME}; choose the dedicated staging root"
  fi
fi

mkdir -p -- "${STAGING_ROOT}"
if [[ -f "${STAGING_ROOT}/${MARKER_NAME}" ]]; then
  [[ "$(<"${STAGING_ROOT}/${MARKER_NAME}")" == "${LOCK_ID}" ]] || die "staging marker belongs to a different lock"
else
  printf '%s\n' "${LOCK_ID}" >"${STAGING_ROOT}/${MARKER_NAME}"
fi

PAYLOAD_ROOT="${STAGING_ROOT}/payload"
REJECTED_ROOT="${STAGING_ROOT}/rejected"
mkdir -p -- "${PAYLOAD_ROOT}" "${REJECTED_ROOT}"

# Every enabled entry must be fully verifiable before any network work begins.
jq -e '
  ([(.artifacts[] | select(.enabled == true)),
     (.snapshots[] | select(.enabled == true) as $s | $s.files[] |
       {destination: ($s.destination + "/" + .path), size_bytes, sha256})]
   | all(.destination | type == "string" and length > 0)
   and all(.size_bytes | type == "number" and . >= 0)
   and all(.sha256 | type == "string" and test("^[0-9a-f]{64}$")))
' "${LOCK_FILE}" >/dev/null || die "enabled lock entries require destination, size, and SHA-256"

emit_entries() {
  jq -c '
    (.artifacts[] | select(.enabled == true) |
      {id, destination, size_bytes, sha256, source}),
    (.snapshots[] | select(.enabled == true) as $snapshot |
      $snapshot.files[] |
      {
        id: ($snapshot.id + ":" + .path),
        destination: ($snapshot.destination + "/" + .path),
        size_bytes,
        sha256,
        source: {
          type: "huggingface_file",
          repo: $snapshot.source.repo,
          revision: $snapshot.source.revision,
          file: .path
        }
      })
  ' "${LOCK_FILE}"
}

if emit_entries | jq -sr 'group_by(.destination) | any(length > 1)' | grep -qx true; then
  die "duplicate enabled destinations in lock"
fi

quarantine_file() {
  local file=$1
  local id=$2
  [[ -e "${file}" ]] || return 0
  local safe_id=${id//[^A-Za-z0-9._-]/_}
  local rejected
  rejected="${REJECTED_ROOT}/${safe_id}.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mv -- "${file}" "${rejected}"
  echo "stage-models: preserved invalid file as ${rejected}" >&2
}

verify_exact() {
  local file=$1
  local expected_size=$2
  local expected_sha=$3
  [[ -f "${file}" ]] || return 1
  [[ "$(stat -c '%s' -- "${file}")" == "${expected_size}" ]] || return 1
  [[ "$(sha256sum -- "${file}" | awk '{print $1}')" == "${expected_sha}" ]]
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
    echo "stage-models: ${description}: rsync attempt ${attempt}/${RSYNC_MAX_ATTEMPTS}"
    if command rsync \
      --rsh="${rsync_rsh}" \
      --timeout="${RSYNC_IO_TIMEOUT_SECONDS}" \
      "$@"; then
      return 0
    else
      status=$?
    fi
    if (( attempt == RSYNC_MAX_ATTEMPTS )); then
      echo "stage-models: ${description}: rsync exhausted retries (exit ${status})" >&2
      return "${status}"
    fi
    echo "stage-models: ${description}: retrying in ${RSYNC_RETRY_DELAY_SECONDS}s" >&2
    sleep "${RSYNC_RETRY_DELAY_SECONDS}"
  done
}

download_huggingface() {
  local repo=$1
  local revision=$2
  local source_file=$3
  local output=$4
  local url="https://huggingface.co/${repo}/resolve/${revision}/${source_file}?download=true"
  local -a curl_args=(
    --fail --location --silent --show-error
    --retry 10 --retry-delay 2 --retry-all-errors
    --continue-at - --output "${output}"
  )

  if [[ -n "${HF_TOKEN:-}" ]]; then
    # Read the header from stdin so the token never appears in curl's argv or logs.
    printf 'Authorization: Bearer %s\n' "${HF_TOKEN}" |
      curl "${curl_args[@]}" --header @- "${url}"
  else
    curl "${curl_args[@]}" "${url}"
  fi
}

stage_entry() {
  local encoded=$1
  local entry id destination expected_size expected_sha source_type final part
  entry=$(printf '%s' "${encoded}" | base64 --decode)
  id=$(jq -er '.id' <<<"${entry}")
  destination=$(jq -er '.destination' <<<"${entry}")
  expected_size=$(jq -er '.size_bytes' <<<"${entry}")
  expected_sha=$(jq -er '.sha256' <<<"${entry}")
  source_type=$(jq -er '.source.type' <<<"${entry}")

  [[ "${destination}" != /* && "${destination}" != *"../"* && "${destination}" != ".." ]] || {
    echo "stage-models: unsafe destination for ${id}: ${destination}" >&2
    return 1
  }

  final="${PAYLOAD_ROOT}/${destination}"
  part="${final}.part"
  mkdir -p -- "$(dirname -- "${final}")"

  if verify_exact "${final}" "${expected_size}" "${expected_sha}"; then
    echo "stage-models: verified existing ${id}"
    return 0
  fi
  quarantine_file "${final}" "${id}.final"

  if verify_exact "${part}" "${expected_size}" "${expected_sha}"; then
    mv -- "${part}" "${final}"
    echo "stage-models: promoted complete partial ${id}"
    return 0
  fi
  if [[ -f "${part}" ]] && (( $(stat -c '%s' -- "${part}") > expected_size )); then
    quarantine_file "${part}" "${id}.oversize-part"
  fi

  echo "stage-models: acquiring ${id}"
  case "${source_type}" in
    huggingface_file)
      local repo revision source_file
      repo=$(jq -er '.source.repo' <<<"${entry}")
      revision=$(jq -er '.source.revision' <<<"${entry}")
      source_file=$(jq -er '.source.file' <<<"${entry}")
      download_huggingface "${repo}" "${revision}" "${source_file}" "${part}"
      ;;
    local_file)
      local source_path
      source_path=$(jq -er '.source.path' <<<"${entry}")
      verify_exact "${source_path}" "${expected_size}" "${expected_sha}" || {
        echo "stage-models: local source failed lock verification for ${id}: ${source_path}" >&2
        return 1
      }
      quarantine_file "${part}" "${id}.stale-part"
      cp --reflink=auto --sparse=always -- "${source_path}" "${part}"
      ;;
    remote_file)
      local source_host source_path resolved_source_host
      source_host=$(jq -er '.source.ssh_host' <<<"${entry}")
      source_path=$(jq -er '.source.path' <<<"${entry}")
      [[ "${source_host}" =~ ^[A-Za-z0-9._-]+$ && "${source_path}" == /* ]] || {
        echo "stage-models: unsafe remote source for ${id}" >&2
        return 1
      }
      [[ "${source_host}" == "${LOCKED_TARGET_SSH_HOST}" ]] || {
        echo "stage-models: remote source is not the locked Tailscale target for ${id}" >&2
        return 1
      }
      resolved_source_host=$(ssh -G \
        -o AddressFamily=inet \
        -o BatchMode=yes \
        -o Compression=no \
        -o ConnectTimeout="${SSH_CONNECT_TIMEOUT_SECONDS}" \
        -o ControlMaster=no \
        -o ControlPath=none \
        "${source_host}" 2>/dev/null | awk '$1 == "hostname" {print $2; exit}')
      [[ "${resolved_source_host}" == "${LOCKED_TARGET_TAILSCALE_IPV4}" ]] || {
        echo "stage-models: remote source does not resolve to locked Tailscale IP for ${id}" >&2
        return 1
      }
      ssh_transport "${source_host}" bash -s -- \
        "${SOURCE_TAILSCALE_IPV4}" "${LOCKED_TARGET_TAILSCALE_IPV4}" <<'REMOTE_TAILSCALE_CHECK'
set -Eeuo pipefail
read -r connected_client_ip _ connected_server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${connected_client_ip}" == "$1" && "${connected_server_ip}" == "$2" ]] || {
  echo "refusing non-Tailscale remote model source path" >&2
  exit 1
}
REMOTE_TAILSCALE_CHECK
      retry_rsync "remote source ${id}" \
        --archive --partial --append-verify --protect-args \
        "${source_host}:${source_path}" "${part}"
      ;;
    *)
      echo "stage-models: unsupported source type for ${id}: ${source_type}" >&2
      return 1
      ;;
  esac

  if ! verify_exact "${part}" "${expected_size}" "${expected_sha}"; then
    quarantine_file "${part}" "${id}.failed-verification"
    echo "stage-models: size/SHA-256 verification failed for ${id}" >&2
    return 1
  fi
  mv -- "${part}" "${final}"
  echo "stage-models: staged ${id}"
}

export PAYLOAD_ROOT REJECTED_ROOT LOCKED_TARGET_SSH_HOST LOCKED_TARGET_TAILSCALE_IPV4 SOURCE_TAILSCALE_IPV4
export SSH_CONNECT_TIMEOUT_SECONDS SSH_KEEPALIVE_INTERVAL_SECONDS SSH_KEEPALIVE_COUNT_MAX
export RSYNC_IO_TIMEOUT_SECONDS RSYNC_MAX_ATTEMPTS RSYNC_RETRY_DELAY_SECONDS
export -f quarantine_file verify_exact ssh_transport retry_rsync download_huggingface stage_entry

ENTRY_COUNT=$(emit_entries | jq -s 'length')
TOTAL_BYTES=$(emit_entries | jq -s 'map(.size_bytes) | add // 0')
echo "stage-models: ${ENTRY_COUNT} files, ${TOTAL_BYTES} locked bytes, ${DOWNLOAD_JOBS} workers"

# The positional parameter is intentionally expanded by each xargs child shell.
# shellcheck disable=SC2016
if ! emit_entries | jq -r '@base64' |
  xargs -r -n 1 -P "${DOWNLOAD_JOBS}" bash -c 'stage_entry "$1"' _; then
  die "one or more artifacts failed; verified files and resumable partials were retained"
fi

STAGING_ROOT="${STAGING_ROOT}" MODELS_LOCK_FILE="${LOCK_FILE}" \
  "${SCRIPT_DIR}/verify-models.sh"

echo "stage-models: complete and verified at ${STAGING_ROOT}"
echo "stage-models: staging is intentionally retained; do not remove it until remote verification and owner acceptance"
