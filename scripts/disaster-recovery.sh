#!/usr/bin/env bash
# Stage and verify an Amarillo backup from dominus-nobara, then optionally run
# the explicitly confirmed production recovery. The default mode is read-only.
#
# Usage:
#   scripts/disaster-recovery.sh [--dry-run] [--backup-date YYYY-MM-DD]
#   DR_CONFIRM_RESTORE=restore-klukai-from-verified-backup \
#   DR_CONFIRM_SHARED_AICHAT=yes scripts/disaster-recovery.sh --execute

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
cd "${REPO_ROOT}"

MODE=dry-run
BACKUP_DATE=
while (( $# > 0 )); do
  case "$1" in
    --dry-run) MODE=dry-run; shift ;;
    --execute) MODE=execute; shift ;;
    --backup-date)
      (( $# >= 2 )) || { echo "disaster-recovery: --backup-date requires a value" >&2; exit 2; }
      BACKUP_DATE=$2
      shift 2
      ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "disaster-recovery: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "${BACKUP_DATE}" ]]; then
  [[ "${BACKUP_DATE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || {
    echo "disaster-recovery: backup date must be YYYY-MM-DD" >&2
    exit 2
  }
  BACKUP_PATTERN="backups-${BACKUP_DATE//-/}-*.tar.gz"
else
  BACKUP_PATTERN='backups-*.tar.gz'
fi

LOCAL_MOUNT="${LOCAL_MOUNT:-/mnt/nvmeINT}"
RESTORE_ROOT="${RESTORE_ROOT:-/mnt/nvmeINT/restore}"
LOG_ROOT="${LOG_ROOT:-/mnt/nvmeINT/logs/dr-drill}"
DEST_HOST="${DEST_HOST:-dominus-nobara}"
DEST_TAILSCALE_IPV4="${DEST_TAILSCALE_IPV4:-100.107.121.5}"
SOURCE_TAILSCALE_IPV4="${SOURCE_TAILSCALE_IPV4:-100.111.198.19}"
DEST_RAID_MOUNT="${DEST_RAID_MOUNT:-/mnt/nvmer0}"
DEST_PATH="${DEST_PATH:-/mnt/nvmer0/services/ai-stack/backups/amarillo/klukai}"
DEST_MARKER=".klukai-offsite-backups-v1"
SSH_CONNECT_TIMEOUT_SECONDS="${SSH_CONNECT_TIMEOUT_SECONDS:-15}"
QDRANT_RESTORE_URL="${QDRANT_RESTORE_URL:-http://127.0.0.1:6333}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-infra-postgres}"
IMAGES_VOLUME="${IMAGES_VOLUME:-companion_companion-images}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8300/health}"

early_fatal() {
  echo "disaster-recovery: ERROR: $*" >&2
  exit 1
}

for command_name in awk basename curl date docker find findmnt grep gzip install jq mountpoint mv realpath sed sha256sum sleep sort ssh tail tailscale tar tee; do
  command -v "${command_name}" >/dev/null || early_fatal "missing required command: ${command_name}"
done
if ! [[ "${SSH_CONNECT_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || \
   (( SSH_CONNECT_TIMEOUT_SECONDS < 5 || SSH_CONNECT_TIMEOUT_SECONDS > 120 )); then
  early_fatal "SSH_CONNECT_TIMEOUT_SECONDS must be between 5 and 120"
fi

LOCAL_MOUNT=$(realpath -m -- "${LOCAL_MOUNT}")
RESTORE_ROOT=$(realpath -m -- "${RESTORE_ROOT}")
LOG_ROOT=$(realpath -m -- "${LOG_ROOT}")
for path_value in "${LOCAL_MOUNT}" "${RESTORE_ROOT}" "${LOG_ROOT}" "${DEST_RAID_MOUNT}" "${DEST_PATH}"; do
  [[ "${path_value}" =~ ^/[A-Za-z0-9._/-]+$ ]] || early_fatal "unsafe path: ${path_value}"
done
for remote_path in "${DEST_RAID_MOUNT}" "${DEST_PATH}"; do
  case "${remote_path}" in
    /|*/|*//*|*/./*|*/.|*/../*|*/..) early_fatal "remote path is not canonical: ${remote_path}" ;;
  esac
done
[[ "${RESTORE_ROOT}" == "${LOCAL_MOUNT}/"* ]] || early_fatal "RESTORE_ROOT must stay below LOCAL_MOUNT"
[[ "${LOG_ROOT}" == "${LOCAL_MOUNT}/"* ]] || early_fatal "LOG_ROOT must stay below LOCAL_MOUNT"
[[ "${DEST_PATH}" == "${DEST_RAID_MOUNT}/"* && "${DEST_PATH}" != "${DEST_RAID_MOUNT}" ]] || \
  early_fatal "DEST_PATH must be a child of DEST_RAID_MOUNT"
[[ "${DEST_HOST}" =~ ^[A-Za-z0-9._-]+$ ]] || early_fatal "unsafe DEST_HOST"
[[ "${SOURCE_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || early_fatal "invalid source Tailscale IPv4"
[[ "${DEST_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || early_fatal "invalid destination Tailscale IPv4"

mountpoint --quiet -- "${LOCAL_MOUNT}" || early_fatal "local data mount is absent: ${LOCAL_MOUNT}"
[[ "$(findmnt -n -o TARGET --target "${LOCAL_MOUNT}")" == "${LOCAL_MOUNT}" ]] || \
  early_fatal "LOCAL_MOUNT is not the live mountpoint"
install -d -m 0700 -- "${LOG_ROOT}"
STAMP=$(date -u +%Y-%m-%d-%H%M%S)
DRILL_LOG="${LOG_ROOT}/${STAMP}.log"
DRILL_JSON="${LOG_ROOT}/${STAMP}.json"
exec > >(tee -a "${DRILL_LOG}") 2>&1
START_TS=$(date -u +%s)

step() { echo "[DR] -- $*"; }
fatal() { echo "[DR] FATAL: $*" >&2; exit 1; }

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
  fatal "${DEST_HOST} resolves to ${resolved_ssh_host:-nothing}, not ${DEST_TAILSCALE_IPV4}"
tailscale ip -4 | grep -Fxq -- "${SOURCE_TAILSCALE_IPV4}" || \
  fatal "this host is not the expected Amarillo Tailnet node"

LATEST_RECORD=$(ssh_transport "${DEST_HOST}" bash -s -- \
  "${SOURCE_TAILSCALE_IPV4}" "${DEST_TAILSCALE_IPV4}" \
  "${DEST_RAID_MOUNT}" "${DEST_PATH}" "${DEST_MARKER}" \
  "${BACKUP_PATTERN}" <<'REMOTE'
set -Eeuo pipefail
expected_client_ip=$1
expected_server_ip=$2
raid_mount=$3
dest_path=$4
marker_name=$5
backup_pattern=$6
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
[[ "${backup_pattern}" =~ ^backups-[0-9*?_-]+\.tar\.gz$ ]]
selected=
while IFS=$'\t' read -r archive_mtime archive_path; do
  if [[ -z "${selected}" && -f "${archive_path}.sha256" ]]; then
    selected="${archive_mtime}"$'\t'"${archive_path}"
  fi
done < <(find "${dest_path}" -maxdepth 1 -type f -name "${backup_pattern}" \
  -printf '%T@\t%p\n' | LC_ALL=C sort -nr)
printf '%s\n' "${selected}"
REMOTE
) || fatal "remote Tailnet/RAID/backup preflight failed"

[[ -n "${LATEST_RECORD}" ]] || fatal "no backup matches ${BACKUP_PATTERN} at ${DEST_HOST}:${DEST_PATH}"
LATEST_TAR=${LATEST_RECORD#*$'\t'}
TAR_BASENAME=$(basename -- "${LATEST_TAR}")
[[ "${TAR_BASENAME}" =~ ^backups-[0-9]{8}-[0-9]{6}\.tar\.gz$ ]] || fatal "unsafe archive name"
[[ "${LATEST_TAR}" == "${DEST_PATH}/${TAR_BASENAME}" ]] || fatal "archive path escaped destination"
step "selected verified-format candidate ${DEST_HOST}:${LATEST_TAR}"

if [[ "${MODE}" == dry-run ]]; then
  echo "[DR] DRY RUN: no archive copied and no service or data modified"
  echo "[DR] would verify ${TAR_BASENAME} plus its SHA-256 sidecar"
  echo "[DR] would require PostgreSQL, Qdrant, and companion image payloads before stopping companion-core"
  echo "[DR] would stop/start only the amarillo companion-core Compose service"
  echo "[DR] execute requires both DR_CONFIRM_RESTORE and DR_CONFIRM_SHARED_AICHAT gates"
  jq -n \
    --arg drill_date "${STAMP}" \
    --arg backup_tar "${TAR_BASENAME}" \
    '{drill_date:$drill_date,mode:"dry-run",backup_tar:$backup_tar,
      duration_seconds:0,rto_achieved:null,outcome:"validated-plan-only"}' \
    >"${DRILL_JSON}"
  exit 0
fi

[[ "${DR_CONFIRM_RESTORE:-}" == 'restore-klukai-from-verified-backup' ]] || \
  fatal "set DR_CONFIRM_RESTORE=restore-klukai-from-verified-backup for live recovery"
[[ "${DR_CONFIRM_SHARED_AICHAT:-}" == yes ]] || \
  fatal "set DR_CONFIRM_SHARED_AICHAT=yes after confirming the dump contains only companion_* objects"

install -d -m 0700 -- "${RESTORE_ROOT}"
WORK_DIR="${RESTORE_ROOT}/dr-${STAMP}"
[[ ! -e "${WORK_DIR}" ]] || fatal "restore work directory already exists: ${WORK_DIR}"
install -d -m 0700 -- "${WORK_DIR}"
printf '%s\n' 'klukai-dr-work-v1' >"${WORK_DIR}/.klukai-dr-work-v1"

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
    || fatal "failed to copy ${remote_path} through the validated Tailnet path"
  mv -- "${partial_path}" "${local_path}"
}

TAR_LOCAL="${WORK_DIR}/${TAR_BASENAME}"
CHECKSUM_LOCAL="${TAR_LOCAL}.sha256"
step "copy archive and checksum to mount-guarded staging"
fetch_remote_file "${LATEST_TAR}" "${TAR_LOCAL}"
fetch_remote_file "${LATEST_TAR}.sha256" "${CHECKSUM_LOCAL}"
read -r EXPECTED_HASH CHECKSUM_NAME EXTRA <"${CHECKSUM_LOCAL}" || fatal "invalid checksum sidecar"
[[ -z "${EXTRA:-}" && "${EXPECTED_HASH}" =~ ^[0-9a-f]{64}$ && "${CHECKSUM_NAME}" == "${TAR_BASENAME}" ]] || \
  fatal "checksum sidecar is malformed"
[[ "$(sha256sum -- "${TAR_LOCAL}" | awk '{print $1}')" == "${EXPECTED_HASH}" ]] || fatal "archive SHA-256 mismatch"
tar -tzf "${TAR_LOCAL}" >/dev/null || fatal "archive failed gzip/tar validation"
if tar -tvzf "${TAR_LOCAL}" | awk '
    $0 ~ / \.\/(klukai|qdrant)\// &&
      substr($1, 1, 1) != "-" && substr($1, 1, 1) != "d" { unsafe = 1 }
    END { exit unsafe ? 0 : 1 }
  '; then
  fatal "Klukai recovery payload contains a link or other non-regular member"
fi
while IFS= read -r ARCHIVE_MEMBER; do
  case "${ARCHIVE_MEMBER}" in
    /*|..|../*|*/../*|*/..) fatal "archive contains unsafe path: ${ARCHIVE_MEMBER}" ;;
  esac
done < <(tar -tzf "${TAR_LOCAL}")

EXTRACT_DIR="${WORK_DIR}/extracted"
install -d -m 0700 -- "${EXTRACT_DIR}"
for REQUIRED_ROOT in './klukai/' './qdrant/'; do
  tar -tzf "${TAR_LOCAL}" | grep -Fx -- "${REQUIRED_ROOT}" >/dev/null || \
    fatal "archive lacks required recovery directory: ${REQUIRED_ROOT}"
done
tar --no-same-owner --no-same-permissions -xzf "${TAR_LOCAL}" -C "${EXTRACT_DIR}" \
  -- './klukai/' './qdrant/' || fatal "recovery payload extraction failed"
PG_DUMP=$(find "${EXTRACT_DIR}" -type f -name 'klukai-db-*.sql.gz' -print | LC_ALL=C sort | tail -n 1)
[[ -n "${PG_DUMP}" ]] || fatal "no klukai-db-*.sql.gz in archive"
gzip -t -- "${PG_DUMP}" || fatal "PostgreSQL dump failed gzip validation"
QDRANT_SNAP_DIR="${EXTRACT_DIR}/qdrant"
compgen -G "${QDRANT_SNAP_DIR}/*.snapshot" >/dev/null || fatal "archive lacks required Qdrant snapshots"
IMAGES_SOURCE="${EXTRACT_DIR}/klukai/images-latest"
[[ -d "${IMAGES_SOURCE}" ]] || fatal "archive lacks required companion images"

# Parse only SQL statements outside COPY data. The accepted grammar matches the
# scoped plain pg_dump produced for Klukai: settings, companion_* tables and
# sequences, indexes on those tables, COPY data, and sequence setval calls.
# Anything broader fails closed before production is stopped.
if ! SQL_SCOPE_ERRORS=$(gzip -cd -- "${PG_DUMP}" | awk '
  BEGIN { in_copy = 0; unsafe = 0 }
  in_copy {
    if ($0 == "\\.") in_copy = 0
    next
  }
  /^COPY / { in_copy = 1 }
  /^\\/ {
    if ($0 !~ /^\\(un)?restrict [A-Za-z0-9]+$/) {
      print "unsupported psql command: " $0
      unsafe = 1
    }
    next
  }
  /^[A-Z]/ {
    if ($0 !~ /^(SET |SELECT pg_catalog\.set(_config|val)\(|CREATE (TABLE|SEQUENCE|INDEX|UNIQUE INDEX) |ALTER (TABLE|SEQUENCE)( ONLY)? |COPY )/) {
      print "unsupported SQL statement: " $0
      unsafe = 1
    }
  }
  {
    remainder = $0
    while (match(remainder, /public\.[A-Za-z_][A-Za-z0-9_$]*/)) {
      reference = substr(remainder, RSTART, RLENGTH)
      if (reference !~ /^public\.companion_[A-Za-z0-9_$]+$/) {
        print "non-companion public object: " reference
        unsafe = 1
      }
      remainder = substr(remainder, RSTART + RLENGTH)
    }
    if ($0 ~ /public\."/) {
      print "quoted public identifier is outside the accepted dump grammar"
      unsafe = 1
    }
  }
  END {
    if (in_copy) {
      print "unterminated COPY data"
      unsafe = 1
    }
    exit unsafe ? 1 : 0
  }
'); then
  fatal "PostgreSQL dump failed the companion-only scope check: ${SQL_SCOPE_ERRORS}"
fi

docker ps --format '{{.Names}}' | grep -Fxq -- "${POSTGRES_CONTAINER}" || fatal "shared PostgreSQL container is not running"
docker volume inspect "${IMAGES_VOLUME}" >/dev/null || fatal "companion images volume is absent"
docker compose config --services | grep -Fxq companion-core || fatal "repository Compose does not define companion-core"
docker compose ps --status running --services | grep -Fxq companion-core || fatal "companion-core is not currently running"

DROP_TABLES_SQL=$(docker exec "${POSTGRES_CONTAINER}" psql -U aichat -d aichat \
  -v ON_ERROR_STOP=1 -Atc \
  "SELECT COALESCE(
     'DROP TABLE IF EXISTS ' ||
       string_agg(format('%I.%I', schemaname, tablename), ', ' ORDER BY tablename) || ';',
     '-- no existing companion tables'
   )
   FROM pg_tables
   WHERE schemaname = 'public' AND tablename ~ '^companion_[A-Za-z0-9_]+$';") || \
  fatal "could not inventory existing companion tables"
DROP_SEQUENCES_SQL=$(docker exec "${POSTGRES_CONTAINER}" psql -U aichat -d aichat \
  -v ON_ERROR_STOP=1 -Atc \
  "SELECT COALESCE(
     'DROP SEQUENCE IF EXISTS ' ||
       string_agg(format('%I.%I', sequence_schema, sequence_name), ', ' ORDER BY sequence_name) || ';',
     '-- no existing companion sequences'
   )
   FROM information_schema.sequences
   WHERE sequence_schema = 'public' AND sequence_name ~ '^companion_[A-Za-z0-9_]+$';") || \
  fatal "could not inventory existing companion sequences"
[[ "${DROP_TABLES_SQL}" == '-- no existing companion tables' || \
   "${DROP_TABLES_SQL}" == 'DROP TABLE IF EXISTS public.companion_'* ]] || \
  fatal "unsafe generated table replacement statement"
[[ "${DROP_SEQUENCES_SQL}" == '-- no existing companion sequences' || \
   "${DROP_SEQUENCES_SQL}" == 'DROP SEQUENCE IF EXISTS public.companion_'* ]] || \
  fatal "unsafe generated sequence replacement statement"

CORE_STOPPED=false
restore_core_on_exit() {
  if [[ "${CORE_STOPPED}" == true ]]; then
    echo "[DR] recovery trap: starting companion-core"
    docker compose up -d --no-build companion-core >/dev/null 2>&1 || true
  fi
}
trap restore_core_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

step "stop only companion-core; shared data services remain under their existing owners"
CORE_STOPPED=true
docker compose stop --timeout 30 companion-core

step "atomically replace companion_* PostgreSQL objects with ON_ERROR_STOP"
{
  printf '%s\n%s\n' "${DROP_TABLES_SQL}" "${DROP_SEQUENCES_SQL}"
  gzip -cd -- "${PG_DUMP}"
} \
  | docker exec -i "${POSTGRES_CONTAINER}" psql --single-transaction -U aichat -d aichat -v ON_ERROR_STOP=1 \
  || fatal "PostgreSQL restore failed"

step "restore required Qdrant collection snapshots"
for SNAPSHOT in "${QDRANT_SNAP_DIR}"/*.snapshot; do
  [[ -f "${SNAPSHOT}" ]] || continue
  COLLECTION=$(basename -- "${SNAPSHOT}" .snapshot)
  [[ "${COLLECTION}" =~ ^companion_[A-Za-z0-9_]+$ ]] || fatal "unsafe Qdrant collection name: ${COLLECTION}"
  curl --fail --silent --show-error \
    -X POST "${QDRANT_RESTORE_URL}/collections/${COLLECTION}/snapshots/upload" \
    -H 'Content-Type: multipart/form-data' \
    -F "snapshot=@${SNAPSHOT}" >/dev/null || fatal "Qdrant restore failed for ${COLLECTION}"
done

step "restore companion images from read-only staged source"
docker run --rm \
  -v "${IMAGES_VOLUME}:/dst" \
  -v "${IMAGES_SOURCE}:/src:ro" \
  alpine sh -c 'cp -a /src/. /dst/' || fatal "companion image restore failed"

step "start companion-core and run smoke test"
docker compose up -d --no-build companion-core
CORE_STOPPED=false
RECOVERED=false
for ATTEMPT in {1..60}; do
  STATUS=$(curl --silent --max-time 3 "${HEALTH_URL}" 2>/dev/null | jq -r '.status // empty' 2>/dev/null || true)
  if [[ "${STATUS}" == ok ]]; then
    RECOVERED=true
    echo "[DR] health recovered after ${ATTEMPT}s"
    break
  fi
  sleep 1
done
[[ "${RECOVERED}" == true ]] || fatal "companion-core health did not recover within 60 seconds"

END_TS=$(date -u +%s)
DURATION=$((END_TS - START_TS))
RTO_TARGET=1800
RTO_OK=false
(( DURATION < RTO_TARGET )) && RTO_OK=true
jq -n \
  --arg drill_date "${STAMP}" \
  --arg backup_tar "${TAR_BASENAME}" \
  --argjson duration_seconds "${DURATION}" \
  --argjson rto_target_seconds "${RTO_TARGET}" \
  --argjson rto_achieved "${RTO_OK}" \
  '{drill_date:$drill_date,mode:"live",backup_tar:$backup_tar,
    duration_seconds:$duration_seconds,rto_target_seconds:$rto_target_seconds,
    rto_achieved:$rto_achieved,outcome:(if $rto_achieved then "passed" else "missed-rto" end)}' \
  >"${DRILL_JSON}"
echo "[DR] complete: ${DURATION}s; RTO achieved=${RTO_OK}; report=${DRILL_JSON}"
