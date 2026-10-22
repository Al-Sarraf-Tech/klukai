"""One controller, two pools: Dominus when it is online, amarillo otherwise."""

from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/amarillo/ci-runner/klukai_ci_runner.py"
sys.path.insert(0, str(Path(__file__).parent))
from test_ci_runner import FakeGitHub, FakeHost, job  # noqa: E402

DOMINUS = {"pool": "dominus", "steal_from": None, "max_vcpus": 20, "max_memory_mib": 32768,
           "slots": 8, "subnet": "10.232.0", "artifacts": "/srv/klukai-ci/artifacts",
           "temp_sensor": "k10temp", "token_source": "gh-cli",
           "heartbeat_variable": "KLUKAI_CI_DOMINUS_HEARTBEAT",
           "gaming_marker": "/run/user/1000/dominus-gpu/game-active", "quiet_hours": [23, 8],
           "start_below_c": 80, "pause_at_c": 90, "resume_below_c": 78}


def fresh():
    """A private copy of the module, so a host config never leaks between tests."""
    spec = importlib.util.spec_from_file_location(f"klukai_ci_runner_{id(object())}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the defining module
    spec.loader.exec_module(module)
    return module


class Beats(FakeGitHub):
    def __init__(self, jobs=()):
        super().__init__(jobs)
        self.variables: list[tuple[str, str]] = []

    def set_variable(self, name, value):
        self.variables.append((name, value))


@pytest.fixture
def build(tmp_path, monkeypatch):
    def make(runner, jobs=(), *, now=1000.0, mem=60000, temp=45.0):
        root = tmp_path / "klukai-ci"
        (root / "base").mkdir(parents=True, exist_ok=True)
        (root / "base" / "base.qcow2").write_bytes(b"base")
        (root / "base" / "base.qcow2.sha256").write_text("x")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for name, value in (("ROOT", root), ("BASE", root / "base" / "base.qcow2"),
                            ("BASE_DIGEST", root / "base" / "base.qcow2.sha256"), ("JOBS", root / "jobs"),
                            ("CONSOLE_LOGS", tmp_path / "logs"), ("ARTIFACTS", artifacts)):
            monkeypatch.setattr(runner, name, value)
        monkeypatch.setattr(runner.shutil, "chown", lambda *a, **k: None)
        state = {"now": now, "mem": mem, "temp": temp}
        github, host = Beats(jobs), FakeHost()
        controller = runner.Controller(github, run=host, temp=lambda: state["temp"],
                                       memory=lambda: state["mem"], clock=lambda: state["now"],
                                       state_file=tmp_path / "state.json")
        return controller, github, host, state
    return make


def test_amarillo_needs_no_host_config_and_keeps_its_labels():
    runner = fresh()
    assert runner.POOL == "amarillo" and set(runner.PROFILES) == {
        f"klukai-amarillo-{k}" for k in ("heavy", "small", "medium", "build", "serial", "frontend")}
    assert runner.address_for(0) == "10.231.0.10"


def test_the_dominus_config_turns_the_module_into_its_pool():
    runner = fresh()
    runner.configure(DOMINUS)
    assert set(runner.PROFILES) == {f"klukai-dominus-{k}" for k in runner.KINDS}
    assert runner.STEAL_PROFILES == {}
    assert (runner.MAX_VCPUS, runner.MAX_MEMORY_MIB, runner.SLOTS) == (20, 32768, 8)
    assert runner.address_for(3) == "10.232.0.13"
    assert '"via": "10.232.0.1"' in runner.network_config(3)
    assert runner.TEMP_SENSOR == "k10temp" and runner.TOKEN_SOURCE == "gh-cli"
    with pytest.raises(runner.RunnerError):
        runner.configure({"pool": "dominus", "sudo": True})
    with pytest.raises(runner.RunnerError):
        runner.configure({"subnet": "192.168.1"})


def test_an_amd_package_temperature_is_read_from_k10temp(tmp_path):
    runner = fresh()
    runner.configure({"temp_sensor": "k10temp"})
    node = tmp_path / "hwmon3"
    node.mkdir()
    (node / "name").write_text("k10temp\n")
    (node / "temp1_input").write_text("50500\n")
    assert runner.cpu_temp_c(tmp_path) == 50.5


def test_amarillo_keeps_its_thermal_limits_and_dominus_has_its_own(build):
    amarillo = fresh()
    assert (amarillo.START_BELOW_C, amarillo.PAUSE_AT_C, amarillo.RESUME_BELOW_C) == (62.0, 68.0, 60.0)
    runner = fresh()
    runner.configure(DOMINUS)
    assert (runner.START_BELOW_C, runner.PAUSE_AT_C, runner.RESUME_BELOW_C) == (80.0, 90.0, 78.0)
    # A 7950X at 70 C is lightly loaded: Dominus still starts guests and keeps them running.
    controller, github, host, state = build(runner, [job("klukai-dominus-serial")], temp=70.0)
    controller.tick()
    assert len(controller.guests) == 1
    controller.thermal()
    assert not any(g.paused for g in controller.guests.values())
    state["temp"] = 90.0
    controller.thermal()
    assert all(g.paused for g in controller.guests.values())
    for bad in ({"start_below_c": 91, "pause_at_c": 90}, {"resume_below_c": 95, "pause_at_c": 90},
                {"pause_at_c": 99}):
        other = fresh()
        with pytest.raises(other.RunnerError):
            other.configure(bad)


def test_dominus_runs_twenty_threads_within_thirty_two_gib(build):
    runner = fresh()
    runner.configure(DOMINUS)
    jobs = [job("klukai-dominus-frontend", f"2026-10-22T10:00:0{i}Z") for i in range(5)]
    controller, github, host, state = build(runner, jobs)
    controller.tick()
    # frontend VMs are 4 threads / 8 GiB: memory (32 GiB) admits four, not five.
    assert len(controller.guests) == 4
    assert sum(g.profile.memory_mib for g in controller.guests.values()) <= 32768


def test_dominus_never_takes_amarillo_jobs(build):
    runner = fresh()
    runner.configure(DOMINUS)
    controller, github, host, state = build(runner, [job("klukai-amarillo-build")])
    state["now"] += 3600
    controller.tick()
    assert controller.guests == {}


def test_amarillo_takes_over_a_dominus_job_that_waits_too_long(build):
    runner = fresh()
    controller, github, host, state = build(runner, [job("klukai-dominus-build", "2026-10-22T10:00:00Z")])
    controller.tick()
    assert controller.guests == {}  # Dominus gets its chance first
    state["now"] += runner.STEAL_AFTER_SECONDS - 1
    controller.tick()
    assert controller.guests == {}
    state["now"] += 2
    controller.tick()
    (guest,) = controller.guests.values()
    assert guest.profile.label == "klukai-dominus-build" and guest.profile.vcpus == 4
    assert github.registered[100] == "klukai-dominus-build"  # GitHub hands that job to this VM


def test_a_restarted_amarillo_controller_still_knows_a_taken_over_guest(build, tmp_path):
    runner = fresh()
    controller, github, host, state = build(runner, [job("klukai-dominus-serial", "2026-10-22T10:00:00Z")])
    controller.tick()
    state["now"] += runner.STEAL_AFTER_SECONDS + 1
    controller.tick()
    restarted = runner.Controller(github, run=host, temp=lambda: 45.0, memory=lambda: 60000,
                                  clock=lambda: state["now"], state_file=tmp_path / "state.json")
    assert [g.profile.label for g in restarted.guests.values()] == ["klukai-dominus-serial"]


def test_the_heartbeat_tells_the_router_when_dominus_can_take_runs(build):
    runner = fresh()
    runner.configure(DOMINUS)
    hour10 = datetime(2026, 10, 22, 10, 0).timestamp()
    controller, github, host, state = build(runner, now=hour10)
    controller.tick()
    assert github.variables == [("KLUKAI_CI_DOMINUS_HEARTBEAT", str(int(hour10)))]
    state["now"] += 30
    controller.tick()
    assert len(github.variables) == 1  # at most once a minute
    state["now"] += 31
    controller.tick()
    assert github.variables[-1] == ("KLUKAI_CI_DOMINUS_HEARTBEAT", str(int(state["now"])))
    state["temp"] = 85.0  # too warm for Dominus: unhealthy, say so once
    controller.tick()
    controller.tick()
    assert [v for _, v in github.variables].count("0") == 1


@pytest.mark.parametrize("guard", ["gaming", "night"])
def test_no_new_dominus_guest_while_gaming_or_overnight(build, tmp_path, guard):
    runner = fresh()
    marker = tmp_path / "game-active"
    runner.configure({**DOMINUS, "gaming_marker": str(marker)})
    when = datetime(2026, 10, 22, 23 if guard == "night" else 15, 30).timestamp()
    controller, github, host, state = build(runner, [job("klukai-dominus-small")], now=when)
    if guard == "gaming":
        marker.write_text("")
    controller.tick()
    assert controller.guests == {}
    assert github.variables == [("KLUKAI_CI_DOMINUS_HEARTBEAT", "0")]


def test_the_perf_vm_runs_alone_in_any_pool(build):
    runner = fresh()
    runner.configure(DOMINUS)
    controller, github, host, state = build(runner, [job("klukai-dominus-heavy", "2026-10-22T10:00:00Z"),
                                                     job("klukai-dominus-small", "2026-10-22T10:00:01Z")])
    controller.tick()
    assert [g.profile.kind for g in controller.guests.values()] == ["small"]


def test_old_records_are_pruned_and_the_cache_and_fresh_runs_kept(tmp_path):
    import os
    runner = fresh()
    old = tmp_path / ("a" * 40) / "1-1" / "frontend"
    new = tmp_path / ("b" * 40) / "2-1" / "core"
    cache = tmp_path / "cache" / "pip"
    for path in (old, new, cache):
        path.mkdir(parents=True)
        (path / "file").write_text("x")
    now = 10 * 86400.0
    for path in [old, old / "file", old.parent, cache, cache / "file"]:
        os.utime(path, (now - 3 * 86400, now - 3 * 86400))
    for path in [new, new / "file", new.parent]:
        os.utime(path, (now - 3600, now - 3600))
    assert runner.prune_records(tmp_path, days=2, now=now) == 1
    assert not (tmp_path / ("a" * 40)).exists() and new.exists() and cache.exists()
