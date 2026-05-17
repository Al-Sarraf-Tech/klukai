# Runbook: PostgreSQL Down

**Severity:** P1 (chat unavailable, memory writes blocked)
**SLO breach:** `/health` flips to `unhealthy`; `/api/chat/*` returns 5xx; `/api/audit/*` blocked.
**Related SLOs:** `docs/slos.md` → "Chat round-trip availability 99.9%"

## Symptom

- `/health` returns `{"status": "unhealthy", "database": {"status": "down"}}`
- `companion-core` logs: `psycopg.OperationalError: connection refused` or `pool timeout`
- Chat returns 500 with `error_code: DB_UNAVAILABLE`
- Grafana panel "DB connection pool" shows 0 available connections or saturation

## Immediate action (< 5 min)

1. Confirm PG is the problem, not networking:
   ```bash
   docker exec companion-core sh -c 'pg_isready -h infra-postgres -p 5432 -U aichat'
   ```
   If `accepting connections` → not actually down, see [high-latency.md](high-latency.md)
2. Check PG container state:
   ```bash
   docker ps --filter name=infra-postgres
   docker logs --tail=200 infra-postgres
   ```
3. If container exited: `docker restart infra-postgres` and watch logs.
4. If container is up but rejecting: check connection count vs `max_connections`:
   ```bash
   docker exec infra-postgres psql -U aichat -c "SELECT count(*) FROM pg_stat_activity;"
   docker exec infra-postgres psql -U aichat -c "SHOW max_connections;"
   ```

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exited, OOM in dmesg | Memory exhaustion | Restart; review Phase 2 memory limits |
| Container up, `too many connections` | Pool leak in companion-core | Restart core; investigate `app/db.py` pool retry path |
| Container up, slow queries | Bloated indexes, long-running tx | `pg_stat_activity` → kill long-running queries; vacuum |
| Disk full on `/mnt/nvmeINT` | WAL accumulation | See [disk-space.md](disk-space.md) |

## Verification after fix

1. `curl http://localhost:8300/api/health/ready` returns 200.
2. Send a test chat turn; verify it persists by querying the most recent
   `companion_messages` row.
3. Run `make perf-baseline` and confirm no regression.

## Post-incident

- File a PR adding the trigger to `tests/integration/test_db_resilience.py`.
- If PG was the actual root cause, file a ticket to upgrade resource limits
  in `docker-compose.yml`.
- Update this runbook if the steps above missed anything.

## Related

- ADR-0003: Three-tier memory (Redis → Qdrant → PG)
- `app/db.py` — connection pool + retry logic
- `app/circuit_breakers.py` (Phase 4) — PG circuit breaker spec
