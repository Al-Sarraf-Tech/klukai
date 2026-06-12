#!/usr/bin/env bash
# verify-backups.sh — sanity-check nightly backups landed and look healthy.
#
# Meant to run as a systemd timer/cron on amarillo after the nightly
# backup job finishes. Reports to stdout + exits 0 on success, non-zero
# on any failure so systemd notifications fire.
#
# Checks:
#   1. Today's kairi DB dump exists and > 1 KB
#   2. Today's klukai DB dump exists and > 1 KB
#   3. Klukai dumps are valid gzip
#   4. Klukai dumps contain at least one INSERT line (non-empty schema export)
#   5. Qdrant snapshots for SACRED collections (companion_episodes,
#      companion_exchanges) exist, are non-empty, and were refreshed today
#   6. klukai images dir is non-empty (if the volume has data)
#
# Exits:
#   0 — all checks passed
#   1 — a check failed (specific failure logged to stderr)

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d)
BACKUP_DIR="${BACKUP_DIR:-/mnt/nvmeINT/backups}"
LOG="${LOG:-/mnt/nvmeINT/logs/backup-verify.log}"

mkdir -p "$(dirname "$LOG")"
echo "$(date) — starting backup verification" >> "$LOG"

fail() {
  local msg="$1"
  echo "FAIL: $msg" >&2
  echo "$(date) — FAIL: $msg" >> "$LOG"
  exit 1
}

# -- 1. Check kairi DB dump exists for today
kairi_dump=$(find "$BACKUP_DIR/kairi" -name "kairi-db-${TIMESTAMP}-*.sql.gz" -size +1k -print -quit 2>/dev/null || true)
if [ -z "$kairi_dump" ]; then
  fail "kairi DB dump missing or < 1KB for $TIMESTAMP"
fi

# -- 2. Check klukai DB dump exists for today
klukai_dump=$(find "$BACKUP_DIR/klukai" -name "klukai-db-${TIMESTAMP}-*.sql.gz" -size +1k -print -quit 2>/dev/null || true)
if [ -z "$klukai_dump" ]; then
  fail "klukai DB dump missing or < 1KB for $TIMESTAMP"
fi

# -- 3. Validate gzip integrity
for dump in "$kairi_dump" "$klukai_dump"; do
  if ! gzip -t "$dump" 2>/dev/null; then
    fail "gzip integrity check failed: $dump"
  fi
done

# -- 4. Verify klukai dump contains data (at least one INSERT / COPY)
if ! zgrep -qE '^(INSERT|COPY)' "$klukai_dump" 2>/dev/null; then
  # A schema-only dump with no data is suspicious for an active deployment
  fail "klukai dump contains no INSERT/COPY data: $klukai_dump"
fi

# -- 5. Qdrant snapshots present + fresh (SACRED: episodes + exchanges)
# backup-companions.sh downloads collection snapshots to $BACKUP_DIR/qdrant/
# as <collection>.snapshot (overwritten nightly), and offsite-backup.sh ships
# that dir inside the tar. If episodes/exchanges snapshots are missing or
# stale, Klukai's SACRED vector memory is NOT in tonight's offsite archive.
for coll in companion_episodes companion_exchanges; do
  snap="$BACKUP_DIR/qdrant/${coll}.snapshot"
  if [ ! -s "$snap" ]; then
    fail "Qdrant snapshot missing/empty for SACRED collection: $coll ($snap)"
  fi
  if [ -z "$(find "$snap" -newermt "$(date +%Y-%m-%d)" -print -quit 2>/dev/null)" ]; then
    fail "Qdrant snapshot is STALE (not refreshed today): $snap"
  fi
done

# -- 6. Images dir non-empty check (warn-only, non-fatal)
if [ -d "$BACKUP_DIR/klukai/images-latest" ]; then
  image_count=$(find "$BACKUP_DIR/klukai/images-latest" -type f 2>/dev/null | wc -l)
  if [ "$image_count" -lt 1 ]; then
    echo "WARN: klukai images-latest is empty" >&2
    echo "$(date) — WARN: images-latest is empty" >> "$LOG"
  fi
fi

echo "$(date) — all backup checks passed" >> "$LOG"
echo "OK: $(basename "$kairi_dump"), $(basename "$klukai_dump")"
exit 0
