"""The CI router: Dominus when its heartbeat is fresh, amarillo otherwise."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.ci import pool  # noqa: E402

NOW = 1_800_000_000


@pytest.mark.parametrize("beat,expected", [
    (str(NOW - 30), "dominus"),
    (str(NOW - pool.FRESH_SECONDS), "dominus"),
    (str(NOW - pool.FRESH_SECONDS - 1), "amarillo"),   # stale: Dominus off or stuck
    (str(NOW + 30), "dominus"),                        # small clock skew
    (str(NOW + pool.SKEW_SECONDS + 1), "amarillo"),    # a future beat is not trusted
    ("0", "amarillo"),                                 # Dominus said it cannot take runs
    ("", "amarillo"),                                  # the variable does not exist yet
    ("garbage", "amarillo"),
    ("-5", "amarillo"),
])
def test_a_fresh_heartbeat_routes_to_dominus_and_anything_else_to_amarillo(beat, expected):
    assert pool.plan(beat, NOW)["pool"] == expected


@pytest.mark.parametrize("beat", [str(NOW - 10), "0"])
def test_only_known_label_sets_come_out(beat):
    out = pool.plan(beat, NOW)
    for group in ("build", "serial", "frontend"):
        labels = json.loads(out[f"labels_{group}"])
        assert labels == ["self-hosted", "Linux", "X64", f"klukai-{out['pool']}-{group}"]


def test_the_cli_writes_github_output_lines():
    result = subprocess.run([sys.executable, "-m", "tools.ci.pool", "--heartbeat", str(NOW - 5),
                             "--now", str(NOW)], cwd=ROOT, capture_output=True, text=True, check=True)
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert lines["pool"] == "dominus" and json.loads(lines["labels_build"])[-1] == "klukai-dominus-build"


@pytest.mark.parametrize("beat,suites", [
    (str(NOW - 30), ["ops", "voice", "bridge", "js"]),
    ("0", ["all"]),
    ("garbage", ["all"]),
    (str(NOW + pool.SKEW_SECONDS + 1), ["all"]),
])
def test_component_suites_fan_out_only_on_dominus(beat, suites):
    assert json.loads(pool.plan(beat, NOW)["component_suites"]) == suites


@pytest.mark.parametrize("beat", ["\u00b2", "9" * 5000, "--x", "1793583808\npool=dominus"])
def test_a_heartbeat_that_is_not_a_plain_number_stays_on_amarillo(beat):
    assert pool.plan(beat, NOW)["pool"] == "amarillo"


def test_the_cli_takes_a_heartbeat_that_looks_like_a_flag():
    result = subprocess.run([sys.executable, "-m", "tools.ci.pool", "--heartbeat=--x", "--now", str(NOW)],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    assert "pool=amarillo" in result.stdout


@pytest.mark.parametrize("gateway,host", [("10.231.0.1", "amarillo"), ("10.232.0.1", "dominus")])
def test_jobs_that_load_the_image_follow_the_pool_that_built_it(gateway, host):
    route = f"default via {gateway} dev enp1s0 proto static\n"
    assert json.loads(pool.here(route)["labels_serial"])[-1] == f"klukai-{host}-serial"


@pytest.mark.parametrize("route", ["default via 192.168.1.1 dev eth0\n", ""])
def test_an_unknown_gateway_fails_the_job_rather_than_guessing(route):
    with pytest.raises(SystemExit):
        pool.here(route)


def test_the_cli_reports_this_vms_pool():
    result = subprocess.run([sys.executable, "-m", "tools.ci.pool", "--here", "default via 10.232.0.1 dev e\n"],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    assert json.loads(dict(line.split("=", 1) for line in result.stdout.splitlines())["labels_serial"])[-1] \
        == "klukai-dominus-serial"
