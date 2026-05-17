# Runbook: Qdrant Down

**Severity:** P2 (semantic memory recall broken; chat continues with empty episodic context)
**SLO breach:** `/health` flips to `degraded`; `/api/memories/search` returns 5xx; chat returns generic responses (no recalled context).

## Symptom

- `/health` returns `{"status": "degraded", "qdrant": "down"}`
- `/api/memories/search` returns 500
- Chat responses feel "amnesiac" — Klukai doesn't reference past conversations
- companion-core logs: `httpx.ConnectError` or `qdrant healthz failed`

## Immediate action (< 5 min)

1. Confirm Qdrant unreachable:
   ```bash
   docker exec companion-core curl -sf http://aichat-vector:6333/healthz
   ```
2. Check container:
   ```bash
   docker ps --filter name=aichat-vector
   docker logs --tail=200 aichat-vector
   ```
3. If exited: `docker restart aichat-vector`.
4. If up but unhealthy: check collection state:
   ```bash
   curl http://aichat-vector:6333/collections
   ```

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exited, OOM | Vector index too large for RAM | Restart; consider on-disk index |
| Container up, collections missing | Volume not mounted | Inspect `docker inspect aichat-vector` for mount |
| Slow queries | Index not optimized | `POST /collections/{name}/index` to rebuild |
| Disk full | WAL or snapshots accumulated | See [disk-space.md](disk-space.md) |

## Verification after fix

1. `curl http://localhost:8300/api/health/ready` returns 200.
2. Test semantic recall via:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
        http://localhost:8300/api/memories/search?q=test
   ```
3. Send a chat that should trigger a memory recall ("remember when we...")
   and verify Klukai's response mentions the recalled fact.

## CRITICAL: Memory integrity

Per global CLAUDE.md `feedback_never_delete_chat.md`: **vector points are
SACRED**. Recovery actions must NEVER include `qdrant collections delete`.
If Qdrant data is corrupt, restore from offsite tar
(`scripts/restore-from-backup.sh`) — do NOT rebuild from scratch.

## Post-incident

- If Qdrant container restart fixed it, file ticket: add liveness +
  readiness probes specific to Qdrant.
- Run `scripts/audit-memories.sh` (Phase 2) to verify point counts match
  PG memory rows.

## Related

- ADR-0003: Three-tier memory
- ADR-0012: Memory seeding cadence
- `feedback_never_delete_chat.md`
- `app/memory.py` — Qdrant client wrapper
