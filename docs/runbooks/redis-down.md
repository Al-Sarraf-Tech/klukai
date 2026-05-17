# Runbook: Redis Down

**Severity:** P2 (sessions reset, rate-limit bypassed, chat degraded)
**SLO breach:** `/health` flips to `degraded`; session loss = users re-login.
**Related SLOs:** `docs/slos.md` → "Chat round-trip availability 99.9%"

## Symptom

- `/health` returns `{"status": "degraded", "redis": "down"}`
- `companion-core` logs: `redis.exceptions.ConnectionError`
- Users report being logged out
- Rate limiter falls open (no Redis backend) — visible as missing rate-limit headers

## Immediate action (< 5 min)

1. Confirm Redis is the problem:
   ```bash
   docker exec companion-core redis-cli -h aichat-redis ping
   ```
   Should return `PONG`. Failure confirms.
2. Check Redis container:
   ```bash
   docker ps --filter name=aichat-redis
   docker logs --tail=200 aichat-redis
   ```
3. If exited: `docker restart aichat-redis`.
4. If up but unresponsive: check memory pressure:
   ```bash
   docker exec aichat-redis redis-cli INFO memory | grep used_memory_human
   ```

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exited, OOM | Reached `maxmemory` cap | Restart; review eviction policy |
| Container up, slow | Persistence flush (RDB/AOF) | Check `redis-cli LATENCY HISTORY` |
| Connection refused only from core | Docker network desync | Restart `companion-core`; verify network alias |

## Verification after fix

1. `curl http://localhost:8300/api/health/ready` returns 200.
2. Log in as a seed user; verify session persists across page refresh.
3. Verify rate limit returns `X-RateLimit-Remaining` header.

## Graceful degradation note

Phase 4 circuit breaker on Redis allows companion-core to stay up with
sessions disabled. Until then, Redis down = users logged out. **Klukai
chat memory is NOT lost** (chat persists in PG; only session is in Redis).
Re-login restores everything.

## Post-incident

- If Redis OOM'd, file ticket: add `maxmemory` cap + LRU eviction.
- Update this runbook if recovery steps missed anything.

## Related

- ADR-0003: Three-tier memory
- `app/rate_limit.py` — Redis-backed rate limiter
- `app/circuit_breakers.py` (Phase 4)
