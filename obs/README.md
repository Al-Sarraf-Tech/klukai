# Observability stack — operator notes

Per S+ uplift spec §5.2. Five-container observability stack
(Alloy + Prometheus + Loki + Tempo + Grafana) for klukai.

## Quick start

```bash
# Initial setup (one-time)
sudo mkdir -p /mnt/nvmeINT/obs/klukai/{prometheus,loki,tempo,grafana,alloy}
sudo chown -R 1000:1000 /mnt/nvmeINT/obs/klukai

# Bring up the stack
docker compose -f docker-compose.obs.yml up -d

# Check all five containers are healthy
docker compose -f docker-compose.obs.yml ps

# Grafana at http://localhost:3000  (default: admin/admin → set new pw)
```

## Layout

| Container | Loopback port | Purpose | Storage |
|---|---|---|---|
| `klukai-alloy` | 12345 (UI), 4317 (OTLP gRPC), 4318 (OTLP HTTP) | Collector + fan-out | `/mnt/nvmeINT/obs/klukai/alloy` |
| `klukai-prometheus` | 9090 | Metrics TSDB (15d) | `/mnt/nvmeINT/obs/klukai/prometheus` |
| `klukai-loki` | 3100 | Log aggregator (30d) | `/mnt/nvmeINT/obs/klukai/loki` |
| `klukai-tempo` | 3200 | Trace store (7d) | `/mnt/nvmeINT/obs/klukai/tempo` |
| `klukai-grafana` | 3000 | UI | `/mnt/nvmeINT/obs/klukai/grafana` |

All bound to 127.0.0.1 — access via Tailscale or LAN SSH tunnel only.
NEVER expose to Cloudflare; Grafana auth is rudimentary and not
internet-grade.

## Provisioned datasources

Grafana auto-wires Prometheus + Loki + Tempo on first start (see
`obs/grafana/provisioning/datasources/datasources.yml`). Trace ↔
log correlation is configured: Loki log lines with `trace_id=...`
become clickable links into Tempo.

## Dashboards-as-code

Drop JSON dashboards in `docs/dashboards/` (mounted at
`/var/lib/grafana/dashboards`). Grafana picks them up within 60s.

Initial dashboards (Phase 2 spec §6.5):
- `overview.json` — TBD: per-endpoint RED panel
- `chat-path.json` — TBD: LM Studio latency, mood breakdown
- `memory-pipeline.json` — TBD: Qdrant search latency, recall rate
- `voice-image-gen.json` — TBD: dominus GPU health

These get authored as klukai's metrics surface stabilizes.

## Next-phase wiring (Phase 3.2)

This compose file lands the **infrastructure**. The **instrumentation**
in `companion-core` follows:

1. Add `opentelemetry-sdk` + `opentelemetry-exporter-otlp` to
   `docker/core/requirements.txt`.
2. Initialize OTel SDK in `app/observability/tracing.py`
   (`OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317`).
3. Wrap FastAPI middleware: span per request, attributes for
   route + status + user_id.
4. Switch `app/observability/__init__.py:structured_log` to emit
   `trace_id` in every line so Loki↔Tempo correlation works.
5. Wire Prometheus scrape on `/api/metrics` (uncomment in
   `obs/prometheus/prometheus.yml`).

## Alerts

Alert rules live in `docs/alerts/klukai.yaml` (Phase 3 spec §7).
Each rule has `runbook_url` annotation pointing into
`docs/runbooks/*.md` anchors.

Currently Prometheus has no `alertmanager_url` set —
Alertmanager is Phase 4 work. Alerts evaluate but don't fire
notifications yet.

## Backup

Add `/mnt/nvmeINT/obs/klukai/` to amarillo's backup rsync if
historical metrics/logs matter. By default they don't (replaceable
data — re-scrape and refill).

## Troubleshooting

- **Alloy refusing OTLP**: check `docker logs klukai-alloy` for
  config parse errors. Common cause: typo in `obs/alloy/config.alloy`.
- **Grafana shows no data**: verify Alloy is healthy → check that
  companion-core actually pushed something (Phase 3.2 wiring not yet
  in place).
- **TSDB OOM**: tune Prometheus `--storage.tsdb.retention.size` if
  retention.time alone isn't enough.
