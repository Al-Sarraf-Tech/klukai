#!/usr/bin/env bash
# Verify the newest Amarillo backup by restoring it into a unique scratch
# PostgreSQL container. Production services and production data are never
# modified by this drill.
#
# Usage: scripts/restore-from-backup.sh [--keep]
# Exit: 0 verified, 1 failed, 2 no recent backup.

set -Eeuo pipefail

LOCAL_MOUNT="${LOCAL_MOUNT:-/mnt/nvmeINT}"
TMP_ROOT="${TMP_ROOT:-/mnt/nvmeINT/tmp}"
LOG_DIR="${LOG_DIR:-/mnt/nvmeINT/logs}"
REPORT_FILE="${REPORT_FILE:-/mnt/nvmeINT/logs/restore-drill.json}"
DEST_HOST="${DEST_HOST:-dominus-nobara}"
DEST_TAILSCALE_IPV4="${DEST_TAILSCALE_IPV4:-100.107.121.5}"
SOURCE_TAILSCALE_IPV4="${SOURCE_TAILSCALE_IPV4:-100.111.198.19}"
DEST_RAID_MOUNT="${DEST_RAID_MOUNT:-/mnt/nvmer0}"
DEST_PATH="${DEST_PATH:-/mnt/nvmer0/services/ai-stack/backups/amarillo/klukai}"
DEST_MARKER=".klukai-offsite-backups-v1"
PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"
RETENTION_DAYS_MAX="${RETENTION_DAYS_MAX:-2}"
SSH_CONNECT_TIMEOUT_SECONDS="${SSH_CONNECT_TIMEOUT_SECONDS:-15}"
KEEP_ARTIFACTS=false

for arg in "$@"; do
  case "${arg}" in
    --keep) KEEP_ARTIFACTS=true ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "restore-from-backup: unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

die_early() {
  echo "restore-from-backup: ERROR: $*" >&2
  exit 1
}

for command_name in awk basename chmod date dirname docker find findmnt grep gzip install jq mktemp mountpoint mv realpath rm sed sha256sum sleep sort ssh stat tail tailscale tar tee tr; do
  command -v "${command_name}" >/dev/null || die_early "missing required command: ${command_name}"
done
if ! [[ "${RETENTION_DAYS_MAX}" =~ ^[0-9]+$ ]] || (( RETENTION_DAYS_MAX < 1 )); then
  die_early "RETENTION_DAYS_MAX must be a positive integer"
fi
if ! [[ "${SSH_CONNECT_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || \
   (( SSH_CONNECT_TIMEOUT_SECONDS < 5 || SSH_CONNECT_TIMEOUT_SECONDS > 120 )); then
  die_early "SSH_CONNECT_TIMEOUT_SECONDS must be between 5 and 120"
fi

LOCAL_MOUNT=$(realpath -m -- "${LOCAL_MOUNT}")
TMP_ROOT=$(realpath -m -- "${TMP_ROOT}")
LOG_DIR=$(realpath -m -- "${LOG_DIR}")
REPORT_FILE=$(realpath -m -- "${REPORT_FILE}")

for path_value in "${LOCAL_MOUNT}" "${TMP_ROOT}" "${LOG_DIR}" "${REPORT_FILE}" "${DEST_RAID_MOUNT}" "${DEST_PATH}"; do
  [[ "${path_value}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die_early "unsafe path: ${path_value}"
done
for remote_path in "${DEST_RAID_MOUNT}" "${DEST_PATH}"; do
  case "${remote_path}" in
    /|*/|*//*|*/./*|*/.|*/../*|*/..) die_early "remote path is not canonical: ${remote_path}" ;;
  esac
done
for local_path in "${TMP_ROOT}" "${LOG_DIR}" "${REPORT_FILE}"; do
  [[ "${local_path}" == "${LOCAL_MOUNT}/"* ]] || \
    die_early "local working/report paths must stay below ${LOCAL_MOUNT}: ${local_path}"
done
[[ "${DEST_PATH}" == "${DEST_RAID_MOUNT}/"* && "${DEST_PATH}" != "${DEST_RAID_MOUNT}" ]] || \
  die_early "DEST_PATH must be a child of DEST_RAID_MOUNT"
[[ "${DEST_HOST}" =~ ^[A-Za-z0-9._-]+$ ]] || die_early "unsafe DEST_HOST"
[[ "${SOURCE_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die_early "invalid source Tailscale IPv4"
[[ "${DEST_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die_early "invalid destination Tailscale IPv4"

mountpoint --quiet -- "${LOCAL_MOUNT}" || die_early "local data mount is absent: ${LOCAL_MOUNT}"
[[ "$(findmnt -n -o TARGET --target "${LOCAL_MOUNT}")" == "${LOCAL_MOUNT}" ]] || \
  die_early "LOCAL_MOUNT is not the live mountpoint"
install -d -m 0700 -- "${TMP_ROOT}" "${LOG_DIR}" "$(dirname -- "${REPORT_FILE}")"
LOG="${LOG_DIR}/restore-drill.log"

log() {
  printf '%s %s\n' "$(date -Iseconds)" "$*" | tee -a "${LOG}"
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

ssh_remote_command() {
  local remote_host=$1
  shift
  local remote_command
  printf -v remote_command '%q ' "$@"
  ssh_transport "${remote_host}" "${remote_command% }"
}

resolved_ssh_host=$(ssh -G -o AddressFamily=inet "${DEST_HOST}" 2>/dev/null \
  | awk '$1 == "hostname" {print $2; exit}')
[[ "${resolved_ssh_host}" == "${DEST_TAILSCALE_IPV4}" ]] || \
  die_early "${DEST_HOST} resolves to ${resolved_ssh_host:-nothing}, not ${DEST_TAILSCALE_IPV4}"
tailscale ip -4 | grep -Fxq -- "${SOURCE_TAILSCALE_IPV4}" || \
  die_early "this host is not the expected Amarillo Tailnet node"

remote_inventory() {
  ssh_transport "${DEST_HOST}" bash -s -- \
    "${SOURCE_TAILSCALE_IPV4}" "${DEST_TAILSCALE_IPV4}" \
    "${DEST_RAID_MOUNT}" "${DEST_PATH}" "${DEST_MARKER}" <<'REMOTE'
set -Eeuo pipefail
expected_client_ip=$1
expected_server_ip=$2
raid_mount=$3
dest_path=$4
marker_name=$5
read -r client_ip _ server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${client_ip}" == "${expected_client_ip}" && "${server_ip}" == "${expected_server_ip}" ]] || {
  echo "refusing non-Tailscale SSH path" >&2
  exit 1
}
mountpoint --quiet -- "${raid_mount}"
[[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]]
[[ "$(realpath -m -- "${dest_path}")" == "${dest_path}" ]]
[[ -f "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]]
[[ "$(<"${dest_path}/${marker_name}")" == 'klukai-offsite-backups-v1' ]]
selected=
while IFS=$'\t' read -r archive_mtime archive_path; do
  if [[ -z "${selected}" && -f "${archive_path}.sha256" ]]; then
    selected="${archive_mtime}"$'\t'"${archive_path}"
  fi
done < <(find "${dest_path}" -maxdepth 1 -type f -name 'backups-*.tar.gz' \
  -printf '%T@\t%p\n' | LC_ALL=C sort -nr)
printf '%s\n' "${selected}"
REMOTE
}

latest_record=$(remote_inventory) || die_early "remote Tailnet/RAID/backup preflight failed"
if [[ -z "${latest_record}" ]]; then
  TAR_AGE_HOURS=null
  log "FAIL: no backup archive found at ${DEST_HOST}:${DEST_PATH}"
  exit 2
fi
tar_mtime=${latest_record%%$'\t'*}
LATEST_TAR=${latest_record#*$'\t'}
tar_mtime=${tar_mtime%%.*}
tar_basename=$(basename -- "${LATEST_TAR}")
[[ "${tar_basename}" =~ ^backups-[0-9]{8}-[0-9]{6}\.tar\.gz$ ]] || \
  die_early "remote inventory returned an unsafe archive name: ${tar_basename}"
[[ "${LATEST_TAR}" == "${DEST_PATH}/${tar_basename}" ]] || \
  die_early "remote inventory escaped the backup directory"

now=$(date +%s)
(( tar_mtime <= now )) || die_early "latest backup has a future timestamp"
TAR_AGE_HOURS=$(( (now - tar_mtime) / 3600 ))
if (( TAR_AGE_HOURS > RETENTION_DAYS_MAX * 24 )); then
  log "FAIL: newest backup is ${TAR_AGE_HOURS}h old (maximum ${RETENTION_DAYS_MAX}d)"
  exit 2
fi
log "selected ${tar_basename}; age ${TAR_AGE_HOURS}h"

WORK_DIR=$(mktemp -d -p "${TMP_ROOT}" restore-drill.XXXXXX)
WORK_MARKER="${WORK_DIR}/.klukai-restore-work-v1"
printf '%s\n' 'klukai-restore-work-v1' >"${WORK_MARKER}"
SCRATCH_NAME="klukai-restore-${BASHPID}-${RANDOM}"
SCRATCH_CREATED=false

cleanup() {
  if [[ "${KEEP_ARTIFACTS}" == true ]]; then
    log "keeping scratch artifacts: ${SCRATCH_NAME}, ${WORK_DIR}"
    return
  fi
  if [[ "${SCRATCH_CREATED}" == true ]]; then
    docker rm -f -- "${SCRATCH_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${WORK_DIR:-}" && "${WORK_DIR}" == "${TMP_ROOT}/restore-drill."* && \
        -f "${WORK_MARKER:-/nonexistent}" && "$(<"${WORK_MARKER}")" == 'klukai-restore-work-v1' ]]; then
    rm -rf --one-file-system -- "${WORK_DIR}"
  fi
}
trap cleanup EXIT

emit_report() {
  local report_status=$1
  local report_message=${2:-}
  local report_tmp="${REPORT_FILE}.partial.$$"
  jq -n \
    --arg ran_at "$(date -Iseconds)" \
    --arg status "${report_status}" \
    --arg message "${report_message}" \
    --arg backup "${tar_basename}" \
    --argjson tar_age_hours "${TAR_AGE_HOURS:-null}" \
    --argjson tables_restored "${TABLES_COUNT:-null}" \
    --argjson rows_messages "${ROWS_MESSAGES:-null}" \
    --argjson rows_memories "${ROWS_MEMORIES:-null}" \
    --argjson rows_audit "${ROWS_AUDIT:-null}" \
    '{ran_at:$ran_at,status:$status,message:$message,backup_tar:$backup,
      tar_age_hours:$tar_age_hours,tables_restored:$tables_restored,
      rows_messages:$rows_messages,rows_memories:$rows_memories,rows_audit:$rows_audit}' \
    >"${report_tmp}"
  chmod 0600 "${report_tmp}"
  mv -- "${report_tmp}" "${REPORT_FILE}"
}

fail() {
  local message=$1
  log "FAIL: ${message}"
  emit_report fail "${message}"
  exit 1
}

fetch_remote_file() {
  local remote_path=$1
  local local_path=$2
  local partial_path="${local_path}.partial"
  # The single-quoted program is evaluated by remote Bash, not locally.
  # shellcheck disable=SC2016
  ssh_remote_command "${DEST_HOST}" bash -c '
      set -Eeuo pipefail
      expected_client_ip=$1
      expected_server_ip=$2
      raid_mount=$3
      dest_path=$4
      remote_path=$5
      marker_name=$6
      read -r client_ip _ server_ip _ <<<"${SSH_CONNECTION:-}"
      [[ "${client_ip}" == "${expected_client_ip}" && "${server_ip}" == "${expected_server_ip}" ]]
      mountpoint --quiet -- "${raid_mount}"
      [[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]]
      [[ "$(realpath -m -- "${dest_path}")" == "${dest_path}" ]]
      [[ "$(realpath -m -- "${remote_path}")" == "${remote_path}" ]]
      [[ -f "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]]
      [[ "$(<"${dest_path}/${marker_name}")" == "klukai-offsite-backups-v1" ]]
      [[ "${remote_path}" == "${dest_path}/backups-"* ]]
      [[ -f "${remote_path}" && ! -L "${remote_path}" ]]
      cat -- "${remote_path}"
    ' bash "${SOURCE_TAILSCALE_IPV4}" "${DEST_TAILSCALE_IPV4}" \
      "${DEST_RAID_MOUNT}" "${DEST_PATH}" "${remote_path}" "${DEST_MARKER}" >"${partial_path}" \
    || fail "could not read ${remote_path} through the validated Tailnet path"
  mv -- "${partial_path}" "${local_path}"
}

TAR_LOCAL="${WORK_DIR}/${tar_basename}"
CHECKSUM_LOCAL="${TAR_LOCAL}.sha256"
log "copying archive and checksum into guarded scratch space"
fetch_remote_file "${LATEST_TAR}" "${TAR_LOCAL}"
fetch_remote_file "${LATEST_TAR}.sha256" "${CHECKSUM_LOCAL}"

read -r expected_hash checksum_name extra <"${CHECKSUM_LOCAL}" || fail "invalid checksum sidecar"
[[ -z "${extra:-}" && "${expected_hash}" =~ ^[0-9a-f]{64}$ && "${checksum_name}" == "${tar_basename}" ]] || \
  fail "checksum sidecar is malformed or names another file"
actual_hash=$(sha256sum -- "${TAR_LOCAL}" | awk '{print $1}')
[[ "${actual_hash}" == "${expected_hash}" ]] || fail "archive SHA-256 mismatch"
tar -tzf "${TAR_LOCAL}" >/dev/null || fail "archive failed gzip/tar validation"
if tar -tvzf "${TAR_LOCAL}" | awk '
    $0 ~ / \.\/klukai\// &&
      substr($1, 1, 1) != "-" && substr($1, 1, 1) != "d" { unsafe = 1 }
    END { exit unsafe ? 0 : 1 }
  '; then
  fail "Klukai recovery payload contains a link or other non-regular member"
fi
while IFS= read -r archive_member; do
  case "${archive_member}" in
    /*|..|../*|*/../*|*/..) fail "archive contains unsafe path: ${archive_member}" ;;
  esac
done < <(tar -tzf "${TAR_LOCAL}")

EXTRACT_DIR="${WORK_DIR}/extracted"
install -d -m 0700 -- "${EXTRACT_DIR}"
tar -tzf "${TAR_LOCAL}" | grep -Fx -- './klukai/' >/dev/null || \
  fail "archive lacks required recovery directory: ./klukai/"
tar --no-same-owner --no-same-permissions -xzf "${TAR_LOCAL}" -C "${EXTRACT_DIR}" \
  -- './klukai/' || fail "recovery payload extraction failed"
DUMP=$(find "${EXTRACT_DIR}" -type f -name 'klukai-db-*.sql.gz' \
  -print | LC_ALL=C sort | tail -n 1)
[[ -n "${DUMP}" ]] || fail "no Klukai PostgreSQL dump found"
gzip -t -- "${DUMP}" || fail "PostgreSQL dump failed gzip validation"

log "starting unique scratch PostgreSQL container ${SCRATCH_NAME}"
docker run -d --rm \
  --name "${SCRATCH_NAME}" \
  -e POSTGRES_PASSWORD=scratch \
  -e POSTGRES_USER=aichat \
  -e POSTGRES_DB=aichat \
  "${PG_IMAGE}" >/dev/null || fail "scratch PostgreSQL failed to start"
SCRATCH_CREATED=true

for attempt in {1..30}; do
  if docker exec "${SCRATCH_NAME}" pg_isready -U aichat >/dev/null 2>&1; then
    log "scratch PostgreSQL ready after ${attempt}s"
    break
  fi
  (( attempt < 30 )) || fail "scratch PostgreSQL never became ready"
  sleep 1
done

log "restoring dump into scratch PostgreSQL only"
gzip -cd -- "${DUMP}" \
  | docker exec -i "${SCRATCH_NAME}" psql --single-transaction -U aichat -d aichat -v ON_ERROR_STOP=1 -q \
  >>"${LOG}" 2>&1 || fail "scratch restore failed"

REQUIRED_TABLES=(
  companion_messages
  companion_memories
  companion_users
  companion_audit_log
  companion_affection
)
TABLES_COUNT=0
for table_name in "${REQUIRED_TABLES[@]}"; do
  docker exec "${SCRATCH_NAME}" psql -U aichat -d aichat -tAc \
    "SELECT 1 FROM information_schema.tables WHERE table_name='${table_name}'" \
    | grep -q 1 || fail "required table missing after restore: ${table_name}"
  ((TABLES_COUNT += 1))
done

ROWS_MESSAGES=$(docker exec "${SCRATCH_NAME}" psql -U aichat -d aichat -tAc \
  'SELECT count(*) FROM companion_messages' | tr -d '[:space:]')
ROWS_MEMORIES=$(docker exec "${SCRATCH_NAME}" psql -U aichat -d aichat -tAc \
  'SELECT count(*) FROM companion_memories' | tr -d '[:space:]')
ROWS_AUDIT=$(docker exec "${SCRATCH_NAME}" psql -U aichat -d aichat -tAc \
  'SELECT count(*) FROM companion_audit_log' | tr -d '[:space:]')
for count_value in "${ROWS_MESSAGES}" "${ROWS_MEMORIES}" "${ROWS_AUDIT}"; do
  [[ "${count_value}" =~ ^[0-9]+$ ]] || fail "scratch restore returned a non-numeric row count"
done
(( ROWS_MESSAGES > 0 )) || fail "scratch restore contains zero companion_messages"

log "restore drill passed: ${TABLES_COUNT} tables, ${ROWS_MESSAGES} messages, ${ROWS_MEMORIES} memories"
emit_report pass "verified archive checksum and scratch-only PostgreSQL restore"
