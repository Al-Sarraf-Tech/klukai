# Homelab event bus integration (Klukai)

## Topology (amarillo + dominus-nobara)

```
dominus-nobara ──Tailscale──► GPU services (LM Studio :1234, voice :8301, Comfy via gateway)
amarillo
  companion-core ──Redis PUBLISH──► aichat-redis  companion:events
       ▲                                 │
       │                                 ▼
       │                    companion-events-bridge
       │                                 │
       │                                 ▼ AMQP :5672 (Tailscale 100.111.198.19)
       │                          rabbitmq (rabbitmq-ops)
       │                           exchange: homelab.events (topic)
       │                                 │
       │              ┌──────────────────┼──────────────────┐
       │              ▼                  ▼                  ▼
       │     events-consumer    events-exporter      (future workers)
       │     rabbitmq-ops       rabbitmq-metrics
       │
  infra-postgres / aichat-redis / aichat-vector (Docker networks)
```

## Routing keys produced by Klukai

| Source | Routing key |
|--------|-------------|
| proactive / romance / seasonal | `host.amarillo.companion.<event_type>` |
| GPU lease acquire/release (from core) | `host.amarillo.gpu_lease.<workload>.<action>` |

Broker + docker lifecycle publishers live in `~/git/rabbitmq-ops`.
Metrics/Grafana live in `~/git/rabbitmq-metrics`.

## Ops

```bash
# password must match rabbitmq-ops
grep RABBITMQ_PASSWORD .env

docker compose up -d --build events-bridge
docker logs -f companion-events-bridge
```

Fail-soft: if RabbitMQ is down the bridge reconnects; companion-core keeps
working (Redis path for tgbot is unchanged).
