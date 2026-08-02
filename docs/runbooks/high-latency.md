# Runbook: High Latency (SLO Breach)

**Severity:** P2 (SLO breach detected by burn-rate alert)
**SLO breach:** Endpoint-specific; see `docs/slos.md` for targets.

## Symptom

- Burn-rate alert fired in Grafana ("14d budget burned in 1h")
- p99 latency on a key endpoint exceeds target
- User reports "chat feels slow"
- companion-core logs show many `slow_query` warnings

## Immediate action (< 10 min)

1. Identify which endpoint is breaching:
   - Open Grafana dashboard "klukai → chat-path".
   - Look at p99 panel by route.
   - Note the slow endpoint.
2. Check subsystem latency:
   - Database panel: pool saturation? Slow queries?
   - Redis panel: command latency?
   - Qdrant panel: search latency?
   - Compatibility gateway and llama.cpp router: response time and cold-load state?
3. Triage by endpoint:

| Endpoint slow | Likely culprit | Next runbook |
|---|---|---|
| `/api/chat/*` | llama.cpp cold start, lease wait, or VRAM pressure | [lm-studio-cold.md](lm-studio-cold.md) |
| `/api/memories/search` | Qdrant degraded | [qdrant-down.md](qdrant-down.md) |
| `/api/user/stats` | PG slow | [db-down.md](db-down.md) |
| `/health` | Cache miss path | Check `health_cache.py` TTL config |
| All endpoints | companion-core itself | [memory-leak.md](memory-leak.md) |

## Investigation tools

```bash
# Per-endpoint p99 from /api/metrics
curl -s http://localhost:8300/api/metrics | grep "http_request_duration_ms_bucket"

# Slow queries logged
docker logs companion-core --since=1h | grep slow_query

# DB pool state
docker exec companion-core curl -sf http://localhost:8300/api/health/subsystems | jq .database

# Top requests by latency (Phase 2 metric)
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=topk(5, http_request_duration_ms_p99{job="klukai"})'
```

## Root-cause investigation

| Pattern | Likely cause |
|---|---|
| Latency rises gradually over hours | Memory leak or DB bloat |
| Latency spikes during peak hours | Load > capacity; scale or queue |
| Latency spike correlated with image gen | Expected bounded lease wait or a failed GPU cleanup |
| One endpoint slow, others fine | Endpoint-specific code regression |
| All endpoints slow | companion-core itself (GC, leak, lock contention) |

## Verification after fix

1. Run `make perf-baseline` and confirm p99 within target.
2. Watch Grafana burn-rate panel; alert clears within one alert window.
3. Send 10 chat turns; verify p99 within SLO.

## Post-incident

- If the breach was real (not test/false alarm), file PR adding the
  scenario to `tests/perf/` so regression is caught at PR-time.
- Update SLO doc if the target was unrealistic.
- Add new dashboard panel if the missing visibility delayed RCA.

## Related

- `docs/slos.md` — per-endpoint targets
- `docs/perf-baseline.md` — current measured baseline
- `tools/load-test/probe.py` — re-measure tool
- Grafana dashboard: `docs/dashboards/chat-path.json` (Phase 2)
