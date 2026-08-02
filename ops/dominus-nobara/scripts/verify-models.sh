#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
LOCK_FILE=${MODELS_LOCK_FILE:-"${SCRIPT_DIR}/../models.lock.json"}
STAGING_INPUT=${STAGING_ROOT:-${1:-}}
VERIFY_JOBS=${VERIFY_JOBS:-4}

die() {
  echo "verify-models: ERROR: $*" >&2
  exit 1
}

for command_name in jq sha256sum stat realpath base64 xargs sort find cmp comm; do
  command -v "${command_name}" >/dev/null || die "missing required command: ${command_name}"
done

[[ -r "${LOCK_FILE}" ]] || die "lock file is not readable: ${LOCK_FILE}"
jq empty "${LOCK_FILE}" || die "invalid JSON lock: ${LOCK_FILE}"
[[ -n "${STAGING_INPUT}" ]] || die "STAGING_ROOT or the first argument must name the dedicated staging root"
[[ "${VERIFY_JOBS}" =~ ^[0-9]+$ ]] || die "VERIFY_JOBS must be an integer"
(( VERIFY_JOBS >= 1 && VERIFY_JOBS <= 20 )) || die "VERIFY_JOBS must be between 1 and 20"

STAGING_ROOT=$(realpath -m -- "${STAGING_INPUT}")
PAYLOAD_ROOT="${STAGING_ROOT}/payload"
LOCK_ID=$(jq -er '.lock_id' "${LOCK_FILE}")
MARKER_NAME=$(jq -er '.staging.marker' "${LOCK_FILE}")
[[ -f "${STAGING_ROOT}/${MARKER_NAME}" ]] || die "staging marker is missing"
[[ "$(<"${STAGING_ROOT}/${MARKER_NAME}")" == "${LOCK_ID}" ]] || die "staging marker belongs to a different lock"
[[ -d "${PAYLOAD_ROOT}" ]] || die "payload directory is missing: ${PAYLOAD_ROOT}"

emit_entries() {
  jq -c '
    (.artifacts[] | select(.enabled == true) |
      {id, destination, size_bytes, sha256}),
    (.snapshots[] | select(.enabled == true) as $snapshot |
      $snapshot.files[] |
      {
        id: ($snapshot.id + ":" + .path),
        destination: ($snapshot.destination + "/" + .path),
        size_bytes,
        sha256
      })
  ' "${LOCK_FILE}"
}

verify_entry() {
  local encoded=$1
  local entry id destination expected_size expected_sha file actual_size actual_sha
  entry=$(printf '%s' "${encoded}" | base64 --decode)
  id=$(jq -er '.id' <<<"${entry}")
  destination=$(jq -er '.destination' <<<"${entry}")
  expected_size=$(jq -er '.size_bytes' <<<"${entry}")
  expected_sha=$(jq -er '.sha256' <<<"${entry}")
  file="${PAYLOAD_ROOT}/${destination}"

  if [[ ! -f "${file}" ]]; then
    echo "verify-models: missing ${id}: ${destination}" >&2
    return 1
  fi
  actual_size=$(stat -c '%s' -- "${file}")
  if [[ "${actual_size}" != "${expected_size}" ]]; then
    echo "verify-models: size mismatch ${id}: expected ${expected_size}, got ${actual_size}" >&2
    return 1
  fi
  actual_sha=$(sha256sum -- "${file}" | awk '{print $1}')
  if [[ "${actual_sha}" != "${expected_sha}" ]]; then
    echo "verify-models: SHA-256 mismatch ${id}: ${destination}" >&2
    return 1
  fi
  echo "verify-models: ok ${id}"
}

export PAYLOAD_ROOT
export -f verify_entry

# The positional parameter is intentionally expanded by each xargs child shell.
# shellcheck disable=SC2016
if ! emit_entries | jq -r '@base64' |
  xargs -r -n 1 -P "${VERIFY_JOBS}" bash -c 'verify_entry "$1"' _; then
  die "payload verification failed"
fi

TMP_MANIFEST_DIR=$(mktemp -d --tmpdir="${STAGING_ROOT}" .verify-manifest.XXXXXX)
trap 'rm -rf -- "${TMP_MANIFEST_DIR}"' EXIT

emit_entries | jq -r '.destination' | LC_ALL=C sort >"${TMP_MANIFEST_DIR}/expected-paths"
find "${PAYLOAD_ROOT}" -type f -printf '%P\n' | LC_ALL=C sort >"${TMP_MANIFEST_DIR}/actual-paths"
if ! cmp --silent "${TMP_MANIFEST_DIR}/expected-paths" "${TMP_MANIFEST_DIR}/actual-paths"; then
  echo "verify-models: payload contains missing or unlocked extra files:" >&2
  comm -3 "${TMP_MANIFEST_DIR}/expected-paths" "${TMP_MANIFEST_DIR}/actual-paths" >&2
  die "payload file set differs from the lock"
fi

emit_entries |
  jq -r '[.sha256, .destination] | @tsv' |
  LC_ALL=C sort -t $'\t' -k2,2 |
  awk -F $'\t' '{print $1 "  " $2}' >"${TMP_MANIFEST_DIR}/SHA256SUMS"

emit_entries |
  jq -r '[.size_bytes, .destination] | @tsv' |
  LC_ALL=C sort -t $'\t' -k2,2 >"${TMP_MANIFEST_DIR}/FILE_SIZES.tsv"

cp -- "${LOCK_FILE}" "${TMP_MANIFEST_DIR}/models.lock.json"
sha256sum -- "${TMP_MANIFEST_DIR}/models.lock.json" |
  awk '{print $1}' >"${TMP_MANIFEST_DIR}/models.lock.sha256"
printf '%s\t%s\n' "${LOCK_ID}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${TMP_MANIFEST_DIR}/LOCAL-VERIFIED"

mv -- "${TMP_MANIFEST_DIR}/SHA256SUMS" "${STAGING_ROOT}/SHA256SUMS"
mv -- "${TMP_MANIFEST_DIR}/FILE_SIZES.tsv" "${STAGING_ROOT}/FILE_SIZES.tsv"
mv -- "${TMP_MANIFEST_DIR}/models.lock.json" "${STAGING_ROOT}/models.lock.json"
mv -- "${TMP_MANIFEST_DIR}/models.lock.sha256" "${STAGING_ROOT}/models.lock.sha256"
mv -- "${TMP_MANIFEST_DIR}/LOCAL-VERIFIED" "${STAGING_ROOT}/LOCAL-VERIFIED"

echo "verify-models: verified all enabled files and wrote SHA256SUMS/FILE_SIZES.tsv"
