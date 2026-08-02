#!/usr/bin/env bash
# Copy Amarillo's verified local backup set to dominus-nobara over Tailscale.
#
# The destination is off-host for Amarillo, but it is on dominus-nobara's
# RAID 0 and is not an independent backup of any data originating there.
# This script never prunes or overwrites completed archives.
#
# Exit codes: 0 success, 2 no sufficiently recent DB dump, 1 fatal.

set -Eeuo pipefail

SOURCE_MOUNT="${SOURCE_MOUNT:-/mnt/nvmeINT}"
SRC="${SRC:-/mnt/nvmeINT/backups}"
LOG="${LOG:-/mnt/nvmeINT/logs/offsite-backup.log}"
DEST_HOST="${DEST_HOST:-dominus-nobara}"
DEST_TAILSCALE_IPV4="${DEST_TAILSCALE_IPV4:-100.107.121.5}"
SOURCE_TAILSCALE_IPV4="${SOURCE_TAILSCALE_IPV4:-100.111.198.19}"
DEST_RAID_MOUNT="${DEST_RAID_MOUNT:-/mnt/nvmer0}"
DEST_PATH="${DEST_PATH:-/mnt/nvmer0/services/ai-stack/backups/amarillo/klukai}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
MAX_SOURCE_AGE_HOURS="${MAX_SOURCE_AGE_HOURS:-30}"
SSH_CONNECT_TIMEOUT_SECONDS="${SSH_CONNECT_TIMEOUT_SECONDS:-15}"
DEST_MARKER=".klukai-offsite-backups-v1"
ARCHIVE_ROOTS=(./klukai ./qdrant ./kairi)

die() {
  echo "offsite-backup: ERROR: $*" >&2
  exit 1
}

for command_name in awk chmod date dirname find findmnt grep gzip install mkdir mountpoint mv realpath sha256sum ssh stat tailscale tar tee wc; do
  command -v "${command_name}" >/dev/null || die "missing required command: ${command_name}"
done

for integer_name in RETENTION_DAYS MAX_SOURCE_AGE_HOURS SSH_CONNECT_TIMEOUT_SECONDS; do
  integer_value=${!integer_name}
  [[ "${integer_value}" =~ ^[0-9]+$ ]] || die "${integer_name} must be a non-negative integer"
done
(( RETENTION_DAYS >= 1 )) || die "RETENTION_DAYS must be at least 1"
(( MAX_SOURCE_AGE_HOURS >= 1 && MAX_SOURCE_AGE_HOURS <= 168 )) || \
  die "MAX_SOURCE_AGE_HOURS must be between 1 and 168"
(( SSH_CONNECT_TIMEOUT_SECONDS >= 5 && SSH_CONNECT_TIMEOUT_SECONDS <= 120 )) || \
  die "SSH_CONNECT_TIMEOUT_SECONDS must be between 5 and 120"

SOURCE_MOUNT=$(realpath -m -- "${SOURCE_MOUNT}")
SRC=$(realpath -m -- "${SRC}")
LOG=$(realpath -m -- "${LOG}")

for path_value in "${SOURCE_MOUNT}" "${SRC}" "${LOG}" "${DEST_RAID_MOUNT}" "${DEST_PATH}"; do
  [[ "${path_value}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "unsafe path: ${path_value}"
done
for remote_path in "${DEST_RAID_MOUNT}" "${DEST_PATH}"; do
  case "${remote_path}" in
    /|*/|*//*|*/./*|*/.|*/../*|*/..) die "remote path is not canonical: ${remote_path}" ;;
  esac
done
[[ "${SRC}" == "${SOURCE_MOUNT}/"* && "${SRC}" != "${SOURCE_MOUNT}" ]] || \
  die "SRC must be a child of SOURCE_MOUNT"
[[ "${LOG}" == "${SOURCE_MOUNT}/"* ]] || die "LOG must stay below SOURCE_MOUNT"
[[ "${DEST_PATH}" == "${DEST_RAID_MOUNT}/"* && "${DEST_PATH}" != "${DEST_RAID_MOUNT}" ]] || \
  die "DEST_PATH must be a child of DEST_RAID_MOUNT"
[[ "${DEST_HOST}" =~ ^[A-Za-z0-9._-]+$ ]] || die "unsafe DEST_HOST"
[[ "${SOURCE_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid source Tailscale IPv4"
[[ "${DEST_TAILSCALE_IPV4}" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid destination Tailscale IPv4"

mountpoint --quiet -- "${SOURCE_MOUNT}" || die "source mount is absent: ${SOURCE_MOUNT}"
[[ "$(findmnt -n -o TARGET --target "${SOURCE_MOUNT}")" == "${SOURCE_MOUNT}" ]] || \
  die "SOURCE_MOUNT is not the live mountpoint: ${SOURCE_MOUNT}"
[[ -d "${SRC}" ]] || die "backup source directory is absent: ${SRC}"
for archive_root in "${ARCHIVE_ROOTS[@]}"; do
  [[ -d "${SRC}/${archive_root#./}" && ! -L "${SRC}/${archive_root#./}" ]] || \
    die "required backup root is absent: ${SRC}/${archive_root#./}"
done
mkdir -p -- "$(dirname -- "${LOG}")"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${LOG}"
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

# OpenSSH joins command arguments into a remote shell command. Quote every
# argument explicitly so the remote Bash program and its parameters retain
# their boundaries.
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
  die "${DEST_HOST} resolves to ${resolved_ssh_host:-nothing}, not ${DEST_TAILSCALE_IPV4}"
tailscale ip -4 | grep -Fxq -- "${SOURCE_TAILSCALE_IPV4}" || \
  die "this host is not the expected Amarillo Tailnet node (${SOURCE_TAILSCALE_IPV4})"

remote_preflight() {
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
  echo "refusing non-Tailscale SSH path: ${client_ip:-unknown} -> ${server_ip:-unknown}" >&2
  exit 1
}
mountpoint --quiet -- "${raid_mount}" || {
  echo "destination RAID is not mounted: ${raid_mount}" >&2
  exit 1
}
[[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]] || {
  echo "destination is not the expected mountpoint: ${raid_mount}" >&2
  exit 1
}
[[ "$(realpath -m -- "${raid_mount}")" == "${raid_mount}" ]] || {
  echo "destination RAID path is not canonical: ${raid_mount}" >&2
  exit 1
}
canonical_dest=$(realpath -m -- "${dest_path}")
[[ "${canonical_dest}" == "${dest_path}" ]] || {
  echo "destination path is not canonical: ${dest_path}" >&2
  exit 1
}
[[ "${dest_path}" == "${raid_mount}/"* && "${dest_path}" != "${raid_mount}" ]] || {
  echo "unsafe destination below RAID: ${dest_path}" >&2
  exit 1
}

if [[ -d "${dest_path}" && ! -f "${dest_path}/${marker_name}" ]] && \
   [[ -n "$(find "${dest_path}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "refusing unmarked non-empty backup destination: ${dest_path}" >&2
  exit 1
fi
install -d -m 0700 -- "${dest_path}"
if [[ -f "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]]; then
  [[ "$(<"${dest_path}/${marker_name}")" == "klukai-offsite-backups-v1" ]] || {
    echo "backup destination marker has unexpected content" >&2
    exit 1
  }
else
  [[ ! -e "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]] || {
    echo "refusing unexpected backup destination marker type" >&2
    exit 1
  }
  printf '%s\n' 'klukai-offsite-backups-v1' >"${dest_path}/${marker_name}"
  chmod 0600 "${dest_path}/${marker_name}"
fi
REMOTE
}

recent_minutes=$((MAX_SOURCE_AGE_HOURS * 60))
recent_dumps=()
for database_name in klukai kairi; do
  mapfile -d '' database_dumps < <(
    find "${SRC}/${database_name}" -type f -name "${database_name}-db-*.sql.gz" \
      -mmin "-${recent_minutes}" -print0
  )
  if (( ${#database_dumps[@]} == 0 )); then
    log "WARN: no recent ${database_name} DB dump; refusing to ship an incomplete snapshot"
    exit 2
  fi
  recent_dumps+=("${database_dumps[@]}")
done
for dump_path in "${recent_dumps[@]}"; do
  gzip -t -- "${dump_path}" || die "DB dump failed gzip validation: ${dump_path}"
done
for collection_name in companion_episodes companion_exchanges; do
  snapshot_path="${SRC}/qdrant/${collection_name}.snapshot"
  [[ -f "${snapshot_path}" && ! -L "${snapshot_path}" && -s "${snapshot_path}" ]] || \
    die "required Qdrant snapshot is absent, linked, or empty: ${snapshot_path}"
  [[ -n "$(find "${snapshot_path}" -mmin "-${recent_minutes}" -print -quit)" ]] || {
    log "WARN: Qdrant snapshot is older than ${MAX_SOURCE_AGE_HOURS}h: ${snapshot_path}"
    exit 2
  }
done

remote_preflight || die "Tailscale or destination RAID preflight failed"

stamp=$(date -u +%Y%m%d-%H%M%S)
tar_name="backups-${stamp}.tar.gz"
remote_final="${DEST_PATH}/${tar_name}"
remote_checksum="${remote_final}.sha256"
remote_partial="${DEST_PATH}/.${tar_name}.partial.$$"

log "backup start: ${SRC} -> ${DEST_HOST}:${remote_final}"

# The single-quoted program is evaluated by remote Bash, not locally.
# shellcheck disable=SC2016
tar --one-file-system -C "${SRC}" -czf - -- "${ARCHIVE_ROOTS[@]}" 2>>"${LOG}" \
  | ssh_remote_command "${DEST_HOST}" bash -c '
      set -Eeuo pipefail
      expected_client_ip=$1
      expected_server_ip=$2
      raid_mount=$3
      dest_path=$4
      partial_path=$5
      marker_name=$6
      read -r client_ip _ server_ip _ <<<"${SSH_CONNECTION:-}"
      [[ "${client_ip}" == "${expected_client_ip}" && "${server_ip}" == "${expected_server_ip}" ]]
      mountpoint --quiet -- "${raid_mount}"
      [[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]]
      [[ "$(realpath -m -- "${dest_path}")" == "${dest_path}" ]]
      [[ "$(realpath -m -- "${partial_path}")" == "${partial_path}" ]]
      [[ -f "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]]
      [[ "$(<"${dest_path}/${marker_name}")" == "klukai-offsite-backups-v1" ]]
      [[ "${partial_path}" == "${dest_path}/."* ]]
      [[ ! -e "${partial_path}" ]]
      umask 077
      set -o noclobber
      cat >"${partial_path}"
    ' bash "${SOURCE_TAILSCALE_IPV4}" "${DEST_TAILSCALE_IPV4}" \
      "${DEST_RAID_MOUNT}" "${DEST_PATH}" "${remote_partial}" "${DEST_MARKER}" \
  || die "tar stream failed; any partial file was preserved for inspection"

remote_size=$(ssh_transport "${DEST_HOST}" bash -s -- \
  "${SOURCE_TAILSCALE_IPV4}" "${DEST_TAILSCALE_IPV4}" \
  "${DEST_RAID_MOUNT}" "${DEST_PATH}" "${remote_partial}" \
  "${remote_final}" "${remote_checksum}" "${tar_name}" "${DEST_MARKER}" <<'REMOTE'
set -Eeuo pipefail
expected_client_ip=$1
expected_server_ip=$2
raid_mount=$3
dest_path=$4
partial_path=$5
final_path=$6
checksum_path=$7
tar_name=$8
marker_name=$9
read -r client_ip _ server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${client_ip}" == "${expected_client_ip}" && "${server_ip}" == "${expected_server_ip}" ]]
mountpoint --quiet -- "${raid_mount}"
[[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]]
[[ "$(realpath -m -- "${dest_path}")" == "${dest_path}" ]]
[[ "$(realpath -m -- "${partial_path}")" == "${partial_path}" ]]
[[ "$(realpath -m -- "${final_path}")" == "${final_path}" ]]
[[ "$(realpath -m -- "${checksum_path}")" == "${checksum_path}" ]]
[[ -f "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]]
[[ "$(<"${dest_path}/${marker_name}")" == 'klukai-offsite-backups-v1' ]]
[[ "${partial_path}" == "${dest_path}/."* ]]
[[ "${final_path}" == "${dest_path}/backups-"*.tar.gz ]]
[[ "${checksum_path}" == "${final_path}.sha256" ]]
[[ -f "${partial_path}" && ! -e "${final_path}" && ! -e "${checksum_path}" ]]
tar -tzf "${partial_path}" >/dev/null
size=$(stat -c '%s' -- "${partial_path}")
(( size >= 100 ))
hash=$(sha256sum -- "${partial_path}" | awk '{print $1}')
checksum_partial="${checksum_path}.partial.$$"
umask 077
printf '%s  %s\n' "${hash}" "${tar_name}" >"${checksum_partial}"
mv -- "${checksum_partial}" "${checksum_path}"
mv -- "${partial_path}" "${final_path}"
printf '%s\n' "${size}"
REMOTE
) || die "remote archive validation/finalization failed; completed backups were not touched"

read -r snapshot_count old_count < <(
  ssh_transport "${DEST_HOST}" bash -s -- \
    "${SOURCE_TAILSCALE_IPV4}" "${DEST_TAILSCALE_IPV4}" \
    "${DEST_RAID_MOUNT}" "${DEST_PATH}" "${RETENTION_DAYS}" "${DEST_MARKER}" <<'REMOTE'
set -Eeuo pipefail
expected_client_ip=$1
expected_server_ip=$2
raid_mount=$3
dest_path=$4
retention_days=$5
marker_name=$6
read -r client_ip _ server_ip _ <<<"${SSH_CONNECTION:-}"
[[ "${client_ip}" == "${expected_client_ip}" && "${server_ip}" == "${expected_server_ip}" ]]
mountpoint --quiet -- "${raid_mount}"
[[ "$(findmnt -n -o TARGET --target "${raid_mount}")" == "${raid_mount}" ]]
[[ "$(realpath -m -- "${dest_path}")" == "${dest_path}" ]]
[[ -f "${dest_path}/${marker_name}" && ! -L "${dest_path}/${marker_name}" ]]
[[ "$(<"${dest_path}/${marker_name}")" == 'klukai-offsite-backups-v1' ]]
snapshot_count=$(find "${dest_path}" -maxdepth 1 -type f -name 'backups-*.tar.gz' | wc -l)
old_count=$(find "${dest_path}" -maxdepth 1 -type f -name 'backups-*.tar.gz' -mtime "+${retention_days}" | wc -l)
printf '%s %s\n' "${snapshot_count}" "${old_count}"
REMOTE
)

log "backup complete: ${tar_name}, ${remote_size} bytes, ${#recent_dumps[@]} recent validated DB dump(s)"
log "destination now has ${snapshot_count} snapshot(s); ${old_count} exceed ${RETENTION_DAYS}d"
if (( old_count > 0 )); then
  log "NOTICE: old snapshots were preserved; prune only after an independent non-RAID copy is verified"
fi
