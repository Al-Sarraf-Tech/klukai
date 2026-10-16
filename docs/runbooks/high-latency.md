# HTTP latency diagnosis

<a id="latency-breach"></a>
## Latency breach

`KlukaiSLOLatency` compares both generated p99 windows against the [canonical endpoint objectives](../slos.md). Its `route` and `method` labels identify the served route template. Inspect the request-count recordings before interpreting an endpoint with little traffic.

Core HTTP duration measures handler time through response headers. Diagnose WebSocket replies and provider completion separately using the observed LLM dashboard and sampled redacted traces; those observations have no invented HTTP route SLO.

1. Open **Klukai SLO Overview**, locate the alert's route, and compare p99, normalized burn and request counts.
2. Query the corresponding recordings and correlate the request ID with a redacted trace. `NO DATA` is missing evidence, never zero latency.
3. Check database pool saturation and query timing, Redis/session availability, application admission and dependency circuit state. Use [dependency response](dependency-outage.md#dependency-circuit), [database](db-down.md), [Redis](redis-down.md) or [Qdrant](qdrant-down.md) as appropriate.
4. For LLM cold starts, respect the existing [gateway/GPU procedure](../../docs/handoff/2026-09-25-dominus-tailscale-publish.md). Do not repair or restart the Dominus GPU stack while its game-active marker exists.
5. Compare the isolated candidate and bound baseline. A compatibility rollback selects prior code and artifacts without reverting newer data.

Read-only Prometheus examples, with the operator-selected local endpoint:

```bash
curl -fsSG "$PROMETHEUS_URL/api/v1/query" \
  --data-urlencode 'query=klukai:slo_p99_ms:5m{route="/health",method="GET"}'
curl -fsSG "$PROMETHEUS_URL/api/v1/query" \
  --data-urlencode 'query=klukai:slo_requests:5m{route="/health",method="GET"}'
```

Require a successful nonempty query response. Check both measurement windows and actual alert resolution after a fix. Add a regression tied to the cause, preserve the evidence, and review threshold changes through the canonical policy instead of changing generated files.
