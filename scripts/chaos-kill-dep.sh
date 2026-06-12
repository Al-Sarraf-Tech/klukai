#!/usr/bin/env bash
# scripts/chaos-kill-dep.sh — chaos drill harness for klukai.
#
# Kills a named dependency, waits, restores, and captures impact + recovery
# time. Per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §7.5.
#
# Supported deps: postgres, redis, qdrant, voice, comfyui, lm_studio.
#
# Usage:
#   scripts/chaos-kill-dep.sh <dep> [hold_seconds] [--dry-run]
#
# Output:
#   docs/chaos-drills/YYYY-MM-DD-<dep>.md   — drill report
#   docs/chaos-drills/YYYY-MM-DD-<dep>.json — machine-readable
#
# Exit codes:
#   0  drill completed; dep recovered within budget
#   1  dep failed to recover within budget
#   2  invalid input / missing tooling
#   3  user requested dry-run (no kill)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

DEP="${1:-}"
HOLD="${2:-30}"
DRY_RUN=0
[[ "${3:-}" == "--dry-run" ]] && DRY_RUN=1

# Real production container names (these are SHARED aichat infra on amarillo,
# NOT klukai-owned compose services — see docker-compose.yml `external` nets):
#   postgres → infra-postgres        (SHARED: kairi + aichat live here too!)
#   redis    → aichat-aichat-redis-1 (network alias aichat-redis)
#   qdrant   → aichat-aichat-vector-1 (alias aichat-vector; kairi_memory too)
#   voice    → companion-voice on dominus
# Override with CHAOS_CONTAINER for non-default layouts.
case "$DEP" in
  postgres) HOST=amarillo CONTAINER="${CHAOS_CONTAINER:-infra-postgres}" ;;
  redis)    HOST=amarillo CONTAINER="${CHAOS_CONTAINER:-aichat-aichat-redis-1}" ;;
  qdrant)   HOST=amarillo CONTAINER="${CHAOS_CONTAINER:-aichat-aichat-vector-1}" ;;
  voice)    HOST=dominus  CONTAINER="${CHAOS_CONTAINER:-companion-voice}" ;;
  comfyui|lm_studio)
    HOST=dominus
    if [[ -z "${CHAOS_CONTAINER:-}" ]]; then
      echo "ERROR: ${DEP} container name on dominus is not standardized — set CHAOS_CONTAINER" >&2
      exit 2
    fi
    CONTAINER="$CHAOS_CONTAINER" ;;
  *) echo "usage: $0 <postgres|redis|qdrant|voice|comfyui|lm_studio> [hold_seconds] [--dry-run]" >&2; exit 2 ;;
esac

if [[ "$DEP" == "postgres" || "$DEP" == "redis" || "$DEP" == "qdrant" ]]; then
  cat >&2 <<WARN
╔════════════════════════════════════════════════════════════════════════╗
║  WARNING: '${CONTAINER}' is SHARED infrastructure.
║  Stopping it takes down klukai AND kairi AND the rest of the aichat
║  stack for the full hold window (${HOLD}s). Keep the hold SHORT.
╚════════════════════════════════════════════════════════════════════════╝
WARN
fi

OUT_DIR="${REPO_ROOT}/docs/chaos-drills"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y-%m-%d-%H%M%S)"
MD="${OUT_DIR}/${STAMP}-${DEP}.md"
JSON="${OUT_DIR}/${STAMP}-${DEP}.json"

start_ts="$(date -u +%s)"

# Pre-drill: capture baseline health.
baseline_ok=$(curl -s -m 3 http://localhost:8300/health 2>/dev/null | jq -r '.status' 2>/dev/null || echo "unknown")
echo "Pre-drill /health status: $baseline_ok"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY-RUN — would kill $CONTAINER on $HOST and hold $HOLD s"
  exit 3
fi

# Kill.
echo "Killing $CONTAINER on $HOST..."
kill_ts="$(date -u +%s)"
if [[ "$HOST" == "amarillo" ]]; then
  docker stop "$CONTAINER" >/dev/null
else
  ssh "$HOST" "docker stop $CONTAINER" >/dev/null
fi

# Sample during hold window.
sleep "$HOLD"
midpoint_status=$(curl -s -m 3 http://localhost:8300/health 2>/dev/null | jq -r '.status' 2>/dev/null || echo "unreachable")
echo "Mid-drill /health status: $midpoint_status"

# Restore.
restore_start="$(date -u +%s)"
echo "Restoring $CONTAINER on $HOST..."
if [[ "$HOST" == "amarillo" ]]; then
  docker start "$CONTAINER" >/dev/null
else
  ssh "$HOST" "docker start $CONTAINER" >/dev/null
fi

# Poll for full recovery (klukai /health back to "ok").
budget=120
recovery_time=0
for _ in $(seq 1 $budget); do
  status=$(curl -s -m 2 http://localhost:8300/health 2>/dev/null | jq -r '.status' 2>/dev/null || echo "")
  if [[ "$status" == "ok" ]]; then
    recovery_time=$(($(date -u +%s) - restore_start))
    break
  fi
  sleep 1
done

end_ts="$(date -u +%s)"
total_outage_s=$((restore_start - kill_ts + recovery_time))

# Report.
cat > "$MD" <<MD_EOF
# Chaos drill — ${DEP} — ${STAMP}

- **Dep killed:** \`${CONTAINER}\` on ${HOST}
- **Hold duration:** ${HOLD}s
- **Recovery time after restore:** ${recovery_time}s
- **Total outage window:** ${total_outage_s}s
- **Pre-drill /health:** ${baseline_ok}
- **Mid-drill /health:** ${midpoint_status}
- **Post-drill /health:** $([ "$recovery_time" -lt "$budget" ] && echo ok || echo failed_to_recover)

## What we expected

- Circuit breaker for \`${DEP}\` should open within 30s of kill.
- klukai should serve degraded responses (text-only for voice, empty memory for qdrant, etc.).
- Recovery should be automatic on dep return.

## Observations

(Capture Grafana screenshot of impact + recovery period and reference it here.)

## Follow-ups

- [ ] Verify circuit breaker actually opened (Prom: \`klukai_circuit_state{dep="${DEP}"} == 2\`)
- [ ] Verify recovery alerted on Grafana
- [ ] Update runbook \`docs/runbooks/${DEP}-down.md\` with anything learned
MD_EOF

cat > "$JSON" <<JSON_EOF
{
  "drill_date": "${STAMP}",
  "dep": "${DEP}",
  "container": "${CONTAINER}",
  "host": "${HOST}",
  "hold_seconds": ${HOLD},
  "recovery_seconds": ${recovery_time},
  "total_outage_seconds": ${total_outage_s},
  "pre_health": "${baseline_ok}",
  "mid_health": "${midpoint_status}",
  "recovered": $([ "$recovery_time" -gt 0 ] && [ "$recovery_time" -lt "$budget" ] && echo true || echo false)
}
JSON_EOF

echo "Drill report: $MD"
echo "JSON: $JSON"

# Exit nonzero if dep didn't recover in budget.
[[ "$recovery_time" -gt 0 ]] && [[ "$recovery_time" -lt "$budget" ]] || exit 1
