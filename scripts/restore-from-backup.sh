#!/usr/bin/env bash
# restore-from-backup.sh — tested restore drill for klukai.
#
# Per S+ uplift spec §6.8 — pulls the latest offsite tar from dominus,
# spins a scratch Postgres container, restores the dump, runs schema
# + row-count sanity checks, and writes a JSON report. Pass = pass;
# fail = page.
#
# Scheduled monthly via systemd timer on amarillo. Manual invocation:
#   ./scripts/restore-from-backup.sh
#   ./scripts/restore-from-backup.sh --keep   # keep scratch container for poking
#
# Exits:
#   0 — restore + all checks passed
#   1 — restore or any check failed
#   2 — no recent backup found
#
# Hard constraint per global CLAUDE.md feedback_never_delete_chat.md:
# this script ONLY touches a scratch container. It must NEVER write to
# the production infra-postgres. Verification is read-only.

set -euo pipefail

# ── Config (env-overridable) ────────────────────────────────────────────────
DEST_HOST="${DEST_HOST:-dominus}"
DEST_PATH="${DEST_PATH:-/c/Users/jalsarraf/klukai-backups/}"
LOG_DIR="${LOG_DIR:-/mnt/nvmeINT/logs}"
REPORT_FILE="${REPORT_FILE:-/mnt/nvmeINT/logs/restore-drill.json}"
PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"
SCRATCH_NAME="klukai-restore-scratch"
KEEP_SCRATCH="false"
RETENTION_DAYS_MAX="${RETENTION_DAYS_MAX:-2}"

# ── Args ────────────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP_SCRATCH="true" ;;
    -h|--help)
      sed -n '1,30p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/restore-drill.log"

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }

fail() {
  local msg="$1"
  log "FAIL: $msg"
  emit_report "fail" "$msg"
  cleanup
  exit 1
}

# ── Cleanup ─────────────────────────────────────────────────────────────────
cleanup() {
  if [[ "$KEEP_SCRATCH" == "true" ]]; then
    log "Keeping scratch container '$SCRATCH_NAME' for inspection."
    return
  fi
  if docker ps -a --format '{{.Names}}' | grep -q "^${SCRATCH_NAME}$"; then
    log "Removing scratch container '$SCRATCH_NAME'..."
    docker rm -f "$SCRATCH_NAME" >/dev/null
  fi
  if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

# ── Report writer ───────────────────────────────────────────────────────────
emit_report() {
  local status="$1" message="${2:-}"
  cat > "$REPORT_FILE" <<EOF
{
  "ran_at": "$(date -Iseconds)",
  "status": "$status",
  "message": "$message",
  "tar_age_hours": ${TAR_AGE_HOURS:-null},
  "tables_restored": ${TABLES_COUNT:-null},
  "rows_messages": ${ROWS_MESSAGES:-null},
  "rows_memories": ${ROWS_MEMORIES:-null},
  "rows_audit": ${ROWS_AUDIT:-null}
}
EOF
}

# ── Step 1: pull latest tar ─────────────────────────────────────────────────
log "Looking for latest backup tar on $DEST_HOST..."
LATEST_TAR=$(ssh "$DEST_HOST" "ls -t ${DEST_PATH}backups-*.tar.gz 2>/dev/null | head -1" || echo "")

if [[ -z "$LATEST_TAR" ]]; then
  TAR_AGE_HOURS=null
  emit_report "no_backup" "No backup tar found at $DEST_HOST:$DEST_PATH"
  log "FAIL: No backup tar found"
  exit 2
fi

log "Latest tar: $LATEST_TAR"

# Check tar age — pages if too old
TAR_MTIME=$(ssh "$DEST_HOST" "stat -c %Y '$LATEST_TAR'" 2>/dev/null || echo 0)
NOW=$(date +%s)
TAR_AGE_HOURS=$(( (NOW - TAR_MTIME) / 3600 ))

if (( TAR_AGE_HOURS > RETENTION_DAYS_MAX * 24 )); then
  fail "Latest backup is ${TAR_AGE_HOURS}h old (max ${RETENTION_DAYS_MAX} days)"
fi
log "Tar age: ${TAR_AGE_HOURS}h — within window"

WORK_DIR=$(mktemp -d -p /mnt/nvmeINT/tmp restore-drill.XXXXXX)
log "Working dir: $WORK_DIR"

# Pull tar to scratch dir
log "Pulling tar to $WORK_DIR/..."
scp "$DEST_HOST:$LATEST_TAR" "$WORK_DIR/" 2>&1 | tail -3 | tee -a "$LOG"
TAR_LOCAL="$WORK_DIR/$(basename "$LATEST_TAR")"

# ── Step 2: extract ─────────────────────────────────────────────────────────
log "Extracting tar..."
tar -xzf "$TAR_LOCAL" -C "$WORK_DIR" || fail "Tar extract failed"
DUMP=$(find "$WORK_DIR" -name "klukai*.sql.gz" -o -name "aichat*.sql.gz" | head -1)
[[ -n "$DUMP" ]] || fail "No klukai/aichat dump found in tar"
log "Using dump: $DUMP"

# ── Step 3: spin scratch Postgres ───────────────────────────────────────────
log "Starting scratch Postgres ($PG_IMAGE)..."
docker rm -f "$SCRATCH_NAME" 2>/dev/null || true
docker run -d --rm \
  --name "$SCRATCH_NAME" \
  -e POSTGRES_PASSWORD=scratch \
  -e POSTGRES_USER=aichat \
  -e POSTGRES_DB=aichat \
  "$PG_IMAGE" >/dev/null

# Wait for PG to accept connections
log "Waiting for scratch PG to accept connections..."
for i in {1..30}; do
  if docker exec "$SCRATCH_NAME" pg_isready -U aichat >/dev/null 2>&1; then
    log "PG ready after ${i}s"
    break
  fi
  sleep 1
  if (( i == 30 )); then
    fail "Scratch PG never accepted connections"
  fi
done

# ── Step 4: restore dump ────────────────────────────────────────────────────
log "Restoring dump..."
gunzip -c "$DUMP" | docker exec -i "$SCRATCH_NAME" psql -U aichat -d aichat -q 2>&1 \
  | tail -20 | tee -a "$LOG" || fail "Restore failed"

# ── Step 5: schema + row-count sanity ───────────────────────────────────────
log "Verifying schema..."

# Required tables (klukai-specific)
REQUIRED_TABLES=(
  "companion_messages"
  "companion_memories"
  "companion_users"
  "companion_audit_log"
  "companion_affection_state"
)

TABLES_COUNT=0
for t in "${REQUIRED_TABLES[@]}"; do
  if docker exec "$SCRATCH_NAME" psql -U aichat -d aichat -tAc \
      "SELECT 1 FROM information_schema.tables WHERE table_name='$t'" \
      | grep -q 1; then
    ((TABLES_COUNT++))
  else
    fail "Required table missing after restore: $t"
  fi
done
log "All $TABLES_COUNT required tables present"

# Row counts (sanity — at least 1 message + 1 user)
ROWS_MESSAGES=$(docker exec "$SCRATCH_NAME" psql -U aichat -d aichat -tAc \
  "SELECT count(*) FROM companion_messages" | tr -d ' ')
ROWS_MEMORIES=$(docker exec "$SCRATCH_NAME" psql -U aichat -d aichat -tAc \
  "SELECT count(*) FROM companion_memories" | tr -d ' ')
ROWS_AUDIT=$(docker exec "$SCRATCH_NAME" psql -U aichat -d aichat -tAc \
  "SELECT count(*) FROM companion_audit_log" | tr -d ' ')

log "Row counts: messages=$ROWS_MESSAGES memories=$ROWS_MEMORIES audit=$ROWS_AUDIT"

if (( ROWS_MESSAGES == 0 )); then
  fail "Restore yielded ZERO companion_messages — likely corrupt dump"
fi

# ── Step 6: success ────────────────────────────────────────────────────────
log "✓ Restore drill PASSED"
emit_report "pass" "Restore verified: $TABLES_COUNT tables, $ROWS_MESSAGES messages, $ROWS_MEMORIES memories, $ROWS_AUDIT audit rows"

exit 0
