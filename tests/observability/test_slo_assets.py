"""Independent behavioral checks for canonical SLO operational assets."""

from __future__ import annotations
import json
from pathlib import Path
import shutil

import pytest
import yaml

from tools.observability.slo_assets import check, render, write

REPO = Path(__file__).resolve().parents[2]
POLICY = Path("docker/core/app/observability/slos.json")


@pytest.fixture
def repository(tmp_path):
    for relative in (
        POLICY,
        Path("docs/runbooks/dependency-outage.md"),
        Path("docs/runbooks/high-latency.md"),
        Path("obs/grafana/provisioning/datasources/datasources.yml"),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == POLICY:
            shutil.copy(REPO / relative, target)
        elif relative.suffix == ".yml":
            target.write_text(
                "datasources:\n- {type: prometheus, uid: prometheus}\n- {type: loki, uid: loki}\n- {type: tempo, uid: tempo}\n"
            )
        else:
            target.write_text(
                "\n".join(
                    f'<a id="{anchor}"></a>'
                    for anchor in (
                        "availability-burn",
                        "latency-breach",
                        "telemetry-missing",
                        "dependency-circuit",
                    )
                )
            )
    return tmp_path


def rules(assets):
    data = yaml.safe_load(assets["obs/prometheus/klukai-slo.rules.yml"])
    return [rule for group in data["groups"] for rule in group["rules"]]


def test_policy_change_propagates_to_rules_docs_and_dashboard(repository):
    before = render(repository)
    source = repository / POLICY
    policy = json.loads(source.read_text())
    policy["endpoints"][0]["availability"] = 0.997
    policy["endpoints"][0]["p99_ms"] = 77
    policy["rollback"]["minimum_requests"] = 31
    source.write_text(json.dumps(policy))
    after = render(repository)
    assert before != after
    target = [r for r in rules(after) if r.get("labels", {}).get("route") == "/health"]
    assert any(
        "0.003" in r["expr"]
        for r in target
        if r.get("record", "").startswith("klukai:slo_burn")
    )
    assert any(
        "> 77" in r["expr"] and ">= 31" in r["expr"]
        for r in target
        if r.get("alert") == "KlukaiSLOLatency"
    )
    assert "99.70000%" in after["docs/slos.md"] and "77 ms" in after["docs/slos.md"]
    dashboard = json.loads(after["docs/dashboards/overview.json"])
    health = next(
        panel for panel in dashboard["panels"] if panel["title"] == "GET /health p99"
    )
    assert health["fieldConfig"]["defaults"]["thresholds"]["steps"][-1]["value"] == 77


def test_deterministic_check_detects_any_drift(repository):
    write(repository)
    check(repository)
    generated = repository / "docs/alerts/klukai.yaml"
    generated.write_text(
        generated.read_text().replace("severity: page", "severity: ticket", 1)
    )
    with pytest.raises(ValueError, match="drift"):
        check(repository)


def test_check_requires_outputs_instead_of_silent_generation(repository):
    with pytest.raises(ValueError, match="missing"):
        check(repository)
    assert not (repository / "docs/alerts/klukai.yaml").exists()


def test_invalid_runbook_anchor_fails_closed(repository):
    write(repository)
    (repository / "docs/runbooks/dependency-outage.md").write_text(
        "# No matching anchors\n"
    )
    with pytest.raises(ValueError, match="anchor"):
        check(repository)


def test_missing_datasource_uid_is_a_validation_failure(repository):
    write(repository)
    (repository / "obs/grafana/provisioning/datasources/datasources.yml").write_text(
        "datasources: []\n"
    )
    with pytest.raises(ValueError, match="datasource"):
        check(repository)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "invalid_number",
        "traversal",
        "empty",
        "nan",
        "method",
        "window",
        "rollback",
    ],
)
def test_invalid_canonical_input_is_rejected(repository, mutation):
    source = repository / POLICY
    policy = json.loads(source.read_text())
    if mutation == "duplicate":
        policy["endpoints"].append(policy["endpoints"][0].copy())
    elif mutation == "invalid_number":
        policy["endpoints"][0]["availability"] = "99.9%"
    elif mutation == "traversal":
        policy["endpoints"][0]["runbook"] = "../secret"
    elif mutation == "nan":
        policy["endpoints"][0]["p99_ms"] = float("nan")
    elif mutation == "method":
        policy["endpoints"][0]["method"] = []
    elif mutation == "window":
        policy["window_days"] = True
    elif mutation == "rollback":
        policy["rollback"]["minimum_requests"] = True
    else:
        policy["endpoints"] = []
    source.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        render(repository)


def test_recordings_use_emitted_schema_preserve_absence_and_gate_low_samples(
    repository,
):
    generated = rules(render(repository))
    recordings = [rule for rule in generated if "record" in rule]
    burns = [rule for rule in recordings if "burn_rate" in rule["record"]]
    assert len(burns) == 20
    assert all('status_class="5xx"' in rule["expr"] for rule in burns)
    assert all(
        "vector(0)" not in rule["expr"] and "clamp_min" not in rule["expr"]
        for rule in burns
    )
    alerts = [rule for rule in generated if rule.get("alert") == "KlukaiSLOBudgetBurn"]
    assert len(alerts) == 10
    assert all(
        ">= 20" in rule["expr"] and ":5m{" in rule["expr"] and ":1h{" in rule["expr"]
        for rule in alerts
    )
    assert all(rule["for"] == "30s" for rule in alerts)
    assert "api/chat/turn" not in str(generated)
    for asset in render(repository).values():
        assert "http_request_duration_seconds" not in asset


def test_chat_dashboard_only_queries_existing_llm_metrics(repository):
    dashboard = json.loads(render(repository)["docs/dashboards/chat-path.json"])
    queries = [panel["targets"][0]["expr"] for panel in dashboard["panels"]]
    for metric in (
        "klukai_llm_requests_total",
        "klukai_llm_latency_ms_bucket",
        "klukai_llm_tokens_in_total",
        "klukai_llm_tokens_out_total",
    ):
        assert any(metric in query for query in queries)
    assert all('job="klukai-core"' in query for query in queries)
    assert all(
        "thresholds" not in panel["fieldConfig"]["defaults"]
        for panel in dashboard["panels"]
    )


def test_datasource_uid_must_resolve_to_the_expected_type(repository):
    write(repository)
    target = repository / "obs/grafana/provisioning/datasources/datasources.yml"
    target.write_text("datasources: [{uid: prometheus, type: loki}]\n")
    with pytest.raises(ValueError, match="datasource"):
        check(repository)
