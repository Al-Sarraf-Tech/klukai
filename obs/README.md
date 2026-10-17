# Klukai observability source and acceptance

Core now emits bounded HTTP RED and dependency breaker metrics at `/internal/metrics`, plus sampled, redacted OTLP traces when its exporter is configured. `/api/metrics` retains the authenticated admin JSON contract. Source files describe the intended collection path; they do not establish that a live collector has loaded, scraped or routed anything.

## Canonical operational assets

`docker/core/app/observability/slos.json` is the sole source of endpoint availability, p99 and rollout sample/hold thresholds. Regenerate and verify from the repository root using the tested Python environment:

```bash
python -m tools.observability.slo_assets --write
python -m tools.observability.slo_assets --check
python -m pytest tests/observability/test_slo_assets.py -q
promtool check rules obs/prometheus/klukai-slo.rules.yml
promtool test rules tests/observability/slo-rules.test.yml
```

`--check` is read-only and fails on missing files, byte drift, invalid policy, missing alert/dashboard runbook anchors or missing datasource UIDs. The Python tool requires the already pinned PyYAML dependency. Missing promtool must fail the CI gate; do not treat a missing executable as a skipped validation.

| Artifact | Use |
| --- | --- |
| `obs/prometheus/klukai-slo.rules.yml` | Deployable generated recordings and alerts |
| `docs/alerts/klukai.yaml` | Identical generated review copy |
| `docs/slos.md` | Generated objective table and measurement semantics |
| `docs/dashboards/overview.json` | Generated route p99/burn panels and measured breaker/scrape state |
| `docs/dashboards/chat-path.json` | Observed LLM duration, request rate and tokens; no invented HTTP chat route |
| `tests/observability/slo-rules.test.yml` | Independent synthetic promtool cases for healthy, absent, low-sample, breached and recovered series |

Generated YAML/JSON are data assets and may exceed source-module line limits. Regenerate them instead of hand editing. Dashboard datasource references use provisioned stable UIDs `prometheus`, `loki` and `tempo`. Preserve existing account credentials and review any live UID migration before applying provisioning.

The emitted counters use `route`, `method`, `status_class`; histograms use milliseconds. Recording series use bounded `route`/`method` labels. The two recording windows are 5m and 1h. Normalized burn greater than 1 means the observed error ratio exceeds that endpoint's canonical error budget; it is not an invented calendar-history claim. Missing input remains absent and insufficient samples suppress endpoint SLO pages. Missing HTTP telemetry and failed scrapes have separate alerts. Every generated alert and endpoint dashboard link resolves to a verified [runbook anchor](../docs/runbooks/dependency-outage.md).

## Private scrape wiring

The root operator owns activation on the existing approved collector. Prometheus must scrape core `/internal/metrics` with `job: klukai-core`, using a dedicated bearer-token file. Core reads `KLUKAI_METRICS_TOKEN_FILE`, default `/run/klukai-secrets/metrics-token`; the separate collector copy must remain private. Core rejects empty, unreadable, nonregular, symlinked or group/world-readable token files. Never use the general admin/session token or put token contents in a shell argument, Git, CI output or incident note.

Activated 2026-10-16. `obs/prometheus/prometheus.yml` scrapes `companion-core:8300/internal/metrics` as `job: klukai-core` over the `klukai-obs` network, with `credentials_file: /etc/prometheus/secrets/metrics-token`. `docker-compose.obs.yml` mounts that file read-only from `${KLUKAI_RUNTIME_SECRETS_DIR:-./.runtime-secrets}/metrics-token` (the same private 0600 file core reads; ignored by Git) and mounts `klukai-slo.rules.yml` under `rule_files`. A disposable Prometheus on that network verified the target `up`, 83 rules healthy with no evaluation errors, and live `klukai_http_requests_total` data before rollout. Rotating the token: write a new 0600 file, then recreate Prometheus; core rereads it on every scrape.

Alerts route to the owner on the bus (owner decision 2026-10-16). Prometheus sends to `klukai-alertmanager` (`obs/alertmanager/alertmanager.yml`), which groups by alert/method/route, repeats every 4h, sends resolutions, and posts to the host bridge `ops/amarillo/scripts/klukai-alert-bridge.py` on the `klukai-obs` gateway `192.168.128.1:19095`. The bridge requires the private bearer token `.runtime-secrets/alert-bridge-token` (0600, outside Git), turns each batch into one `bus send owner@amarillo --note "KLUKAI_ALERT …"`, and answers 502 when the bus send fails so Alertmanager retries. Breaker alerts for the Dominus-backed dependencies (`lm_studio`, `gpu_gateway`, `voice`, `embedding`) go to a silent receiver because that host is powered off by design every night and often by day; every other alert is delivered.

Delivery was proven on 2026-10-17 with a disposable Alertmanager and the bridge: a test alert produced owner bus note `2b4fad1bc8e7` about 30 s later (the group wait).

Activation on amarillo (after the change is on `main`):

```bash
cd ~/git/klukai
install -m 0644 ops/amarillo/systemd/klukai-alert-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now klukai-alert-bridge.service
install -d -m 0750 /mnt/nvmeINT/obs/klukai/alertmanager
docker compose -p klukai -f docker-compose.obs.yml up -d --no-deps alertmanager prometheus
```

If the `klukai-obs` network is ever recreated with a different subnet, update the gateway address in the unit and in `alertmanager.yml`; the bridge refuses to listen on all interfaces.


## Nightly performance history

`.github/workflows/nightly.yml` runs the paired disposable harness on `main`
against its previous commit at 19:37 CDT (18:37 CST), first in a chain of perf,
trivy and mutation on the single heavy runner. If Dominus is off, the job
waits in the queue and its window guard refuses to start unless it can finish
before the 23:30 hard stop; a refused or failed night is an explicit failure,
and a harness failure still writes a `passed: false` summary. Summaries are kept
on the heavy guest under `/opt/klukai-ci/artifacts/nightly/<UTC stamp>-<sha7>/`.

Amarillo's hourly `klukai-ci-relay.timer` pulls new summaries through the
Dominus guest agent into `/mnt/nvmeINT/obs/klukai/perf-nightly/summaries.jsonl`
(one verdict line per night and one line per route), which Alloy ships to Loki
stamped with each night's `finished_at` (so re-reading the file after an Alloy
restart cannot file old nights under today). While Dominus is off the relay
skips quietly; when no summary has arrived for 3 days it sends the owner one bus
note a day. Lines arriving more than 3 h after `finished_at` become queryable
once Loki flushes the chunk. The guest keeps raw evidence bundles 14 days and
every summary indefinitely; CI image tars are kept 7 days. In Grafana:

```logql
avg_over_time({job="klukai-perf-nightly"} | json | kind="route" | unwrap candidate_p99_ms [1d]) by (route)
```

Enable on amarillo after the change is on `main`:

```bash
install -d -m 0750 /mnt/nvmeINT/obs/klukai/perf-nightly   # before Alloy, or Docker creates it root-owned
install -m 0644 ops/amarillo/systemd/klukai-ci-relay.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now klukai-ci-relay.timer
docker compose -p klukai -f docker-compose.obs.yml up -d --no-deps alloy
```

## Live acceptance

Select the actual approved local collector endpoint and retain redacted query JSON with source/artifact identity. The commands below are read-only; executing them and inspecting successful, nonempty results is required. Example endpoint selection is not evidence that this port belongs to the intended collector.

```bash
export PROMETHEUS_URL=http://127.0.0.1:9090
curl -fsS "$PROMETHEUS_URL/api/v1/targets" |
  jq -e '.status == "success" and ([.data.activeTargets[] | select(.labels.job == "klukai-core")] | length > 0 and all(.health == "up" and .lastError == ""))'
curl -fsSG "$PROMETHEUS_URL/api/v1/query" \
  --data-urlencode 'query=klukai_http_requests_total{job="klukai-core"}' |
  jq -e '.status == "success" and (.data.result | length > 0)'
curl -fsSG "$PROMETHEUS_URL/api/v1/query" \
  --data-urlencode 'query=klukai_http_request_duration_ms_bucket{job="klukai-core",route="/health",method="GET"}' |
  jq -e '.status == "success" and (.data.result | length > 0)'
curl -fsSG "$PROMETHEUS_URL/api/v1/query" \
  --data-urlencode 'query=klukai:slo_requests:5m{route="/health",method="GET"}' |
  jq -e '.status == "success" and (.data.result | length > 0)'
curl -fsS "$PROMETHEUS_URL/api/v1/rules" |
  jq -e '.status == "success" and ([.data.groups[] | select(.name == "klukai_slo_alerts")] | length > 0 and all(.[] | .rules[]; .health == "ok"))'
curl -fsS "$PROMETHEUS_URL/api/v1/status/flags" |
  jq -e '.status == "success" and .data["storage.tsdb.retention.time"]'
```

Generate bounded authorized test requests before checking each policy route, including authenticated routes. Verify route-template labels rather than per-user paths, both recording windows, millisecond units, initial no-data behavior and breaker state transitions. A nonempty query alone does not prove correct values. Verify Grafana loads the expected dashboard UIDs and links, and uses the expected datasource UIDs without credential exposure.

Read back effective metrics retention and compare it against `window_days` in the canonical policy. The historical compose scaffold used 15 days, which cannot retain a complete 30-day objective window; since 2026-10-17 Prometheus keeps 35 days (capped at 20 GB; about 15 MB a day, 218 MB for the first 15 days on `/mnt/nvmeINT`). Samples older than 15 days were already deleted, so the first complete 30-day window exists from 2026-11-01. Confirm the existing disk budget and any retention-size cap before changing live retention; no new paid capacity is authorized. New collection does not create historical samples retroactively. Loki's source retention is 30 days and Tempo's source block retention is 7 days; verify effective live configuration and cleanup behavior separately.

Finally, the root operator must verify the existing approved alert route using its authorized test procedure and retain actual delivery/resolution evidence. A healthy rule, promtool test or Alertmanager configuration is not a delivery receipt. The generator and verifier deliberately make no live scrape, retention or routing success claim.

## Isolated rule validation

If promtool is unavailable on the host, the already cached immutable Prometheus image `sha256:4fcecf061b747d0b5dc55bc7efdba26f87768d058d403b5bcc3f0904cf49f973` contains the tested promtool 3.6.0. Run it with no network, a read-only root filesystem, a bounded writable `/tmp`, and read-only mounts of only the generated rule and fixture directories. Do not pull a floating image, mount credentials or mount the Docker socket into a container. The retained local evidence records the exact tool identity, invocation, synthetic results and source hashes.
