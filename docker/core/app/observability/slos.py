"""Single machine-readable source for endpoint objectives and health budgets.

Metric names match app.metrics.prometheus_text. 5xx responses consume the
availability budget. Auth rejections count as successful service responses.
"""

from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from ..dependencies import json_object, json_objects

POLICY_PATH = Path(__file__).with_name("slos.json")


@dataclass(frozen=True)
class EndpointSLO:
    method: str
    route: str
    availability: float
    p99_ms: float
    runbook: str

    @property
    def error_budget(self) -> float:
        return 1 - self.availability

    def labels(self) -> str:
        return f"route={json.dumps(self.route)},method={json.dumps(self.method)}"


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    policy = json_object(json.loads(path.read_text()))
    if policy.get("version") != 1 or policy.get("window_days") != 30:
        raise ValueError("Unsupported SLO policy")
    objectives = endpoint_slos(policy)
    if not objectives or len({(s.method, s.route) for s in objectives}) != len(
        objectives
    ):
        raise ValueError("Empty or duplicate SLO objectives")
    rollback = json_object(policy.get("rollback"))
    for key in ("minimum_requests", "consecutive_breaches", "poll_seconds"):
        if not isinstance(rollback.get(key), int) or rollback[key] < 1:
            raise ValueError("Invalid rollback budget")
    return policy


def endpoint_slos(policy: dict[str, Any] | None = None) -> tuple[EndpointSLO, ...]:
    data = policy if policy is not None else load_policy()
    slos = tuple(_endpoint(entry) for entry in json_objects(data.get("endpoints")))
    for slo in slos:
        if not (
            0 < slo.availability < 1 and slo.p99_ms > 0 and slo.route.startswith("/")
        ):
            raise ValueError("Invalid endpoint objective")
    return slos


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a nonempty SLO string")
    return value


def _number(value: object) -> float:
    import math

    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError("Expected a finite SLO number")
    return float(value)


def _endpoint(entry: dict[str, Any]) -> EndpointSLO:
    return EndpointSLO(
        method=_text(entry.get("method")),
        route=_text(entry.get("route")),
        availability=_number(entry.get("availability")),
        p99_ms=_number(entry.get("p99_ms")),
        runbook=_text(entry.get("runbook")),
    )


def validate_routes(routes: set[tuple[str, str]]) -> None:
    missing = {(s.method, s.route) for s in endpoint_slos()} - routes
    if missing:
        raise ValueError(f"SLO targets unregistered routes: {sorted(missing)}")


def recording_rules() -> list[dict[str, str]]:
    """Prometheus recording rules derived from the actual RED label schema."""
    rules = []
    for slo in endpoint_slos():
        for window in ("5m", "1h"):
            selector = slo.labels()
            requests = f"rate(klukai_http_requests_total{{{selector}}}[{window}])"
            errors = f'rate(klukai_http_requests_total{{{selector},status_class="5xx"}}[{window}])'
            rules.append(
                {
                    "record": f"klukai:slo_burn_rate:{window}",
                    "expr": f"(sum({errors}) or vector(0)) / clamp_min(sum({requests}), 0.000001) / {slo.error_budget:.8f}",
                    "route": slo.route,
                    "method": slo.method,
                }
            )
    return rules


def markdown_table() -> str:
    rows = ["| Method | Route | Availability | p99 |", "| --- | --- | --- | --- |"]
    rows.extend(
        f"| {s.method} | `{s.route}` | {s.availability:.2%} | {s.p99_ms:g} ms |"
        for s in endpoint_slos()
    )
    return "\n".join(rows)
