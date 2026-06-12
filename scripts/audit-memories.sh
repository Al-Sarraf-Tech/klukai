#!/usr/bin/env bash
# audit-memories.sh — memory integrity check across PG + Qdrant.
#
# Per S+ uplift spec §4 Q12: "Read-only audit: scripts/audit-memories.sh
# counts Qdrant points, PG memory rows, Redis sessions; alerts on >5% drop".
#
# Per global CLAUDE.md feedback_never_delete_chat.md: chat messages,
# episodes, affection, Qdrant vectors are SACRED. This script ONLY READS.
#
# Scheduled hourly via systemd timer. Maintains a rolling baseline
# in /mnt/nvmeINT/logs/memory-integrity-baseline.json — alerts if the
# current count drops >5% vs the baseline.
#
# Exits:
#   0 — counts OK or growing
#   1 — count dropped >5% (likely data loss)
#   2 — required service unreachable

set -euo pipefail

LOG_DIR="${LOG_DIR:-/mnt/nvmeINT/logs}"
BASELINE_FILE="${BASELINE_FILE:-$LOG_DIR/memory-integrity-baseline.json}"
THRESHOLD_PCT="${THRESHOLD_PCT:-5}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/memory-integrity.log"

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }
fail() { log "FAIL: $*"; exit 1; }

# ── Counts ─────────────────────────────────────────────────────────────────
log "Counting PG memory rows..."

PG_MESSAGES=$(docker exec infra-postgres psql -U aichat -d aichat -tAc \
  "SELECT count(*) FROM companion_messages" 2>/dev/null | tr -d ' ' || echo "ERR")
PG_MEMORIES=$(docker exec infra-postgres psql -U aichat -d aichat -tAc \
  "SELECT count(*) FROM companion_memories WHERE kept=true" 2>/dev/null | tr -d ' ' || echo "ERR")
PG_AUDIT=$(docker exec infra-postgres psql -U aichat -d aichat -tAc \
  "SELECT count(*) FROM companion_audit_log" 2>/dev/null | tr -d ' ' || echo "ERR")

if [[ "$PG_MESSAGES" == "ERR" || "$PG_MEMORIES" == "ERR" ]]; then
  log "PG unreachable"; exit 2
fi

log "Counting Qdrant points..."
# aichat-vector is container DNS — unresolvable from the host where this timer
# runs. Qdrant is host-published at localhost:6333 on amarillo (16333 via
# tailscale). Override with QDRANT_URL if probing remotely.
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

# A probe failure must NEVER read as "0 points" — silent zeros would defeat
# the SACRED >5%-drop alarm. Any curl/parse failure is a loud exit 2, same as
# the PG branch. Collections per docker/core/app/memory.py.
qdrant_count() {
  local coll="$1" count
  count=$(curl -sf "${QDRANT_URL}/collections/${coll}" | grep -oP '"points_count":\K[0-9]+') || {
    log "Qdrant unreachable or collection '${coll}' missing at ${QDRANT_URL}"
    exit 2
  }
  echo "$count"
}

QDRANT_EPISODES=$(qdrant_count companion_episodes)
QDRANT_EXCHANGES=$(qdrant_count companion_exchanges)

log "Counting Redis sessions..."
# Redis container is aichat-aichat-redis-1 (compose name; 'aichat-redis' is
# only a network alias, not a docker name). Resolve dynamically as fallback.
REDIS_CONTAINER="${REDIS_CONTAINER:-aichat-aichat-redis-1}"
if ! docker ps --format '{{.Names}}' | grep -qx "$REDIS_CONTAINER"; then
  REDIS_CONTAINER=$(docker ps --filter "name=aichat-redis" --format '{{.Names}}' | head -1)
fi
if [[ -z "$REDIS_CONTAINER" ]] \
   || ! docker exec "$REDIS_CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG; then
  log "Redis unreachable (container: ${REDIS_CONTAINER:-not found})"
  exit 2
fi
REDIS_SESSIONS=$(docker exec "$REDIS_CONTAINER" redis-cli --scan --pattern 'session:*' | wc -l) || {
  log "Redis session scan failed on ${REDIS_CONTAINER}"
  exit 2
}

CURRENT=$(cat <<EOF
{
  "ran_at": "$(date -Iseconds)",
  "pg_messages": $PG_MESSAGES,
  "pg_memories": $PG_MEMORIES,
  "pg_audit": $PG_AUDIT,
  "qdrant_episodes": $QDRANT_EPISODES,
  "qdrant_exchanges": $QDRANT_EXCHANGES,
  "redis_sessions": $REDIS_SESSIONS
}
EOF
)

log "Current counts: messages=$PG_MESSAGES memories=$PG_MEMORIES audit=$PG_AUDIT episodes=$QDRANT_EPISODES exchanges=$QDRANT_EXCHANGES sessions=$REDIS_SESSIONS"

# ── Compare against baseline ────────────────────────────────────────────────
if [[ ! -f "$BASELINE_FILE" ]]; then
  log "No baseline yet — writing initial baseline."
  echo "$CURRENT" > "$BASELINE_FILE"
  exit 0
fi

BASE_MESSAGES=$(grep -oP '"pg_messages":\s*\K[0-9]+' "$BASELINE_FILE" || echo "0")
BASE_MEMORIES=$(grep -oP '"pg_memories":\s*\K[0-9]+' "$BASELINE_FILE" || echo "0")
BASE_EPISODES=$(grep -oP '"qdrant_episodes":\s*\K[0-9]+' "$BASELINE_FILE" || echo "0")
BASE_EXCHANGES=$(grep -oP '"qdrant_exchanges":\s*\K[0-9]+' "$BASELINE_FILE" || echo "0")

check_drop() {
  local name="$1" current="$2" baseline="$3"
  if (( baseline == 0 )); then return; fi  # No baseline data, no comparison

  local drop_pct=$(( (baseline - current) * 100 / baseline ))
  if (( drop_pct > THRESHOLD_PCT )); then
    log "ALERT: $name dropped ${drop_pct}% (baseline=$baseline, current=$current)"
    return 1
  fi
  return 0
}

DROPS=0
check_drop "PG messages"   "$PG_MESSAGES"   "$BASE_MESSAGES"   || ((DROPS++))
check_drop "PG memories"   "$PG_MEMORIES"   "$BASE_MEMORIES"   || ((DROPS++))
check_drop "Qdrant episodes" "$QDRANT_EPISODES" "$BASE_EPISODES" || ((DROPS++))
check_drop "Qdrant exchanges" "$QDRANT_EXCHANGES" "$BASE_EXCHANGES" || ((DROPS++))

# Update baseline with the higher value per field (counts should monotonically grow)
NEW_BASELINE_MESSAGES=$(( PG_MESSAGES > BASE_MESSAGES ? PG_MESSAGES : BASE_MESSAGES ))
NEW_BASELINE_MEMORIES=$(( PG_MEMORIES > BASE_MEMORIES ? PG_MEMORIES : BASE_MEMORIES ))
NEW_BASELINE_EPISODES=$(( QDRANT_EPISODES > BASE_EPISODES ? QDRANT_EPISODES : BASE_EPISODES ))
NEW_BASELINE_EXCHANGES=$(( QDRANT_EXCHANGES > BASE_EXCHANGES ? QDRANT_EXCHANGES : BASE_EXCHANGES ))

cat > "$BASELINE_FILE" <<EOF
{
  "updated_at": "$(date -Iseconds)",
  "pg_messages": $NEW_BASELINE_MESSAGES,
  "pg_memories": $NEW_BASELINE_MEMORIES,
  "qdrant_episodes": $NEW_BASELINE_EPISODES,
  "qdrant_exchanges": $NEW_BASELINE_EXCHANGES
}
EOF

if (( DROPS > 0 )); then
  log "FAIL: $DROPS counters dropped beyond ${THRESHOLD_PCT}% threshold"
  log "      Investigate per docs/runbooks/qdrant-down.md or docs/runbooks/db-down.md"
  exit 1
fi

log "✓ All memory integrity counts within threshold (or growing)"
exit 0
