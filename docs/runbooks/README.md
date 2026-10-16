# Operational runbooks

Generated SLO alerts link to explicit anchors in [dependency outage response](dependency-outage.md). The [canonical SLO document](../slos.md) and [operator source/live verification](../../obs/README.md) describe the emitted metric schema.

| Signal | Runbook |
| --- | --- |
| Endpoint availability budget exceeded | [Availability burn](dependency-outage.md#availability-burn) |
| Endpoint p99 objective exceeded | [Latency breach](dependency-outage.md#latency-breach), [diagnosis](high-latency.md#latency-breach) |
| Scrape/counters missing | [Missing telemetry](dependency-outage.md#telemetry-missing) |
| Dependency breaker open | [Dependency circuit](dependency-outage.md#dependency-circuit) |
| PostgreSQL outage | [Database](db-down.md) |
| Redis/session outage | [Redis](redis-down.md) |
| Optional semantic memory outage | [Qdrant](qdrant-down.md) |
| Dominus gateway cold or unavailable | [LLM gateway](lm-studio-cold.md) |
| Dominus voice unavailable | [Voice](voice-unreachable.md) |
| Durable job delivery issue | [Job handoff](durable-job-handoff.md) |
| Migration/rollback issue | [Database migrations](database-migrations.md) |
| Encrypted credential storage and rotation | [Encrypted credentials](encrypted-credentials.md) |

Preserve ownership, session revocation and accepted image job identity during investigation. Keep authentication closed on database failure and session admission closed on Redis failure. Optional outages may leave text available. Never delete conversations, images or memories as a repair, rotate credentials without the owner's authorization, bypass GPU gaming guards or enable an unauthorized provider/paid fallback.

Legacy runbooks remain historical operational context where they describe retired ComfyUI image paths. The current repository AGENTS.md and provider handoffs govern production policy. Generated alert links target the current response anchors above, and `python -m tools.observability.slo_assets --check` validates those targets.
