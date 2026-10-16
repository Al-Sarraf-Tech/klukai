"""Pure canonical-policy rendering for Prometheus, Grafana and documentation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

import yaml

POLICY = "docker/core/app/observability/slos.json"
RULES = "obs/prometheus/klukai-slo.rules.yml"
RUNBOOK_BASE = "https://github.com/Al-Sarraf-Tech/klukai/blob/main/docs/runbooks/"
WINDOWS = ("5m", "1h")


@dataclass(frozen=True)
class Policy:
    data: dict[str, Any]
    digest: str

    @property
    def endpoints(self) -> list[dict[str, Any]]:
        endpoints: list[dict[str, Any]] = self.data["endpoints"]
        return endpoints

    @property
    def minimum(self) -> int:
        return int(self.data["rollback"]["minimum_requests"])

    @property
    def hold(self) -> str:
        rollback = self.data["rollback"]
        return f"{rollback['consecutive_breaches'] * rollback['poll_seconds']}s"


def load(root: Path) -> Policy:
    raw = (root / POLICY).read_bytes()
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or type(data.get("window_days")) is not int
        or data["window_days"] < 1
    ):
        raise ValueError("Invalid canonical SLO policy version/window")
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError("Empty SLO endpoints")
    seen: set[tuple[str, str]] = set()
    for item in endpoints:
        identity = _validate_endpoint(item)
        if identity in seen:
            raise ValueError("Duplicate SLO endpoint")
        seen.add(identity)
    rollback = data.get("rollback")
    if not isinstance(rollback, dict) or any(
        type(rollback.get(key)) is not int or rollback[key] < 1
        for key in ("minimum_requests", "consecutive_breaches", "poll_seconds")
    ):
        raise ValueError("Invalid rollback health budget")
    return Policy(data, hashlib.sha256(raw).hexdigest())


def _validate_endpoint(item: object) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise ValueError("Invalid SLO endpoint")
    for field in ("availability", "p99_ms"):
        value = item.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid SLO numeric objective")
    if not 0 < item["availability"] < 1 or item["p99_ms"] <= 0:
        raise ValueError("Invalid SLO objective bounds")
    if not isinstance(item.get("method"), str) or item.get("method") not in {
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "HEAD",
        "OPTIONS",
    }:
        raise ValueError("Invalid SLO method")
    route, runbook = item.get("route"), item.get("runbook")
    if not isinstance(route, str) or not re.fullmatch(r"/[A-Za-z0-9_/{.}-]*", route):
        raise ValueError("Invalid SLO route template")
    if not isinstance(runbook, str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", runbook
    ):
        raise ValueError("Invalid SLO runbook slug")
    return item["method"], route


def selector(endpoint: dict[str, Any], job: bool = False) -> str:
    labels = {"method": endpoint["method"], "route": endpoint["route"]}
    if job:
        labels["job"] = "klukai-core"
    return ",".join(
        f"{key}={json.dumps(value)}" for key, value in sorted(labels.items())
    )


def budget(endpoint: dict[str, Any]) -> str:
    return f"{1 - endpoint['availability']:.12g}"


def record_name(metric: str, window: str) -> str:
    return f"klukai:slo_{metric}:{window}"


def recordings(endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    labels = {"method": endpoint["method"], "route": endpoint["route"]}
    match = selector(endpoint, True)
    for window in WINDOWS:
        request = f"sum(rate(klukai_http_requests_total{{{match}}}[{window}]))"
        error = f'sum(rate(klukai_http_requests_total{{{match},status_class="5xx"}}[{window}]))'
        expressions = {
            "requests": f"sum(increase(klukai_http_requests_total{{{match}}}[{window}]))",
            "burn_rate": f"(({error} or ({request} * 0)) / {request}) / {budget(endpoint)}",
            "p99_ms": f"histogram_quantile(0.99, sum by (le) (rate(klukai_http_request_duration_ms_bucket{{{match}}}[{window}])))",
        }
        result.extend(
            {
                "record": record_name(metric, window),
                "expr": expression,
                "labels": labels,
            }
            for metric, expression in expressions.items()
        )
    return result


def sample_gate(endpoint: dict[str, Any], minimum: int) -> str:
    match = selector(endpoint)
    return " and on (route, method) ".join(
        f"({record_name('requests', window)}{{{match}}} >= {minimum})"
        for window in WINDOWS
    )


def endpoint_alerts(endpoint: dict[str, Any], policy: Policy) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    match = selector(endpoint)
    for metric, threshold, name, anchor in (
        ("burn_rate", "1", "KlukaiSLOBudgetBurn", "availability-burn"),
        ("p99_ms", str(endpoint["p99_ms"]), "KlukaiSLOLatency", "latency-breach"),
    ):
        comparisons = " and on (route, method) ".join(
            f"({record_name(metric, window)}{{{match}}} > {threshold})"
            for window in WINDOWS
        )
        result.append(
            {
                "alert": name,
                "expr": f"{comparisons} and on (route, method) {sample_gate(endpoint, policy.minimum)}",
                "for": policy.hold,
                "labels": {
                    "severity": "page",
                    "route": endpoint["route"],
                    "method": endpoint["method"],
                },
                "annotations": {
                    "summary": f"{endpoint['method']} {endpoint['route']} {metric} objective exceeded",
                    "description": f"Both {', '.join(WINDOWS)} windows exceed the objective with at least {policy.minimum} requests each. Availability {endpoint['availability']:.5%}; p99 {endpoint['p99_ms']:g} ms. Missing samples never count as healthy.",
                    "runbook_url": f"{RUNBOOK_BASE}{endpoint['runbook']}.md#{anchor}",
                },
            }
        )
    return result


def render_rules(policy: Policy) -> str:
    health = [
        {
            "alert": "KlukaiCoreDown",
            "expr": 'up{job="klukai-core"} == 0',
            "for": policy.hold,
            "labels": {"severity": "page"},
            "annotations": {
                "summary": "Klukai scrape target unreachable",
                "runbook_url": f"{RUNBOOK_BASE}dependency-outage.md#telemetry-missing",
            },
        },
        {
            "alert": "KlukaiTelemetryMissing",
            "expr": 'absent_over_time(klukai_http_requests_total{job="klukai-core"}[5m])',
            "for": policy.hold,
            "labels": {"severity": "page"},
            "annotations": {
                "summary": "Klukai HTTP RED telemetry is absent",
                "runbook_url": f"{RUNBOOK_BASE}dependency-outage.md#telemetry-missing",
            },
        },
        {
            "alert": "KlukaiDependencyCircuitOpen",
            "expr": 'klukai_circuit_state{job="klukai-core"} == 2',
            "for": policy.hold,
            "labels": {"severity": "ticket"},
            "annotations": {
                "summary": "Klukai dependency circuit is open",
                "runbook_url": f"{RUNBOOK_BASE}dependency-outage.md#dependency-circuit",
            },
        },
    ]
    groups = [
        {
            "name": "klukai_slo_recordings",
            "interval": "30s",
            "rules": [
                rule for endpoint in policy.endpoints for rule in recordings(endpoint)
            ],
        },
        {
            "name": "klukai_slo_alerts",
            "interval": "30s",
            "rules": [
                rule
                for endpoint in policy.endpoints
                for rule in endpoint_alerts(endpoint, policy)
            ]
            + health,
        },
    ]
    return (
        f"# Generated by python -m tools.observability.slo_assets; policy SHA256 {policy.digest}\n"
        + yaml.safe_dump({"groups": groups}, sort_keys=False, width=160)
    )


def render_docs(policy: Policy) -> str:
    rows = [
        "| Method | Registered route | Availability | p99 |",
        "| --- | --- | --- | --- |",
    ]
    rows.extend(
        f"| {e['method']} | `{e['route']}` | {e['availability']:.5%} | {e['p99_ms']:g} ms |"
        for e in policy.endpoints
    )
    return f"""<!-- Generated; policy SHA256 {policy.digest}. Edit {POLICY}, then regenerate. -->
# Service level objectives

The canonical thresholds are [{POLICY}](../{POLICY}). This generated table covers the registered HTTP endpoints in that policy over a rolling {policy.data["window_days"]}-day window.

{chr(10).join(rows)}

## Measurement contract

Core emits `klukai_http_requests_total{{route,method,status_class}}` and cumulative `klukai_http_request_duration_ms_bucket{{route,method,le}}` at authenticated `/internal/metrics`. `/api/metrics` remains the admin JSON API. Route values are matched templates; unknown URLs share `_unmatched`. Latency measures the core HTTP handler through response headers, excluding gateway/network overhead, WebSocket turns and time to first token. HTTP 5xx consumes availability; authentication rejections are completed service responses.

## Burn and latency alerts

For each endpoint, the generated recording rules compute requests, normalized availability burn and histogram p99 over both 5m and 1h. Burn is `(5xx rate / request rate) / (1 - availability)`. A value above 1 consumes the endpoint budget faster than its target; both windows must exceed 1 and each must have at least **{policy.minimum} requests**. Latency alerts use the same sample gates and the endpoint p99 threshold. Alerts persist for **{policy.hold}**, derived from the policy rollback polling interval times consecutive breaches. These are budget-boundary alerts, without an additional unconfigured multiplier.

Absent input stays absent; zero requests does not become a synthetic successful sample. Low traffic does not page for endpoint burn or latency. A separate `KlukaiTelemetryMissing` alert detects missing HTTP counters, and `KlukaiCoreDown` detects an unreachable scrape target. No-data dashboard fields explicitly say `NO DATA`.

Rules: [deployable rules](../obs/prometheus/klukai-slo.rules.yml), [review copy](alerts/klukai.yaml). Runbooks: [availability burn](runbooks/dependency-outage.md#availability-burn), [latency breach](runbooks/dependency-outage.md#latency-breach), [missing telemetry](runbooks/dependency-outage.md#telemetry-missing).

## Rollback and retention

The same policy supplies rollback minimum samples {policy.minimum}, consecutive breaches {policy.data["rollback"]["consecutive_breaches"]}, and polling interval {policy.data["rollback"]["poll_seconds"]} seconds. The release deployer owns evaluation and compatible artifact rollback; an alert never restores production data.

A full rolling-window availability report requires at least {policy.data["window_days"]} days of retained metrics. The historical 15-day Prometheus setting cannot prove that report. The root operator must verify effective live retention, storage capacity, scrape ingestion and notification routing before operational acceptance. Generated files and passing promtool tests provide source evidence only.

## Reproduce and verify

```bash
python -m tools.observability.slo_assets --write
python -m tools.observability.slo_assets --check
python -m pytest tests/observability/test_slo_assets.py -q
promtool check rules obs/prometheus/klukai-slo.rules.yml
promtool test rules tests/observability/slo-rules.test.yml
```

The check compares generated bytes, validates every generated alert/dashboard runbook anchor and verifies provisioned datasource UIDs. Changes to thresholds require editing the canonical JSON and reviewing regenerated rules, docs and dashboards. WebSocket and LLM observations are shown in the chat dashboard without inventing HTTP endpoint objectives.
"""


def panel(
    identity: int,
    title: str,
    expression: str,
    unit: str,
    threshold: float | None = None,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit, "noValue": "NO DATA"}
    if threshold is not None:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "red", "value": threshold},
            ],
        }
    return {
        "id": identity,
        "title": title,
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "targets": [{"refId": "A", "expr": expression}],
        "fieldConfig": {"defaults": defaults},
        "gridPos": {
            "h": 8,
            "w": 12,
            "x": 12 * ((identity - 1) % 2),
            "y": 8 * ((identity - 1) // 2),
        },
    }


def render_dashboard(policy: Policy, chat: bool = False) -> str:
    if chat:
        panels = [
            panel(
                1,
                "Observed LLM duration (no HTTP SLO)",
                'histogram_quantile(0.99, sum by (model,le) (rate(klukai_llm_latency_ms_bucket{job="klukai-core"}[5m])))',
                "ms",
            ),
            panel(
                2,
                "Model request rate",
                'sum by (model) (rate(klukai_llm_requests_total{job="klukai-core"}[5m]))',
                "reqps",
            ),
            panel(
                3,
                "Input tokens",
                'sum by (model) (rate(klukai_llm_tokens_in_total{job="klukai-core"}[5m]))',
                "ops",
            ),
            panel(
                4,
                "Output tokens",
                'sum by (model) (rate(klukai_llm_tokens_out_total{job="klukai-core"}[5m]))',
                "ops",
            ),
        ]
    else:
        panels = [
            panel(1, "Scrape target availability", 'up{job="klukai-core"}', "none"),
            panel(
                2,
                "Circuit state: closed0 / probe1 / open2",
                'klukai_circuit_state{job="klukai-core"}',
                "none",
            ),
        ]
        for endpoint in policy.endpoints:
            match = selector(endpoint)
            for metric, title, unit, threshold in (
                ("p99_ms", "p99", "ms", endpoint["p99_ms"]),
                ("burn_rate", "budget burn", "none", 1),
            ):
                item = panel(
                    len(panels) + 1,
                    f"{endpoint['method']} {endpoint['route']} {title}",
                    f"{record_name(metric, '5m')}{{{match}}}",
                    unit,
                    threshold,
                )
                item["links"] = [
                    {
                        "title": "Runbook",
                        "url": f"{RUNBOOK_BASE}{endpoint['runbook']}.md#{'latency-breach' if metric == 'p99_ms' else 'availability-burn'}",
                    }
                ]
                panels.append(item)
    dashboard: dict[str, Any] = {
        "uid": "klukai-chat-path" if chat else "klukai-overview",
        "title": "Klukai Chat Observations" if chat else "Klukai SLO Overview",
        "description": f"Generated from {POLICY}; SHA256 {policy.digest}. Missing telemetry is not healthy.",
        "schemaVersion": 38,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "annotations": {"list": []},
        "tags": ["klukai", "canonical-slo"],
        "panels": panels,
    }
    return json.dumps(dashboard, indent=2) + "\n"
