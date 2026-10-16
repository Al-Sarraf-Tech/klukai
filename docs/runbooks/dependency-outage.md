# Dependency outage and SLO response

This runbook covers the generated SLO alerts. [Canonical objectives](../slos.md) and the [JSON policy](../../docker/core/app/observability/slos.json) define endpoint thresholds and health budgets. Alert windows are measurements; successful rule generation does not establish live ingestion or notification delivery.

<a id="availability-burn"></a>
## Availability burn

`KlukaiSLOBudgetBurn` means both measurement windows exceed the endpoint's normalized availability budget and satisfy the canonical minimum sample count. Read the alert's `method` and `route` labels, which are registered route templates.

1. Query the matching `klukai:slo_burn_rate:5m` and `klukai:slo_burn_rate:1h` series. Check the accompanying request counts; absent series are an evidence gap.
2. Inspect `klukai_circuit_state`, readiness and request/trace correlation. Never include bearer headers, session tokens, prompts or private account credentials in incident notes.
3. For PostgreSQL loss, follow [database recovery](db-down.md). Authentication must remain closed. For Redis loss, follow [session-store recovery](redis-down.md); never substitute another owner's session.
4. Keep text available when optional image, search, voice or memory services fail. Use the existing provider and admission policies; an alert does not authorize an extra inference attempt or a provider change.
5. The release deployer may select the verified compatible prior artifact under its rollout policy. Ordinary rollback never restores over newer writes.

<a id="latency-breach"></a>
## Latency breach

`KlukaiSLOLatency` uses the endpoint p99 target and sample gates from the canonical policy. See [latency diagnosis](high-latency.md#latency-breach). Core HTTP duration ends at response headers; this signal does not measure WebSocket time to first token or full provider completion.

Use the request ID and redacted trace ID to distinguish database pool wait, session persistence, application admission and provider latency. Compare an isolated candidate with its bound baseline before blaming a deployment. Do not edit the baseline or thresholds to clear an alert.

<a id="telemetry-missing"></a>
## Missing telemetry

`KlukaiCoreDown` indicates a failed scrape. `KlukaiTelemetryMissing` indicates absent emitted HTTP counters, including a missing or misconfigured target. Neither is a successful availability sample.

1. Check Prometheus `/api/v1/targets` for `job="klukai-core"`, `health="up"` and an empty `lastError`.
2. Core's private scrape endpoint is `/internal/metrics`; `/api/metrics` remains an admin JSON endpoint. Prometheus must read the separately scoped token file. A missing, unreadable, empty, symlinked, nonregular or group/world-readable core token file fails closed.
3. Check token mount ownership and permissions without printing its contents. A general login/session token cannot substitute for the metrics credential.
4. Verify actual `klukai_http_requests_total`, histogram buckets and recording series using [operator checks](../../obs/README.md#live-acceptance). A configured target or dashboard alone proves no ingestion.
5. Confirm Alertmanager routing with the root operator's approved test route. Do not send an unsolicited page or copy credentials into a command argument.

<a id="dependency-circuit"></a>
## Dependency circuit

`KlukaiDependencyCircuitOpen` reports a measured open breaker. Values are closed `0`, half-open probe `1`, open `2`. Check `klukai_circuit_transitions_total` and `klukai_circuit_rejected_total` for the same bounded dependency label.

A half-open request is the recovery probe. Do not force repeated inference, retry accepted image work, or reset a breaker merely to conceal the outage. Repeated optional-service failures may leave text functioning while the alert remains active.

Production retains Sonnet as the authorized interactive primary, Dominus as its secondary, and Dominus voice with its GPU lease and gaming guards. Agy/Grok account images retain their alternating preference and limited service-error failover; policy refusal is terminal and accepted work must not be resubmitted. No automatic Arc, GPU image or paid API fallback is authorized by an outage. Voice cancellation cleanup may still issue authenticated unload after synthesis trips its breaker; that operation releases work and admits no new inference.

## Verification and closure

After recovery, verify real calls close the breaker, required PostgreSQL/Redis readiness returns healthy, and optional Qdrant loss is reported as degraded. Observe the actual generated SLO series and alert resolution. The disposable backend gate exercises this behavior in isolated resources:

```bash
cd docker/core
KLUKAI_RUNTIME_BACKENDS=1 KLUKAI_RUNTIME_EVIDENCE=<private-evidence-directory> \
  python -m pytest tests/runtime_backends.py -q --no-cov
```

Retain source identity, incident timing, redacted observations and recovery evidence. Preserve conversations, images, memories, passwords and sessions.
