#!/usr/bin/env bash
# Reversible Docker + containerd relocation for dominus-nobara.
#
# The old stores are never renamed or deleted. On any failure after shutdown,
# the EXIT trap restores the exact prior configs/drop-ins and restarts the old
# runtime. Re-running after a completed migration performs verification only.
set -Eeuo pipefail

readonly expected_host=dominus-nobara
readonly expected_tailscale_ipv4=100.107.121.5
readonly raid_mount=/mnt/nvmer0
readonly docker_source=/var/lib/docker
readonly containerd_source=/var/lib/containerd
readonly docker_target=/mnt/nvmer0/docker-data
readonly containerd_target=/mnt/nvmer0/containerd
readonly ledger_root=/mnt/nvmer0/services/ai-stack/backups/container-storage-v1
readonly state_root=/mnt/nvmer0/services/ai-stack/state/container-storage-v1
readonly complete_marker=/mnt/nvmer0/services/ai-stack/state/container-storage-v1/COMPLETE
readonly target_start_marker=/mnt/nvmer0/services/ai-stack/state/container-storage-v1/TARGET-START-ATTEMPTED
readonly game_marker=/run/user/1000/dominus-gpu/game-active
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly source_root
readonly docker_dropin_source="$source_root/systemd/docker.service.d/10-dominus-nvme-mount.conf"
readonly containerd_dropin_source="$source_root/systemd/containerd.service.d/10-dominus-nvme-mount.conf"
readonly docker_dropin_target=/etc/systemd/system/docker.service.d/10-dominus-nvme-mount.conf
readonly containerd_dropin_target=/etc/systemd/system/containerd.service.d/10-dominus-nvme-mount.conf

mode=migrate
services_stopped=0
configs_changed=0
migration_complete=0

log() {
  printf '[container-storage-migration] %s\n' "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

usage() {
  cat >&2 <<'EOF'
Usage: migrate-container-storage.sh [--preflight]

  --preflight  Verify the locked host, Tailscale session, RAID, runtime roots,
               configuration, and free space without stopping or changing a service.
EOF
}

require_file() {
  [[ -f "$1" ]] || die "required file is absent: $1"
}

assert_tailscale_ssh() {
  [[ "$(hostname -s)" == "$expected_host" ]] || \
    die "must run on $expected_host"
  tailscale ip -4 | grep -Fxq "$expected_tailscale_ipv4" || \
    die "locked Tailscale IP is not active"
  [[ -n "${SSH_CONNECTION:-}" ]] || \
    die "SSH_CONNECTION is missing; reconnect over Tailscale and invoke sudo --preserve-env=SSH_CONNECTION"

  local client_ip server_ip
  read -r client_ip _ server_ip _ <<<"$SSH_CONNECTION"
  [[ "$server_ip" == "$expected_tailscale_ipv4" ]] || \
    die "SSH server endpoint is not the locked Tailscale IP: ${server_ip:-missing}"
  tailscale status --json \
    | jq -e --arg client "$client_ip" \
      '[.Peer[]?.TailscaleIPs[]?] | index($client) != null' >/dev/null || \
    die "SSH client ${client_ip:-missing} is not a current Tailscale peer"
}

assert_no_game() {
  [[ ! -e "$game_marker" ]] || die "game marker is active: $game_marker"
  local game_status
  game_status=$(runuser -u jalsarraf -- \
    env XDG_RUNTIME_DIR=/run/user/1000 gamemoded -s 2>&1) || \
    die "cannot query GameMode state: $game_status"
  [[ "$game_status" == *"gamemode is inactive"* ]] || \
    die "GameMode is not inactive: $game_status"
}

assert_raid() {
  mountpoint --quiet "$raid_mount" || die "$raid_mount is not a mountpoint"
  [[ "$(findmnt -n -o TARGET --target "$raid_mount")" == "$raid_mount" ]] || \
    die "resolved filesystem target is not $raid_mount"
  [[ "$(findmnt -n -o FSTYPE --target "$raid_mount")" == xfs ]] || \
    die "$raid_mount is not XFS"
  xfs_info "$raid_mount" | grep -q 'ftype=1' || die "XFS ftype=1 is required"
  xfs_info "$raid_mount" | grep -q 'reflink=1' || die "XFS reflink=1 is required"
}

runtime_inventory() {
  local destination=$1
  install -d -o root -g root -m 0700 "$destination"
  docker ps -aq --no-trunc | LC_ALL=C sort -u >"$destination/container-ids.txt"
  docker image ls --all --quiet --no-trunc \
    | LC_ALL=C sort -u >"$destination/image-ids.txt"
  docker ps -a --no-trunc --format '{{.ID}}\t{{.Image}}\t{{.Names}}' \
    | LC_ALL=C sort >"$destination/containers.txt"
  docker image ls --all --no-trunc \
    --format '{{.ID}}\t{{.Repository}}:{{.Tag}}' \
    | LC_ALL=C sort >"$destination/images.txt"
  docker volume ls --format '{{.Name}}\t{{.Driver}}' \
    | LC_ALL=C sort >"$destination/volumes.txt"
  docker network ls --no-trunc \
    --format '{{.ID}}\t{{.Name}}\t{{.Driver}}\t{{.Scope}}' \
    | awk -F '\t' 'BEGIN {OFS = "\t"}
        $2 == "bridge" || $2 == "host" || $2 == "none" {
          print "builtin", $2, $3, $4
          next
        }
        {print "custom", $1, $2, $3, $4}' \
    | LC_ALL=C sort >"$destination/networks.txt"
}

current_containerd_root() {
  containerd config dump | python3 -c '
import re
import sys

match = re.search(r"^root\s*=\s*[\047\042]([^\047\042]+)[\047\042]\s*$", sys.stdin.read(), re.MULTILINE)
if match is None:
    raise SystemExit("containerd config dump did not expose a parseable root")
print(match.group(1))
'
}

assert_live_roots() {
  local wanted_docker_root=$1 wanted_containerd_root=$2
  local live_docker_root live_containerd_root
  live_docker_root=$(docker info --format '{{.DockerRootDir}}') || \
    die "Docker is not queryable"
  live_containerd_root=$(current_containerd_root) || \
    die "containerd is not queryable"
  [[ "$live_docker_root" == "$wanted_docker_root" ]] || \
    die "DockerRootDir is $live_docker_root; expected $wanted_docker_root"
  [[ "$live_containerd_root" == "$wanted_containerd_root" ]] || \
    die "live containerd root is $live_containerd_root; expected $wanted_containerd_root"
}

assert_capacity() {
  local source_bytes available_bytes required_bytes
  source_bytes=$(du -s --block-size=1 "$docker_source" "$containerd_source" \
    | awk '{total += $1} END {print total}')
  available_bytes=$(df --output=avail --block-size=1 "$raid_mount" \
    | awk 'NR == 2 {print $1}')
  [[ "$source_bytes" =~ ^[0-9]+$ && "$available_bytes" =~ ^[0-9]+$ ]] || \
    die "could not calculate source size and RAID capacity"
  required_bytes=$((source_bytes + source_bytes / 10 + 1073741824))
  (( available_bytes >= required_bytes )) || \
    die "RAID free space is insufficient: $available_bytes available, $required_bytes required"
  log "capacity check passed: $source_bytes source bytes, $available_bytes RAID bytes free"
}

backup_optional_file() {
  local source=$1 destination=$2 absence_marker=$3
  if [[ -e "$source" || -L "$source" ]]; then
    rm -f -- "$absence_marker"
    cp -a -- "$source" "$destination"
  else
    rm -f -- "$destination"
    : >"$absence_marker"
  fi
}

restore_optional_file() {
  local destination=$1 backup=$2 absence_marker=$3
  if [[ -f "$absence_marker" ]]; then
    rm -f -- "$destination"
  else
    install -D -m 0644 -- "$backup" "$destination"
  fi
}

restore_old_runtime() {
  local rollback_failed=0
  log "restoring pre-migration Docker/containerd configuration"
  systemctl stop docker.service docker.socket containerd.service >/dev/null 2>&1 || true

  restore_optional_file /etc/docker/daemon.json \
    "$ledger_root/daemon.json.before" "$ledger_root/daemon.json.absent"
  install -m 0644 -- "$ledger_root/containerd-config.toml.before" \
    /etc/containerd/config.toml
  restore_optional_file "$docker_dropin_target" \
    "$ledger_root/docker-mount-dropin.before" "$ledger_root/docker-mount-dropin.absent"
  restore_optional_file "$containerd_dropin_target" \
    "$ledger_root/containerd-mount-dropin.before" "$ledger_root/containerd-mount-dropin.absent"

  restorecon /etc/docker/daemon.json /etc/containerd/config.toml \
    "$docker_dropin_target" "$containerd_dropin_target" >/dev/null 2>&1 || true
  systemctl daemon-reload || rollback_failed=1
  systemctl start containerd.service docker.socket docker.service || rollback_failed=1
  [[ "$(docker info --format '{{.DockerRootDir}}')" == "$docker_source" ]] || \
    { log "CRITICAL: rollback DockerRootDir is not $docker_source"; rollback_failed=1; }
  [[ "$(current_containerd_root)" == "$containerd_source" ]] || \
    { log "CRITICAL: rollback containerd root is not $containerd_source"; rollback_failed=1; }
  (( rollback_failed == 0 )) || return 1
  log "rollback complete; both RAID target stores were deliberately retained"
}

on_exit() {
  local rc=$?
  trap - EXIT
  set +e
  if (( rc != 0 && migration_complete == 0 )); then
    if (( configs_changed == 1 )); then
      restore_old_runtime || \
        log "CRITICAL: automatic rollback did not verify; use the saved ledger before any retry"
    elif (( services_stopped == 1 )); then
      systemctl start containerd.service docker.socket docker.service || true
    fi
    log "migration failed with exit $rc; old stores were not deleted"
  fi
  exit "$rc"
}
trap on_exit EXIT

case $# in
  0) ;;
  1)
    [[ $1 == --preflight ]] || { usage; exit 2; }
    mode=preflight
    ;;
  *)
    usage
    exit 2
    ;;
esac

[[ $EUID -eq 0 ]] || \
  die "run with sudo --preserve-env=SSH_CONNECTION from the Tailscale SSH session"
for command_name in awk cmp containerd cp df diff dirname docker dockerd du find \
  findmnt flock gamemoded grep hostname install jq mountpoint python3 readlink \
  restorecon rm rsync runuser sort systemctl systemd-analyze tailscale xfs_info; do
  command -v "$command_name" >/dev/null || die "missing command: $command_name"
done

exec 9>/run/lock/dominus-container-storage-migration.lock
flock -n 9 || die "another container-storage migration is running"

assert_tailscale_ssh
assert_no_game
assert_raid
require_file "$docker_dropin_source"
require_file "$containerd_dropin_source"
require_file /etc/containerd/config.toml
[[ -d "$docker_source" && -d "$containerd_source" ]] || \
  die "one or both original stores are absent"
[[ ! -L "$docker_source" && ! -L "$containerd_source" ]] || \
  die "an original store is a symlink; refusing an ambiguous copy source"

if [[ -f "$complete_marker" ]]; then
  log "completion marker exists; performing idempotent verification"
  assert_live_roots "$docker_target" "$containerd_target"
  [[ -d "$docker_target" && ! -L "$docker_target" ]] || \
    die "completed Docker target is absent or a symlink"
  [[ -d "$containerd_target" && ! -L "$containerd_target" ]] || \
    die "completed containerd target is absent or a symlink"
  grep -Fxq 'root = "/mnt/nvmer0/containerd"' /etc/containerd/config.toml || \
    die "completion marker exists but containerd root drifted"
  cmp --silent "$docker_dropin_source" "$docker_dropin_target" || \
    die "Docker mount drop-in drifted"
  cmp --silent "$containerd_dropin_source" "$containerd_dropin_target" || \
    die "containerd mount drop-in drifted"
  docker info --format '{{json .Runtimes}}' | jq -e 'has("nvidia")' >/dev/null || \
    die "NVIDIA Docker runtime is absent"
  log "already complete and verified; no state changed"
  migration_complete=1
  exit 0
fi

[[ ! -f "$target_start_marker" ]] || \
  die "a prior target-start attempt lacks COMPLETE; inspect both stores and rollback ledger before retrying"

assert_live_roots "$docker_source" "$containerd_source"
grep -Fxq 'disabled_plugins = ["cri"]' /etc/containerd/config.toml || \
  die "refusing to change containerd without the accepted disabled_plugins setting"
if [[ -f /etc/docker/daemon.json ]]; then
  python3 - /etc/docker/daemon.json <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if "nvidia" not in document.get("runtimes", {}):
    raise SystemExit("existing NVIDIA runtime is absent")
root = document.get("data-root")
if root not in (None, "/var/lib/docker"):
    raise SystemExit(f"unexpected pre-migration Docker data-root: {root!r}")
PY
else
  die "/etc/docker/daemon.json is absent; the accepted NVIDIA runtime cannot be preserved"
fi
assert_capacity

if [[ $mode == preflight ]]; then
  log "preflight passed; no service or persistent file was changed"
  migration_complete=1
  exit 0
fi

install -d -o root -g root -m 0700 "$ledger_root" "$state_root"
[[ ! -L "$docker_target" && ! -L "$containerd_target" ]] || \
  die "a RAID target store is a symlink"
if [[ -n "$(find "$docker_target" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" \
   || -n "$(find "$containerd_target" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  [[ -f "$state_root/IN-PROGRESS" ]] || \
    die "non-empty target store lacks this migration's IN-PROGRESS marker"
fi

if [[ ! -f "$state_root/IN-PROGRESS" ]]; then
  backup_optional_file /etc/docker/daemon.json \
    "$ledger_root/daemon.json.before" "$ledger_root/daemon.json.absent"
  cp -a -- /etc/containerd/config.toml "$ledger_root/containerd-config.toml.before"
  backup_optional_file "$docker_dropin_target" \
    "$ledger_root/docker-mount-dropin.before" "$ledger_root/docker-mount-dropin.absent"
  backup_optional_file "$containerd_dropin_target" \
    "$ledger_root/containerd-mount-dropin.before" "$ledger_root/containerd-mount-dropin.absent"
  runtime_inventory "$ledger_root/before"
  printf '%s\n' 'container-storage-v1' >"$state_root/IN-PROGRESS"
fi

assert_no_game
log "stopping Docker, its socket, and containerd for a stable copy"
systemctl stop docker.service docker.socket containerd.service
services_stopped=1
systemctl is-active --quiet docker.service && die "Docker did not stop"
systemctl is-active --quiet containerd.service && die "containerd did not stop"

install -d -o root -g root -m 0711 "$docker_target" "$containerd_target"
log "copying Docker store without deletion"
rsync -aHAX --numeric-ids --stats "$docker_source/" "$docker_target/"
log "copying containerd store without deletion"
rsync -aHAX --numeric-ids --stats "$containerd_source/" "$containerd_target/"

# The rollback trap must become armed before the first atomic config replace.
# Backups and the stable source copies are complete at this point.
configs_changed=1
python3 - /etc/docker/daemon.json <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
if "nvidia" not in document.get("runtimes", {}):
    raise SystemExit("refusing to lose the NVIDIA runtime")
old = document.get("data-root")
if old not in (None, "/var/lib/docker", "/mnt/nvmer0/docker-data"):
    raise SystemExit(f"unexpected Docker data-root: {old!r}")
document["data-root"] = "/mnt/nvmer0/docker-data"
temporary = path.with_name(".daemon.json.dominus.partial")
temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY

python3 - /etc/containerd/config.toml <<'PY'
import os
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
if not re.search(r'^disabled_plugins\s*=\s*\["cri"\]\s*$', text, re.MULTILINE):
    raise SystemExit('accepted disabled_plugins = ["cri"] setting is absent')
target = 'root = "/mnt/nvmer0/containerd"'
if re.search(r'^root\s*=\s*"/mnt/nvmer0/containerd"\s*$', text, re.MULTILINE):
    updated = text
elif re.search(r'^#root\s*=\s*"/var/lib/containerd"\s*$', text, re.MULTILINE):
    updated = re.sub(
        r'^#root\s*=\s*"/var/lib/containerd"\s*$', target, text,
        count=1, flags=re.MULTILINE,
    )
elif re.search(r'^root\s*=\s*"/var/lib/containerd"\s*$', text, re.MULTILINE):
    updated = re.sub(
        r'^root\s*=\s*"/var/lib/containerd"\s*$', target, text,
        count=1, flags=re.MULTILINE,
    )
else:
    raise SystemExit("unexpected containerd root configuration")
temporary = path.with_name(".config.toml.dominus.partial")
temporary.write_text(updated, encoding="utf-8")
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY

install -D -m 0644 "$docker_dropin_source" "$docker_dropin_target"
install -D -m 0644 "$containerd_dropin_source" "$containerd_dropin_target"
restorecon /etc/docker/daemon.json /etc/containerd/config.toml \
  "$docker_dropin_target" "$containerd_dropin_target" >/dev/null 2>&1 || true

dockerd --validate --config-file /etc/docker/daemon.json
systemctl daemon-reload
systemd-analyze verify docker.service containerd.service
assert_no_game

log "starting mount-gated containerd and Docker on RAID storage"
printf '%s\n' 'container-storage-v1' >"$target_start_marker"
systemctl start containerd.service docker.socket docker.service
[[ "$(docker info --format '{{.DockerRootDir}}')" == "$docker_target" ]] || \
  die "Docker started with an unexpected data root"
[[ "$(current_containerd_root)" == "$containerd_target" ]] || \
  die "containerd started with an unexpected root"
docker info --format '{{json .Runtimes}}' | jq -e 'has("nvidia")' >/dev/null || \
  die "NVIDIA runtime disappeared"
cmp --silent "$docker_dropin_source" "$docker_dropin_target" || \
  die "Docker mount drop-in failed verification"
cmp --silent "$containerd_dropin_source" "$containerd_dropin_target" || \
  die "containerd mount drop-in failed verification"
[[ "$(systemctl cat docker.service containerd.service \
  | grep -Fc 'ExecStartPre=/usr/bin/mountpoint --quiet /mnt/nvmer0')" == 2 ]] || \
  die "one or both mountpoint ExecStartPre gates are absent"

install -d -o root -g root -m 0700 "$ledger_root/after"
runtime_inventory "$ledger_root/after"
for inventory_name in container-ids.txt image-ids.txt containers.txt images.txt \
  volumes.txt networks.txt; do
  diff -u "$ledger_root/before/$inventory_name" "$ledger_root/after/$inventory_name"
done

python3 - "$complete_marker" <<'PY'
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

path = Path(sys.argv[1])
document = {
    "version": 1,
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "docker_root": "/mnt/nvmer0/docker-data",
    "containerd_root": "/mnt/nvmer0/containerd",
    "old_stores_retained": ["/var/lib/docker", "/var/lib/containerd"],
    "tailscale_server_ip": "100.107.121.5",
}
temporary = path.with_name(".COMPLETE.partial")
temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY

migration_complete=1
log "migration complete; old stores remain intact for rollback"
