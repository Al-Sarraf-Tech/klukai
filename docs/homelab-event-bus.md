# Homelab event bus (Klukai)

## Overview

Klukai publishes lifecycle signals onto the shared homelab bus so ops tooling (metrics, notifiers, future workers) can observe companion and GPU-lease activity without knowing Redis.

**Redaction:** broker host addresses and passwords are not documented here. They live in core-host `.env` (`RABBIT_HOST`, `RABBIT_PORT`, `RABBITMQ_PASSWORD` / bridge `RABBIT_PASS`).

## Topology

```
companion-core
    │  Redis PUBLISH  companion:events
    ▼
aichat-redis (or equivalent)
    │  SUBSCRIBE
    ▼
events-bridge
    │  AMQP
    ▼
RabbitMQ
    ├── exchange homelab.events (topic)     → metrics / consumers
    ├── exchange klukai.defer  (direct)     → TTL bucket queues
    ├── exchange klukai.due    (direct)     → due.tasks
    └── queue    klukai.jobs.her_pov (quorum)
```

Core still speaks **no AMQP**. The bridge is the only AMQP citizen for this app.

## Routing keys (companion → bus)

| Domain | Pattern |
|---|---|
| Companion lifecycle | `host.<hostname>.companion.<type>` |
| GPU lease fan-out | `host.<hostname>.gpu_lease.<workload>.<action>` |

`<hostname>` is the bridge `LOCAL_HOSTNAME` (short host label).

## Deferred rail

Fixed delay buckets (seconds): `10, 60, 300, 900, 3600, 21600, 86400`.

- Per-**queue** TTL (not per-message) avoids head-of-line blocking.
- Longer waits hop buckets.
- Bridge idle loop: heartbeat + `drain_due` + `drain_jobs`.

Postgres row is written **before** arming. Broker loss delays delivery; the core sweeper is the backstop.

## Her POV work queue

| Property | Value |
|---|---|
| Queue | `klukai.jobs.her_pov` |
| Type | **quorum** |
| Body | `{ "job_id", "kind": "her_pov" }` |
| Consumer | bridge `drain_jobs` → HTTP run endpoint |
| Semantics | hold-until-done ≈ single-flight GPU lease |

## Ops

```bash
# core host
docker compose up -d --build events-bridge
docker logs -f companion-events-bridge

# expect lines like:
#   bridge: subscribed companion:events ...
#   bridge: armed defer task …
#   bridge: enqueued her_pov job …
#   bridge: running her_pov job … / done
```

Fail-soft: bridge reconnect loops; core keeps serving chat if the bus is down.

## Related

- `docker/events-bridge/bridge.py`
- `docker/core/app/deferred.py`
- `docker/core/app/memory_her_pov.py`
- `docs/architecture.md`
