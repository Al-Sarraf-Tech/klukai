#!/usr/bin/env bash
# offsite-backup.sh — mirror amarillo's local backups to dominus over LAN.
#
# Runs ~30 min after nightly backup-companions.sh. Tars
# /mnt/nvmeINT/backups/ (klukai + kairi DB dumps, images) and pipes
# over ssh to dominus:~/klukai-backups/ as dated snapshot tars so
# amarillo hardware/drive death doesn't take the backups with it.
#
# Uses tar-over-ssh (not rsync) because dominus runs Windows Git Bash
# with no rsync in PATH. Backups are small (few MB) so a full transfer
# each night is fine.
#
# Retention: keep last 30 tars on dominus. Old ones pruned by filename date.
#
# Exits 0 on success, 2 on warning (no fresh dump found), 1 on fatal.

set -euo pipefail

SRC="${SRC:-/mnt/nvmeINT/backups/}"
DEST_HOST="${DEST_HOST:-dominus}"
DEST_PATH="${DEST_PATH:-/c/Users/jalsarraf/klukai-backups/}"
LOG="${LOG:-/mnt/nvmeINT/logs/offsite-backup.log}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M)"
TAR_NAME="backups-${STAMP}.tar.gz"

mkdir -p "$(dirname "$LOG")"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"
}

log "=== offsite backup start: $SRC -> $DEST_HOST:$DEST_PATH ==="

# Ensure destination exists on dominus
ssh -o LogLevel=ERROR -o ConnectTimeout=10 "$DEST_HOST" \
  "bash -l -c 'mkdir -p \"$DEST_PATH\"'" >> "$LOG" 2>&1 || {
    log "ERROR: could not create destination on $DEST_HOST"
    exit 1
}

# Verify source has content
if ! find "$SRC" -type f -name "*.sql.gz" -print -quit | grep -q .; then
  log "WARN: source has no DB dumps — upstream backup may have failed"
fi

# Tar + stream over SSH — create a compressed snapshot on dominus
tar -C "$SRC" -czf - . 2>> "$LOG" \
  | ssh -o LogLevel=ERROR -o ConnectTimeout=30 "$DEST_HOST" \
      "bash -l -c 'cat > \"${DEST_PATH}${TAR_NAME}\"'" || {
    log "ERROR: tar+ssh pipe failed"
    exit 1
}

# Verify the remote tar is non-empty and readable
REMOTE_SIZE=$(ssh -o LogLevel=ERROR "$DEST_HOST" \
  "bash -l -c 'stat -c%s \"${DEST_PATH}${TAR_NAME}\" 2>/dev/null'" || echo 0)

if [ "${REMOTE_SIZE:-0}" -lt 100 ]; then
  log "ERROR: remote tar too small ($REMOTE_SIZE bytes) — treating as failure"
  exit 1
fi

log "tar shipped — remote size: ${REMOTE_SIZE} bytes (${TAR_NAME})"

# Retention: prune tars older than RETENTION_DAYS
ssh -o LogLevel=ERROR "$DEST_HOST" "bash -l -c '
  find \"$DEST_PATH\" -name \"backups-*.tar.gz\" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
  ls \"$DEST_PATH\" 2>/dev/null | wc -l
'" 2>> "$LOG" | tail -1 | {
  read -r COUNT
  log "retention applied — ${COUNT:-?} snapshots remain on $DEST_HOST"
}

# Sanity: verify today's dump made it into the tar (quick peek)
TODAY=$(date +%Y%m%d)
HAS_TODAY=$(ssh -o LogLevel=ERROR "$DEST_HOST" "bash -l -c '
  tar -tzf \"${DEST_PATH}${TAR_NAME}\" 2>/dev/null | grep -c \"${TODAY}\" || echo 0
'" 2>> "$LOG" | tail -1 || echo 0)

if [ "${HAS_TODAY:-0}" -lt 1 ]; then
  log "WARN: tar contains no files matching today ($TODAY) — upstream backup may have failed"
  exit 2
fi

log "=== offsite backup ok — ${HAS_TODAY} fresh files in ${TAR_NAME} ==="
exit 0
