# Runbook: Memory Leak in companion-core

**Severity:** P2 (RSS climbing over hours; will OOM-kill the container)
**SLO breach:** Once OOM-killed, all endpoints fail until restart.

## Symptom

- companion-core RSS grows steadily over 24h (>50% increase = suspect)
- Grafana panel "Container Memory" shows monotonic upward trend
- `docker stats` shows RSS approaching `mem_limit` (1g per compose)
- Eventually: container exits with `OOMKilled: true`

## Immediate action (< 5 min)

1. Confirm the leak (not just a working-set spike):
   ```bash
   docker stats --no-stream companion-core
   ```
2. Check OOM kill log:
   ```bash
   docker inspect companion-core | jq '.[0].State.OOMKilled'
   dmesg | grep -i "killed process.*companion-core"
   ```
3. Capture diagnostic before restart (lost on restart):
   ```bash
   docker exec companion-core ps auxf > /tmp/companion-core-procs.txt
   docker exec companion-core sh -c 'cat /proc/1/status' > /tmp/companion-core-status.txt
   ```
4. Restart to clear the leak:
   ```bash
   docker compose restart companion-core
   ```
5. Verify recovery:
   ```bash
   curl -sf http://localhost:8300/health
   ```

## Root-cause investigation (post-restart)

Memory leaks in async Python are usually one of:

| Cause | Detection | Fix |
|---|---|---|
| WebSocket connections not closed | Count rises in `/api/metrics` | Audit `ws_manager.py` cleanup |
| asyncio task pile-up | `asyncio.all_tasks()` grows unboundedly | Audit background tasks for awaits |
| HTTP client connection pool leak | httpx clients not closed | `async with` everywhere |
| Cache without eviction | Dict grows monotonically | LRU + TTL on caches |
| psycopg connection leak | Pool waiting count >0 always | Audit `get_conn()` usage |

Tools:
```bash
# Snapshot heap (Phase 4: scheduled snapshots)
docker exec companion-core python3 -c "
import tracemalloc; tracemalloc.start()
# ... let it run, snapshot, compare
"

# Process state continuously
watch -n 5 'docker stats --no-stream companion-core'
```

## Verification after fix

1. RSS stable over 1h post-fix.
2. Run extended chat session; verify no growth pattern.
3. Add regression test if a specific code path was the leak.

## Long-term mitigation

- Phase 4: scheduled tracemalloc snapshots every 6h; diff and alert on growth.
- Phase 2: ratchet `mem_limit` only after confirming leak is fixed, not as a workaround.

## Related

- `docker-compose.yml` — `deploy.resources.limits.memory: 1g`
- `app/ws_manager.py` — connection lifecycle
- `app/background.py` — background tasks
- `app/db.py` — psycopg pool
