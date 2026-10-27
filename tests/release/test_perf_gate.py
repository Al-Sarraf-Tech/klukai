"""Adversarial performance evidence validation, independent of measured timings."""

from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import pytest
import httpx

from tools.ci.perf_gate import compare
from tools.ci.perf_measure import Sampler, contract
from tools.ci.perf_resources import DisposableStack, inspect_image
from tools.ci.perf_network import LoopbackProxy
from tools.ci.perf_stack import execute

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "docker/core/app/observability/slos.json"
TARGETS = [
    ("GET", "/health"),
    ("GET", "/api/health/live"),
    ("GET", "/api/health/ready"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/verify"),
    ("GET", "/api/affection"),
    ("GET", "/api/messages"),
    ("GET", "/api/memories"),
]


def test_disposable_redis_uses_nonroot_without_setuid_capability(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("tools.ci.perf_resources.cpu_temperature", lambda: 40.0)
    monkeypatch.setattr(
        "tools.ci.perf_resources.docker",
        lambda *args, **kwargs: calls.append(args) or "",
    )
    stack = DisposableStack(tmp_path, ROOT)
    stack._create("redis", "sha256:" + "a" * 64)
    assert "--user" in calls[0]
    assert calls[0][calls[0].index("--user") + 1] != "0:0"


def test_disposable_qdrant_snapshots_writable_only_on_tmpfs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("tools.ci.perf_resources.cpu_temperature", lambda: 40.0)
    monkeypatch.setattr(
        "tools.ci.perf_resources.docker",
        lambda *args, **kwargs: calls.append(args) or "",
    )
    DisposableStack(tmp_path, ROOT)._create("qdrant", "sha256:" + "a" * 64)
    assert any(item.startswith("/qdrant/snapshots:rw,size=") for item in calls[0])
    assert "--read-only" in calls[0]


@pytest.mark.parametrize(
    "route,body",
    [
        ("/api/auth/verify", {"user_id": "ricky"}),
        ("/api/messages", {"messages": [{"content": "seed:ricky:0"}] * 50}),
        ("/api/memories", [{"annotation": "seed:ricky:0"}] * 20),
        ("/api/health/ready", {"status": "degraded"}),
        # Voice is Fish Audio only (paid); it is no longer a measured contract.
        ("/api/tts", {"status": "queued", "job_id": "test"}),
    ],
)
def test_actual_contract_validation_rejects_wrong_owner_and_fake_work(route, body):
    assert not contract(route, body)


def report(role="baseline", millis=10.0):
    revision = ("a" if role == "baseline" else "b") * 40
    image = "sha256:" + ("a" if role == "baseline" else "b") * 64
    return {
        "schema": 1,
        "role": role,
        "run_id": "paired-evidence",
        "image": {
            "id": image,
            "declared_revision": revision,
            "image_revision": revision,
            "verified": True,
        },
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "provenance": {
            key: hashlib.sha256(key.encode()).hexdigest()
            for key in ("harness", "fixture", "seed", "host")
        }
        | {
            "policy": hashlib.sha256(
                json.dumps(
                    json.loads(POLICY.read_text()),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        },
        "resources": {
            "core": {"nano_cpus": 1400000000, "memory_bytes": 805306368},
            "postgres": {"nano_cpus": 400000000, "memory_bytes": 402653184},
            "redis": {"nano_cpus": 100000000, "memory_bytes": 100663296},
            "qdrant": {"nano_cpus": 400000000, "memory_bytes": 402653184},
            "fixture": {"nano_cpus": 200000000, "memory_bytes": 100663296},
        },
        "configuration": {
            "samples": 100,
            "rounds": 3,
            "warmup": 10,
            "concurrency": 1,
            "seed_version": 1,
        },
        "cpu_temperature_max_c": 49.0,
        "cleanup_verified": True,
        "development_only": False,
        "rounds": [
            {
                "round": index,
                "actual_container": {
                    "container": f"test-{role}-{index}",
                    "image": image,
                    "resources": {"nano_cpus": 1400000000, "memory_bytes": 805306368},
                },
                "routes": [
                    {
                        "method": method,
                        "route": route,
                        "warmup_count": 10,
                        "samples": [
                            {
                                "status": 200,
                                "elapsed_ms": millis,
                                "contract_ok": True,
                            }
                            for _ in range(100)
                        ],
                    }
                    for method, route in TARGETS
                ],
            }
            for index in range(3)
        ],
    }


def arguments():
    return {
        "policy": json.loads(POLICY.read_text()),
        "baseline_image": "sha256:" + "a" * 64,
        "candidate_image": "sha256:" + "b" * 64,
        "baseline_revision": "a" * 40,
        "candidate_revision": "b" * 40,
    }


def test_valid_evidence_compares_fresh_images_and_preserves_baseline():
    baseline, candidate = report(), report("candidate", 11)
    original = deepcopy(baseline)
    result = compare(baseline, candidate, **arguments())
    assert result["passed"] is True
    assert result["max_p99_ratio"] == pytest.approx(1.1)
    assert baseline == original


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "nan",
        "zero",
        "401",
        "404",
        "duplicate",
        "revision",
        "warmup",
        "missing_route",
        "missing_round",
        "stale",
        "unclean",
        "hot",
        "unverified",
    ],
)
def test_invalid_evidence_fails_closed(mutation):
    candidate = report("candidate")
    sample = candidate["rounds"][0]["routes"][0]["samples"][0]
    if mutation == "empty":
        candidate["rounds"][0]["routes"][0]["samples"] = []
    elif mutation == "nan":
        sample["elapsed_ms"] = float("nan")
    elif mutation == "zero":
        sample["elapsed_ms"] = 0
    elif mutation in ("401", "404"):
        sample["status"] = int(mutation)
    elif mutation == "duplicate":
        candidate["rounds"][0]["routes"].append(
            deepcopy(candidate["rounds"][0]["routes"][0])
        )
    elif mutation == "revision":
        candidate["image"]["image_revision"] = "c" * 40
    elif mutation == "warmup":
        candidate["configuration"]["warmup"] = 0
    elif mutation == "missing_route":
        candidate["rounds"][0]["routes"].pop()
    elif mutation == "missing_round":
        candidate["rounds"].pop()
    elif mutation == "stale":
        candidate["finished_at"] = "2000-01-01T00:00:00+00:00"
    elif mutation == "unclean":
        candidate["cleanup_verified"] = False
    elif mutation == "hot":
        candidate["cpu_temperature_max_c"] = 70.0
    else:
        candidate["image"]["verified"] = False
    with pytest.raises(ValueError):
        compare(report(), candidate, **arguments())


@pytest.mark.parametrize(
    "field", ["resources", "fixture", "seed", "policy", "host", "samples"]
)
def test_noncomparable_configuration_is_rejected(field):
    candidate = report("candidate")
    if field == "resources":
        candidate["resources"]["core"]["nano_cpus"] = 1000000000
    elif field == "samples":
        candidate["configuration"]["samples"] = 101
    else:
        candidate["provenance"][field] = "f" * 64
    with pytest.raises(ValueError):
        compare(report(), candidate, **arguments())


def test_more_than_twenty_percent_p99_regression_fails_even_below_absolute_slo():
    result = compare(report(), report("candidate", 15.01), **arguments())
    assert result["passed"] is False
    assert any("regression" in failure for failure in result["failures"])


def test_exact_twenty_percent_is_permitted():
    assert compare(report(), report("candidate", 12), **arguments())["passed"] is True


def test_concurrency_other_than_one_cannot_certify_comparable_samples():
    baseline, candidate = report(), report("candidate")
    baseline["configuration"]["concurrency"] = 2
    candidate["configuration"]["concurrency"] = 2
    with pytest.raises(ValueError):
        compare(baseline, candidate, **arguments())


@pytest.mark.parametrize("version", [99, True, "1"])
def test_noncanonical_seed_configuration_cannot_certify(version):
    baseline, candidate = report(), report("candidate")
    for data in (baseline, candidate):
        data["configuration"]["seed_version"] = version
    with pytest.raises(ValueError):
        compare(baseline, candidate, **arguments())


def test_canonical_absolute_slo_failure_cannot_hide_behind_fast_baseline():
    baseline, candidate = report(millis=31), report("candidate", 31)
    result = compare(baseline, candidate, **arguments())
    assert result["passed"] is False
    assert any("absolute" in failure for failure in result["failures"])


def test_a_single_noisy_round_is_not_a_regression():
    # Owner decision 2026-10-17: an A/A nightly failed on one /api/tts round.
    baseline, candidate = report(), report("candidate")
    for sample in candidate["rounds"][1]["routes"][0]["samples"]:
        sample["elapsed_ms"] = 20
    result = compare(baseline, candidate, **arguments())
    assert result["passed"] is True
    assert result["regressed_rounds_required"] == 2


def test_a_regression_in_two_of_three_rounds_fails_even_if_pooled_hides_it():
    baseline, candidate = report(), report("candidate")
    for index in (0, 2):
        for sample in candidate["rounds"][index]["routes"][0]["samples"]:
            sample["elapsed_ms"] = 20
    result = compare(baseline, candidate, **arguments())
    assert result["passed"] is False
    assert any("in 2 of 3 rounds (0,2)" in failure for failure in result["failures"])


def test_development_reports_cannot_satisfy_ci():
    candidate = report("candidate")
    candidate["development_only"] = True
    with pytest.raises(ValueError):
        compare(report(), candidate, **arguments())


@pytest.mark.parametrize("field", ["image", "resources", "missing"])
def test_actual_container_binding_is_required(field):
    candidate = report("candidate")
    actual = candidate["rounds"][0]["actual_container"]
    if field == "missing":
        del candidate["rounds"][0]["actual_container"]
    elif field == "image":
        actual["image"] = "sha256:" + "e" * 64
    else:
        actual["resources"]["nano_cpus"] = 1
    with pytest.raises(ValueError):
        compare(report(), candidate, **arguments())


@pytest.mark.asyncio
async def test_sampler_never_counts_authenticated_body_with_401_as_success(monkeypatch):
    async def noop(_cache):
        return None

    monkeypatch.setattr("tools.ci.perf_measure.reset_rate_limits", noop)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"user_id": "jalsarraf"})
    )
    async with httpx.AsyncClient(
        base_url="http://owned-test.invalid", transport=transport
    ) as client:
        sampler = Sampler(client, None, "disposable", lambda: None)
        row = await sampler.one("GET", "/api/auth/verify", 200)
    assert row["status"] == 401 and row["contract_ok"] is False
    assert row["elapsed_ms"] > 0


@pytest.mark.parametrize(
    "field",
    ["ANTHROPIC_API_KEY", "VOICE_API_TOKEN", "SEED_PASSWORD_JALSARRAF", "DATABASE_URL"],
)
def test_images_with_baked_credentials_are_refused(field, monkeypatch):
    data = [
        {
            "Id": "sha256:" + "a" * 64,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"Env": [field + "=disposable-test-value"]},
        }
    ]
    monkeypatch.setattr(
        "tools.ci.perf_resources.docker", lambda *args: json.dumps(data)
    )
    with pytest.raises(ValueError):
        inspect_image("sha256:" + "a" * 64)


def test_cleanup_attempts_remove_even_if_log_capture_fails(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("tools.ci.perf_resources.cpu_temperature", lambda: 40.0)

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "logs":
            raise RuntimeError("log unavailable")
        return ""

    monkeypatch.setattr("tools.ci.perf_resources.docker", fake_docker)
    stack = DisposableStack(tmp_path, ROOT)
    stack.containers.append("owned-container")
    with pytest.raises(RuntimeError):
        stack.close()
    assert ("rm", "-fv", "owned-container") in calls
    assert json.loads((tmp_path / "cleanup.json").read_text())["verified"] is False


@pytest.mark.asyncio
async def test_existing_baseline_directory_is_never_refreshed(tmp_path, monkeypatch):
    original = b"retained immutable baseline\n"
    (tmp_path / "baseline.json").write_bytes(original)

    def image_info(image):
        return {"id": image, "revision": ("a" if image.endswith("a") else "b") * 40}

    monkeypatch.setattr("tools.ci.perf_stack.inspect_image", image_info)
    args = argparse.Namespace(
        **{key: value for key, value in arguments().items() if key != "policy"},
        out=tmp_path,
        samples=100,
        rounds=3,
        warmup=10,
        concurrency=1,
        development=False,
    )
    with pytest.raises(FileExistsError):
        await execute(args)
    assert (tmp_path / "baseline.json").read_bytes() == original


@pytest.mark.asyncio
async def test_loopback_proxy_cancellation_closes_active_streams():
    async def echo(reader, writer):
        try:
            while data := await reader.read(256):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    proxy = LoopbackProxy("127.0.0.1", upstream.sockets[0].getsockname()[1])
    await proxy.start()
    port = int(proxy.url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"probe")
    await writer.drain()
    assert await reader.readexactly(5) == b"probe"
    await proxy.close()
    assert not proxy.tasks
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    upstream.close()
    await upstream.wait_closed()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


def _virtualized(item):
    item.update(cpu_temperature_max_c=None, thermal_telemetry="virtualized-guest")
    return item


def test_virtualized_guest_without_sensors_is_explicit_not_a_reading():
    result = compare(
        _virtualized(report()), _virtualized(report("candidate", 11)), **arguments()
    )
    assert result["passed"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"thermal_telemetry": "virtualized-guest"},  # reading present
        {"thermal_telemetry": "guessed"},
        {"cpu_temperature_max_c": None},  # sensors claimed, none read
    ],
)
def test_thermal_telemetry_cannot_be_forged_or_omitted(change):
    candidate = report("candidate", 11) | change
    with pytest.raises(ValueError):
        compare(report(), candidate, **arguments())


def test_cpu_temperature_is_unobservable_only_inside_a_hypervisor(
    monkeypatch, tmp_path
):
    from tools.ci import perf_resources

    monkeypatch.setattr(perf_resources, "Path", _NoSensors(tmp_path))
    monkeypatch.setattr(perf_resources, "virtualized_guest", lambda: True)
    assert perf_resources.cpu_temperature() is None
    monkeypatch.setattr(perf_resources, "virtualized_guest", lambda: False)
    with pytest.raises(RuntimeError, match="thermal"):
        perf_resources.cpu_temperature()


class _NoSensors:
    def __init__(self, root):
        self.root = root

    def __call__(self, value):
        return self.root / "hwmon" if value == "/sys/class/hwmon" else Path(value)


def test_backend_identity_accepts_both_docker_image_store_forms():
    from tools.ci.perf_resources import (
        BACKEND_IMAGES,
        BACKEND_INDEX_DIGESTS,
        reviewed_backends,
    )

    assert reviewed_backends(dict(BACKEND_IMAGES))
    assert reviewed_backends(dict(BACKEND_INDEX_DIGESTS))
    assert not reviewed_backends(BACKEND_IMAGES | {"redis": "sha256:" + "0" * 64})
    assert not reviewed_backends({"postgres": BACKEND_IMAGES["postgres"]})


def test_millisecond_jitter_under_the_noise_floor_is_not_a_regression():
    # 10 -> 12.01 ms is +20.1% but only 2.01 ms: two scheduler hiccups.
    assert compare(report(), report("candidate", 12.01), **arguments())["passed"]
    assert compare(report(), report("candidate", 15), **arguments())["passed"]


@pytest.mark.parametrize(
    "measured,failed", [(120.0, False), (120.01, True), (104.99, False)]
)
def test_twenty_percent_budget_applies_above_the_noise_floor(measured, failed):
    from tools.ci.perf_gate import _assess

    route = ("GET", "/api/messages")
    rows = _assess(
        {route: ([100.0] * 100, 1.0)}, {route: ([measured] * 100, 1.0)}, {}, "0"
    )
    assert rows[0]["regressed"] is failed
    assert rows[0]["failures"] == []


def test_messages_contract_tolerates_owner_scheduler_rows_not_leaks():
    from tools.ci.perf_measure import contract

    seed = [{"content": f"seed:jalsarraf:{i}"} for i in range(49)]
    replay = {"content": "End of day. Log your status if you see fit."}
    assert contract("/api/messages", {"messages": [*seed, replay]})
    leak = {"content": "seed:ricky:1"}
    assert not contract("/api/messages", {"messages": [*seed, leak]})
    assert not contract("/api/messages", {"messages": seed})  # 49 rows
    assert not contract("/api/messages", {"messages": [replay] * 50})


def test_paid_cloud_voice_is_never_measured_or_configured():
    from tools.ci.perf_schema import TARGETS as MEASURED
    from tools.ci.perf_stack import environment

    assert not [route for _, route, _ in MEASURED if "/tts" in route]
    env = environment(argparse.Namespace(password="disposable"), "perf_candidate_0")
    assert not [key for key in env if key.startswith(("VOICE_", "FISH_"))]
    assert not [value for value in env.values() if "fish.audio" in value]


def test_fixture_counts_the_startup_mcp_handshake_and_serves_no_voice():
    import threading
    from http.server import ThreadingHTTPServer

    from tools.ci import perf_fixtures

    server = ThreadingHTTPServer(("127.0.0.1", 0), perf_fixtures.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = dict(perf_fixtures.COUNTS)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            trust_env=False,
            timeout=5,
        ) as client:
            session = client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": "1", "method": "initialize"}
            )
            for path in ("/tts", "/unload", "/api/v1/gpu/lease/acquire"):
                assert client.post(path, json={"text": "no voice"}).status_code == 404
            stats = client.get("/fixture-stats").json()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert session.headers["Mcp-Session-Id"] == "isolated-fixture-session"
    assert session.json()["result"]["serverInfo"]["name"] == "isolated-fixture"
    assert stats["external_calls"] == 0 and stats["gpu_calls"] == 0
    assert stats["counts"]["POST /mcp"] == before.get("POST /mcp", 0) + 1
