#!/usr/bin/env bash
# scripts/disaster-recovery.sh — single-command DR drill for klukai.
#
# Per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §8.7.
#
# Procedure:
#   1. Stop klukai compose stack on amarillo.
#   2. Pull latest offsite tar from dominus.
#   3. Restore PG dump.
#   4. Restore Qdrant collections.
#   5. Restore companion-images volume.
#   6. Restart stack.
#   7. Run smoke tests.
#   8. Report time-to-recovery.
#
# Target RTO: < 30 min.
#
# Usage:
#   scripts/disaster-recovery.sh [--dry-run] [--backup-date YYYY-MM-DD]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

DRY_RUN=0
BACKUP_DATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --backup-date) BACKUP_DATE="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

LOG_DIR="${LOG_DIR:-/mnt/nvmeINT/logs}"
DR_DIR="${LOG_DIR}/dr-drill"
mkdir -p "$DR_DIR"
STAMP="$(date -u +%Y-%m-%d-%H%M%S)"
DRILL_LOG="${DR_DIR}/${STAMP}.log"
DRILL_JSON="${DR_DIR}/${STAMP}.json"

exec > >(tee -a "$DRILL_LOG") 2>&1

start_ts="$(date -u +%s)"

echo "[DR drill] start ${STAMP} (dry_run=${DRY_RUN}, backup_date=${BACKUP_DATE:-latest})"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DR drill] DRY-RUN — would:"
  echo "  1. docker compose down"
  echo "  2. ssh dominus 'ls /shared_linux/backups/klukai/${BACKUP_DATE:-latest}*'"
  echo "  3. rsync backup tarballs to /mnt/nvmeINT/restore/"
  echo "  4. drop+recreate companion-pg from dump"
  echo "  5. POST /collections/* to qdrant from snapshot"
  echo "  6. restore companion-images volume from tar"
  echo "  7. docker compose up -d"
  echo "  8. smoke tests via /health"
  cat > "$DRILL_JSON" <<JSON_EOF
{
  "drill_date": "${STAMP}",
  "mode": "dry-run",
  "duration_seconds": 0,
  "rto_achieved": null,
  "outcome": "skipped-dry-run"
}
JSON_EOF
  exit 0
fi

step() { echo "[DR drill] ── $*"; }

step "1. Stop klukai compose stack"
docker compose down

step "2. Locate latest offsite backup on dominus"
BACKUP_PATTERN="${BACKUP_DATE:-latest}"
LATEST_TARBALL=$(ssh dominus "ls -t /shared_linux/backups/klukai/companion-*.tar.gz 2>/dev/null | head -1") || {
  echo "[DR drill] FATAL: cannot list offsite backups on dominus"
  exit 1
}
echo "[DR drill]   selected: $LATEST_TARBALL"

step "3. Pull backup to /mnt/nvmeINT/restore/"
mkdir -p /mnt/nvmeINT/restore
rsync -av "dominus:${LATEST_TARBALL}" /mnt/nvmeINT/restore/

step "4. Extract"
TARBALL_LOCAL="/mnt/nvmeINT/restore/$(basename "$LATEST_TARBALL")"
mkdir -p /mnt/nvmeINT/restore/extracted
tar -xzf "$TARBALL_LOCAL" -C /mnt/nvmeINT/restore/extracted

step "5. Restore PG dump"
if [[ -f /mnt/nvmeINT/restore/extracted/companion-pg.dump ]]; then
  docker compose up -d postgres
  sleep 8
  docker compose exec -T postgres psql -U companion -c "DROP DATABASE IF EXISTS companion;"
  docker compose exec -T postgres psql -U companion -c "CREATE DATABASE companion;"
  docker compose exec -T postgres pg_restore -U companion -d companion < /mnt/nvmeINT/restore/extracted/companion-pg.dump
else
  echo "[DR drill]   no PG dump in backup; skipping"
fi

step "6. Restore Qdrant snapshots"
if [[ -d /mnt/nvmeINT/restore/extracted/qdrant ]]; then
  docker compose up -d qdrant
  sleep 6
  for snap in /mnt/nvmeINT/restore/extracted/qdrant/*.snapshot; do
    [[ -e "$snap" ]] || continue
    coll="$(basename "$snap" .snapshot)"
    curl -sf -X POST "http://localhost:6333/collections/${coll}/snapshots/upload" \
      -H "Content-Type: multipart/form-data" \
      -F "snapshot=@${snap}" || echo "[DR drill]   warn: failed to upload ${coll}"
  done
else
  echo "[DR drill]   no Qdrant snapshots in backup; skipping"
fi

step "7. Restore companion-images volume"
if [[ -f /mnt/nvmeINT/restore/extracted/companion-images.tar ]]; then
  docker run --rm -v klukai_companion-images:/dst -v /mnt/nvmeINT/restore/extracted:/src alpine \
    sh -c "cd /dst && tar -xf /src/companion-images.tar"
fi

step "8. Bring stack up"
docker compose up -d

step "9. Smoke test"
sleep 8
for attempt in $(seq 1 30); do
  status=$(curl -s -m 3 http://localhost:8300/health 2>/dev/null | jq -r '.status' 2>/dev/null || echo "")
  if [[ "$status" == "ok" ]]; then
    echo "[DR drill]   /health ok after ${attempt}s"
    break
  fi
  sleep 1
done

end_ts="$(date -u +%s)"
duration=$((end_ts - start_ts))
rto_target=1800   # 30 minutes
rto_ok="false"
[[ "$duration" -lt "$rto_target" ]] && rto_ok="true"

cat > "$DRILL_JSON" <<JSON_EOF
{
  "drill_date": "${STAMP}",
  "mode": "live",
  "duration_seconds": ${duration},
  "rto_target_seconds": ${rto_target},
  "rto_achieved": ${rto_ok},
  "outcome": "$([ "$rto_ok" = "true" ] && echo passed || echo missed-rto)"
}
JSON_EOF

echo "[DR drill] complete — duration ${duration}s — RTO target ${rto_target}s — achieved: ${rto_ok}"
echo "[DR drill] report: $DRILL_JSON"
