#!/usr/bin/env bash
# scripts/disaster-recovery.sh — single-command DR drill for klukai.
#
# Per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §8.7.
#
# Procedure (backup is located + staged + verified BEFORE the stack is touched):
#   1. Locate offsite tar on dominus (same DEST conventions as
#      offsite-backup.sh / restore-from-backup.sh).
#   2. Pull + extract + verify the backup archive locally.
#   3. Stop klukai compose stack on amarillo.
#   4. Restore PG dump (klukai-db-*.sql.gz, plain SQL via gunzip|psql).
#   5. Restore Qdrant collections from qdrant/*.snapshot.
#   6. Restore companion-images volume.
#   7. Restart stack.
#   8. Run smoke tests.
#   9. Report time-to-recovery.
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
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Offsite conventions — keep in sync with offsite-backup.sh / restore-from-backup.sh.
DEST_HOST="${DEST_HOST:-dominus}"
DEST_PATH="${DEST_PATH:-/c/Users/jalsarraf/klukai-backups/}"
RESTORE_DIR="${RESTORE_DIR:-/mnt/nvmeINT/restore}"

LOG_DIR="${LOG_DIR:-/mnt/nvmeINT/logs}"
DR_DIR="${LOG_DIR}/dr-drill"
mkdir -p "$DR_DIR"
STAMP="$(date -u +%Y-%m-%d-%H%M%S)"
DRILL_LOG="${DR_DIR}/${STAMP}.log"
DRILL_JSON="${DR_DIR}/${STAMP}.json"

exec > >(tee -a "$DRILL_LOG") 2>&1

start_ts="$(date -u +%s)"

# offsite-backup.sh names tars backups-YYYYMMDD-HHMM.tar.gz; --backup-date is
# given as YYYY-MM-DD, so strip the dashes to build the glob.
if [[ -n "$BACKUP_DATE" ]]; then
  BACKUP_PATTERN="backups-${BACKUP_DATE//-/}-*.tar.gz"
else
  BACKUP_PATTERN="backups-*.tar.gz"
fi

echo "[DR drill] start ${STAMP} (dry_run=${DRY_RUN}, backup_date=${BACKUP_DATE:-latest})"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DR drill] DRY-RUN — would:"
  echo "  1. ssh ${DEST_HOST} 'ls -t ${DEST_PATH}${BACKUP_PATTERN}'"
  echo "  2. scp newest tar to ${RESTORE_DIR}/ + extract + verify klukai-db-*.sql.gz"
  echo "  3. docker compose down"
  echo "  4. gunzip klukai-db-*.sql.gz | psql into infra-postgres (gated by DR_CONFIRM_RESTORE=yes)"
  echo "  5. POST qdrant/*.snapshot to /collections/<coll>/snapshots/upload"
  echo "  6. restore companion-images volume from klukai/images-latest/"
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
fatal() { echo "[DR drill] FATAL: $*" >&2; exit 1; }

# ── Phase A: locate + stage + verify the backup BEFORE touching the stack ──

step "1. Locate offsite backup on ${DEST_HOST} (pattern: ${BACKUP_PATTERN})"
LATEST_TARBALL=$(ssh "$DEST_HOST" "ls -t ${DEST_PATH}${BACKUP_PATTERN} 2>/dev/null | head -1") || true
[[ -n "${LATEST_TARBALL:-}" ]] \
  || fatal "no backup matching ${BACKUP_PATTERN} at ${DEST_HOST}:${DEST_PATH} — stack untouched"
echo "[DR drill]   selected: $LATEST_TARBALL"

step "2. Pull backup to ${RESTORE_DIR}/"
mkdir -p "$RESTORE_DIR"
scp "${DEST_HOST}:${LATEST_TARBALL}" "${RESTORE_DIR}/" \
  || fatal "could not pull ${LATEST_TARBALL} from ${DEST_HOST} — stack untouched"
TARBALL_LOCAL="${RESTORE_DIR}/$(basename "$LATEST_TARBALL")"

step "3. Extract + verify"
EXTRACT_DIR="${RESTORE_DIR}/extracted-${STAMP}"
mkdir -p "$EXTRACT_DIR"
tar -xzf "$TARBALL_LOCAL" -C "$EXTRACT_DIR" \
  || fatal "tar extract failed (${TARBALL_LOCAL}) — stack untouched"

# Nightly backup layout (backup-companions.sh + offsite-backup.sh):
#   klukai/klukai-db-YYYYMMDD-*.sql.gz   plain SQL, gzipped
#   klukai/images-latest/                image files
#   qdrant/<collection>.snapshot         Qdrant snapshots (SACRED)
PG_DUMP=$(find "$EXTRACT_DIR" -name "klukai-db-*.sql.gz" -print | sort | tail -1)
[[ -n "$PG_DUMP" ]] \
  || fatal "no klukai-db-*.sql.gz in backup — refusing to proceed; stack untouched"
gzip -t "$PG_DUMP" \
  || fatal "PG dump failed gzip integrity check (${PG_DUMP}) — stack untouched"
echo "[DR drill]   PG dump verified: $PG_DUMP"

QDRANT_SNAP_DIR="${EXTRACT_DIR}/qdrant"
if compgen -G "${QDRANT_SNAP_DIR}/*.snapshot" > /dev/null; then
  echo "[DR drill]   Qdrant snapshots present: $(ls "$QDRANT_SNAP_DIR")"
else
  echo "[DR drill]   WARN: no Qdrant snapshots in backup — vector restore will be skipped"
fi

# ── Phase B: backup verified — now (and only now) touch the stack ──────────

step "4. Stop klukai compose stack"
docker compose down

step "5. Restore PG dump"
# SAFETY: Klukai data lives as companion_* TABLES inside the SHARED `aichat`
# database (infra-postgres) — kairi and other apps share that DB. So this must
# NEVER drop the database. The dump is a plain-SQL pg_dump of the klukai scope
# (gunzip|psql); the operator MUST confirm the dump's scope is companion_*
# only before enabling the destructive restore.
if [[ "${DR_CONFIRM_RESTORE:-}" == "yes" ]]; then
  gunzip -c "$PG_DUMP" \
    | docker exec -i infra-postgres psql -U aichat -d aichat -v ON_ERROR_STOP=0 \
    || echo "[DR drill]   warn: psql restore reported errors — review above"
else
  echo "[DR drill]   PG restore is DESTRUCTIVE to companion_* objects in the shared aichat DB."
  echo "[DR drill]   Set DR_CONFIRM_RESTORE=yes to run it (after verifying the dump scope). Skipping."
fi

step "6. Restore Qdrant snapshots"
if compgen -G "${QDRANT_SNAP_DIR}/*.snapshot" > /dev/null; then
  # aichat-vector (Qdrant) is external + already running — nothing to start here.
  # Upload target is operator-configurable (host-reachable Qdrant port).
  QDRANT_RESTORE_URL="${QDRANT_RESTORE_URL:-http://localhost:6333}"
  for snap in "${QDRANT_SNAP_DIR}"/*.snapshot; do
    [[ -e "$snap" ]] || continue
    coll="$(basename "$snap" .snapshot)"
    curl -sf -X POST "${QDRANT_RESTORE_URL}/collections/${coll}/snapshots/upload" \
      -H "Content-Type: multipart/form-data" \
      -F "snapshot=@${snap}" || echo "[DR drill]   warn: failed to upload ${coll}"
  done
else
  echo "[DR drill]   no Qdrant snapshots in backup; skipping"
fi

step "7. Restore companion-images volume"
if [[ -d "${EXTRACT_DIR}/klukai/images-latest" ]]; then
  docker run --rm \
    -v companion_companion-images:/dst \
    -v "${EXTRACT_DIR}/klukai/images-latest":/src:ro \
    alpine sh -c "cp -a /src/. /dst/"
else
  echo "[DR drill]   no images-latest dir in backup; skipping"
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
  "backup_tar": "$(basename "$TARBALL_LOCAL")",
  "duration_seconds": ${duration},
  "rto_target_seconds": ${rto_target},
  "rto_achieved": ${rto_ok},
  "outcome": "$([ "$rto_ok" = "true" ] && echo passed || echo missed-rto)"
}
JSON_EOF

echo "[DR drill] complete — duration ${duration}s — RTO target ${rto_target}s — achieved: ${rto_ok}"
echo "[DR drill] report: $DRILL_JSON"
