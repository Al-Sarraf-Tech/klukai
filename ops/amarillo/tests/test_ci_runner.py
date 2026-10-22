"""Klukai's on-demand amarillo CI runner: admission, lifecycle and guest shape."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/amarillo/ci-runner/klukai_ci_runner.py"
SPEC = importlib.util.spec_from_file_location("klukai_ci_runner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

HEAVY = runner.PROFILES["klukai-amarillo-heavy"]
SMALL = runner.PROFILES["klukai-amarillo-small"]


def job(label: str, created: str = "2026-10-19T10:00:00Z", **extra) -> dict:
    return {
        "id": hash(created) & 0xFFFF,
        "status": "queued",
        "created_at": created,
        "labels": ["self-hosted", "Linux", "X64", label],
        **extra,
    }


class FakeGitHub:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.registered: dict[int, str] = {}
        self.deleted: list[int] = []
        self.busy: set[int] = set()
        self.unknown: set[int] = set()

    def queued_jobs(self):
        return list(self.jobs)

    def jit_config(self, name, profile):
        runner_id = 100 + len(self.registered)
        self.registered[runner_id] = profile.label
        return runner_id, "SECRET-JIT-" + name

    def delete_runner(self, runner_id):
        self.deleted.append(runner_id)

    def runner_busy(self, runner_id):
        if runner_id in self.unknown:
            return None
        return runner_id in self.busy


class FakeHost:
    """virsh, qemu-img, genisoimage and nft as the controller uses them."""

    def __init__(self):
        self.domains: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.guard = True
        self.network_active = True

    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        self.calls.append(argv)
        out, rc = "", 0
        if argv[0] == "virsh":
            verb = argv[4]
            if verb == "list":
                out = "\n".join(self.domains)
            elif verb == "domstate":
                out = self.domains.get(argv[5], "")
                rc = 0 if out else 1
            elif verb == "net-info":
                out = f"Name: klukai-ci\nActive: {'yes' if self.network_active else 'no'}\nBridge: kci-br0\n"
            elif verb == "create":
                name = ET.parse(argv[5]).getroot().findtext("name")
                self.domains[name] = "running"
            elif verb == "destroy":
                self.domains.pop(argv[5], None)
            elif verb in ("suspend", "resume"):
                self.domains[argv[5]] = "paused" if verb == "suspend" else "running"
        elif argv[0] == "nft":
            out, rc = (
                ("table inet klukai_ci_guard {}" if self.guard else ""),
                (0 if self.guard else 1),
            )
        elif argv[0] == "qemu-img":
            Path(argv[-2]).write_bytes(b"qcow2")
        elif argv[0] == "genisoimage":
            Path(argv[argv.index("-output") + 1]).write_bytes(b"iso")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "klukai-ci"
    (root / "base").mkdir(parents=True)
    (root / "base" / "base.qcow2").write_bytes(b"base")
    (root / "base" / "base.qcow2.sha256").write_text("x")
    artifacts = tmp_path / "ci-artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "BASE", root / "base" / "base.qcow2")
    monkeypatch.setattr(runner, "BASE_DIGEST", root / "base" / "base.qcow2.sha256")
    monkeypatch.setattr(runner, "JOBS", root / "jobs")
    monkeypatch.setattr(runner, "CONSOLE_LOGS", tmp_path / "logs")
    monkeypatch.setattr(runner, "ARTIFACTS", artifacts)
    monkeypatch.setattr(runner.shutil, "chown", lambda *a, **k: None)
    state = {"temp": 45.0, "mem": 17000, "now": 1000.0}

    def make(jobs=()):
        github, host = FakeGitHub(jobs), FakeHost()
        controller = runner.Controller(
            github,
            run=host,
            temp=lambda: state["temp"],
            memory=lambda: state["mem"],
            clock=lambda: state["now"],
            state_file=tmp_path / "state.json",
        )
        return controller, github, host

    return make, state, tmp_path


def test_only_exact_klukai_labels_are_served():
    assert runner.profile_for(job("klukai-amarillo-heavy")) is HEAVY
    assert runner.profile_for(job("klukai-amarillo-small")) is SMALL
    assert runner.profile_for(job("ci-heavy")) is None
    assert runner.profile_for({"labels": ["fleet-ci-v1", "ci-small"]}) is None
    assert (
        runner.profile_for(
            {"labels": ["self-hosted", "Linux", "X64", "klukai-amarillo-heavy", "gpu"]}
        )
        is None
    )


def test_a_queued_job_gets_one_fresh_guest(env):
    make, state, tmp = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    controller.tick()
    assert len(host.domains) == 1
    name = next(iter(host.domains))
    assert runner.NAME_RE.fullmatch(name)
    job_dir = runner.JOBS / name
    assert (job_dir / "disk.qcow2").exists() and (job_dir / "seed.iso").exists()
    qemu_img = next(c for c in host.calls if c[0] == "qemu-img")
    assert qemu_img[qemu_img.index("-b") + 1] == str(runner.BASE)
    controller.tick()  # same queued job, guest still warming: no second guest
    assert len(host.domains) == 1
    assert json.loads((tmp / "state.json").read_text())[0]["name"] == name


def test_finished_guest_is_torn_down_with_its_registration(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-small")])
    controller.tick()
    name = next(iter(host.domains))
    github.jobs = []
    host.domains.pop(name)  # guest powered off after its job
    controller.tick()
    assert controller.guests == {}
    assert not (runner.JOBS / name).exists()
    assert github.deleted == [100]


def test_cpu_budget_never_exceeds_eight_threads(env):
    make, state, _ = env
    jobs = [
        job("klukai-amarillo-heavy", "2026-10-19T10:00:00Z"),
        job("klukai-amarillo-small", "2026-10-19T10:00:01Z"),
    ]
    controller, github, host = make(jobs)
    controller.tick()
    assert [g.profile for g in controller.guests.values()] == [SMALL]
    assert sum(g.profile.vcpus for g in controller.guests.values()) <= runner.MAX_VCPUS


def test_small_jobs_share_the_budget(env):
    make, state, _ = env
    jobs = [job("klukai-amarillo-small", f"2026-10-19T10:00:0{i}Z") for i in range(5)]
    controller, github, host = make(jobs)
    state["mem"] = 40000
    controller.tick()
    assert len(controller.guests) == 4  # 4 x 2 threads = 8
    assert {g.slot for g in controller.guests.values()} == {0, 1, 2, 3}


@pytest.mark.parametrize(
    "temp,mem,reason", [(62.0, 17000, "warm"), (45.0, 8192 + 4095, "memory")]
)
def test_admission_waits_for_heat_and_memory(env, temp, mem, reason):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    state["temp"], state["mem"] = temp, mem
    controller.tick()
    assert host.domains == {}
    assert github.registered == {}


def test_nothing_starts_without_the_guard_network_or_base(env):
    make, state, _ = env
    for breakage in ("guard", "network", "base"):
        controller, github, host = make([job("klukai-amarillo-heavy")])
        if breakage == "guard":
            host.guard = False
        elif breakage == "network":
            host.network_active = False
        else:
            runner.BASE.unlink()
        controller.tick()
        assert host.domains == {}, breakage
        if breakage == "base":
            runner.BASE.write_bytes(b"base")


def test_guests_pause_when_hot_and_resume_when_cool(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    controller.tick()
    name = next(iter(host.domains))
    state["temp"] = 68.0
    controller.tick()
    assert host.domains[name] == "paused" and controller.guests[name].paused
    state["temp"] = 63.0  # between thresholds: stays paused
    controller.tick()
    assert host.domains[name] == "paused"
    state["temp"] = 59.0
    controller.tick()
    assert host.domains[name] == "running" and not controller.guests[name].paused


def test_a_stuck_guest_is_destroyed_after_the_deadline(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    controller.tick()
    github.jobs = []
    state["now"] += runner.MAX_JOB_SECONDS + 1
    controller.tick()
    assert host.domains == {} and controller.guests == {}


def test_unknown_klukai_guests_and_dirs_are_reaped(env):
    make, state, _ = env
    controller, github, host = make()
    host.domains["klukai-ci-0123456789ab"] = "running"
    host.domains["lmstudio-sandbox"] = "running"
    stray = runner.JOBS / "klukai-ci-ba9876543210"
    stray.mkdir(parents=True)
    controller.tick()
    assert "klukai-ci-0123456789ab" not in host.domains
    assert host.domains["lmstudio-sandbox"] == "running"  # never touched
    assert not stray.exists()


def test_a_failed_launch_leaves_no_registration_or_files(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    original = host.__call__

    def failing(argv, **kw):
        if argv[0] == "virsh" and argv[4] == "create":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return original(argv, **kw)

    controller._run = failing
    with pytest.raises(runner.RunnerError):
        controller.tick()
    assert github.deleted == [100]
    assert not any(runner.JOBS.iterdir())


def test_state_survives_a_controller_restart(env):
    make, state, tmp = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    controller.tick()
    again = runner.Controller(
        github,
        run=host,
        temp=lambda: 45.0,
        memory=lambda: 17000,
        clock=lambda: state["now"],
        state_file=tmp / "state.json",
    )
    assert set(again.guests) == set(controller.guests)


def test_guest_definition_is_isolated_and_bounded(tmp_path):
    xml = ET.fromstring(runner.domain_xml("klukai-ci-0123456789ab", HEAVY, 1, tmp_path))
    assert xml.findtext("vcpu") == "8" and xml.findtext("memory") == "8192"
    assert xml.find("memtune/hard_limit").text == "9216"
    nic = xml.find("devices/interface")
    assert (
        nic.get("type") == "network"
        and nic.find("source").get("network") == "klukai-ci"
    )
    assert nic.find("port").get("isolated") == "yes"
    shares = xml.findall("devices/filesystem")
    assert len(shares) == 1 and shares[0].find("source").get("dir") == str(
        runner.ARTIFACTS
    )
    assert all(
        xml.findtext(e) == "destroy" for e in ("on_poweroff", "on_reboot", "on_crash")
    )
    with pytest.raises(runner.RunnerError):
        runner.domain_xml("../evil", HEAVY, 0, tmp_path)


def test_guest_network_is_static_with_public_dns():
    config = json.loads(runner.network_config(2))["ethernets"]["ci0"]
    assert config["addresses"] == ["10.231.0.12/24"] and config["dhcp4"] is False
    assert config["nameservers"]["addresses"] == ["1.1.1.1", "9.9.9.9"]


def test_jit_config_reaches_only_the_seed():
    data = runner.user_data("SECRET")
    assert data.startswith("#cloud-config\n")
    files = json.loads(data.split("\n", 1)[1])["write_files"]
    assert files == [
        {
            "path": "/run/klukai-ci/jit",
            "permissions": "0600",
            "owner": "root:root",
            "content": "SECRET",
        }
    ]


def test_temperature_and_memory_readers(tmp_path):
    hw = tmp_path / "hwmon"
    (hw / "hwmon0").mkdir(parents=True)
    (hw / "hwmon0" / "name").write_text("nvme\n")
    (hw / "hwmon3").mkdir()
    (hw / "hwmon3" / "name").write_text("coretemp\n")
    (hw / "hwmon3" / "temp1_input").write_text("57000\n")
    assert runner.cpu_temp_c(hw) == 57.0
    assert runner.cpu_temp_c(tmp_path / "none") == float("inf")  # unknown = too hot
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 48000000 kB\nMemAvailable:   17825792 kB\n")
    assert runner.mem_available_mib(meminfo) == 17408


def test_guest_written_files_go_to_the_tree_owner_without_setuid(tmp_path, monkeypatch):
    tree = tmp_path / "ci-artifacts"
    (tree / "rev" / "run").mkdir(parents=True)
    exe = tree / "rev" / "run" / "tool"
    exe.write_bytes(b"x")
    exe.chmod(0o4755)
    (tree / "rev" / "link").symlink_to("/etc/passwd")
    chowned = []
    monkeypatch.setattr(runner.os, "lchown", lambda p, u, g: chowned.append(p))
    real_lstat = runner.os.lstat

    def lstat(path):  # pretend the guest wrote everything as root
        info = real_lstat(path)
        return runner.os.stat_result(
            (
                info.st_mode,
                info.st_ino,
                info.st_dev,
                info.st_nlink,
                0,
                0,
                *tuple(info)[6:10],
            )
        )

    monkeypatch.setattr(runner.os, "lstat", lstat)
    assert runner.normalize_ownership(tree) == 4
    assert exe.stat().st_mode & 0o6000 == 0
    assert str(tree / "rev" / "link") in chowned  # the link itself, never its target
    assert (tree / "rev" / "link").is_symlink()


def test_a_guest_that_never_gets_a_job_is_reaped(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    controller.tick()
    name = next(iter(host.domains))
    state["now"] += runner.IDLE_SECONDS - 1
    controller.tick()
    assert name in host.domains  # still within its chance to get the job
    github.unknown.add(100)  # GitHub cannot say: never reap on a guess
    state["now"] += 10
    controller.tick()
    assert name in host.domains
    github.unknown.clear()
    github.jobs = []
    controller.tick()
    assert name not in host.domains and controller.guests == {}


def test_a_busy_guest_is_never_reaped_for_idleness(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-heavy")])
    controller.tick()
    name = next(iter(host.domains))
    github.busy.add(100)
    state["now"] += runner.IDLE_SECONDS * 3
    controller.tick()
    assert name in host.domains


def test_one_unreadable_run_does_not_hide_the_others():
    calls = []

    def fake_call(method, path, body=None):
        calls.append(path)
        if path.startswith(f"/repos/{runner.REPO}/actions/runs?status=queued"):
            return {"workflow_runs": [{"id": 1}, {"id": 2}]}
        if path.startswith(f"/repos/{runner.REPO}/actions/runs?status=in_progress"):
            return {"workflow_runs": []}
        if "/runs/1/jobs" in path:
            raise runner.RunnerError("GitHub GET /runs/1/jobs: HTTP 502")
        return {
            "jobs": [
                job("klukai-amarillo-small"),
                {"status": "completed", "labels": []},
            ]
        }

    github = runner.GitHub("token")
    github.call = fake_call
    jobs = github.queued_jobs()
    assert len(jobs) == 1 and runner.profile_for(jobs[0]) is SMALL


def test_quick_checks_are_admitted_before_exclusive_work(env):
    make, state, _ = env
    controller, github, host = make(
        [job("klukai-amarillo-heavy"), job("klukai-amarillo-small")]
    )
    assert controller.demand()[0] is SMALL
    controller.tick()
    assert [g.profile for g in controller.guests.values()] == [SMALL]


def test_medium_jobs_pack_with_quick_checks_without_changing_host_limits(env):
    make, state, _ = env
    state["mem"] = 40000
    controller, github, host = make(
        [
            job("klukai-amarillo-medium"),
            job("klukai-amarillo-small"),
            job("klukai-amarillo-small"),
            job("klukai-amarillo-heavy"),
        ]
    )
    controller.tick()
    assert sorted(g.profile.vcpus for g in controller.guests.values()) == [2, 2, 4]
    assert runner.MAX_VCPUS == 8
    assert runner.MEMORY_HEADROOM_MIB == 4096
    assert (runner.START_BELOW_C, runner.PAUSE_AT_C, runner.RESUME_BELOW_C) == (
        62,
        68,
        60,
    )


def test_blocked_profile_does_not_hide_admissible_work(env, monkeypatch):
    make, state, _ = env
    controller, github, host = make()
    monkeypatch.setattr(controller, "demand", lambda: [HEAVY, SMALL])
    original = controller.admit
    monkeypatch.setattr(
        controller, "admit", lambda p: "blocked heavy" if p is HEAVY else original(p)
    )
    controller.tick()
    assert [g.profile for g in controller.guests.values()] == [SMALL]


def test_frontend_keeps_eight_gib_but_uses_only_four_threads(env):
    profile = runner.profile_for(job("klukai-amarillo-frontend"))
    assert profile is not None
    assert (profile.vcpus, profile.memory_mib) == (4, 8192)


def test_long_frontend_overlaps_medium_jobs_instead_of_waiting_at_tail(env):
    make, state, _ = env
    controller, github, host = make(
        [
            job("klukai-amarillo-medium"),
            job("klukai-amarillo-frontend"),
        ]
    )
    assert controller.demand()[0].label == "klukai-amarillo-frontend"
    controller.tick()
    assert sum(g.profile.vcpus for g in controller.guests.values()) == 8


def test_idle_cache_pruning_keeps_recent_and_published_exports(tmp_path):
    import os

    root = tmp_path / "artifacts"
    caches = []
    for number in range(5):
        path = root / ("a" * 40) / f"{number}-1" / "release-buildkit"
        path.mkdir(parents=True)
        (path / "index.json").write_text("{}")
        os.utime(path, (number, number))
        caches.append(path)
    (root / "cache").mkdir()
    (root / "cache" / "release-buildkit.json").write_text(
        json.dumps({"directory": str(caches[0])})
    )
    assert runner.prune_build_caches(root, now=10000) == 2
    assert [path.exists() for path in caches] == [True, False, False, True, True]


def test_serial_suites_fit_two_alongside_frontend_and_keep_build_priority(env):
    make, state, _ = env
    state["mem"] = 40000
    controller, github, host = make(
        [
            job("klukai-amarillo-frontend"),
            job("klukai-amarillo-serial"),
            job("klukai-amarillo-serial"),
        ]
    )
    controller.tick()
    assert sorted(g.profile.vcpus for g in controller.guests.values()) == [2, 2, 4]
    serial = runner.profile_for(job("klukai-amarillo-serial"))
    assert serial.memory_mib == 4096
    controller, github, host = make(
        [
            job("klukai-amarillo-serial"),
            job("klukai-amarillo-build"),
        ]
    )
    assert controller.demand()[0].label == "klukai-amarillo-build"


def test_busy_young_guest_does_not_cover_another_queued_job(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-medium")])
    controller.tick()
    github.busy.add(100)
    # The first job is now assigned; queued_jobs still has one waiting job.
    # Replacing it with another job models GitHub's authoritative queued list.
    github.jobs = [job("klukai-amarillo-medium", "2026-10-19T10:00:01Z")]
    controller.tick()
    assert len(controller.guests) == 2
    assert sum(g.profile.vcpus for g in controller.guests.values()) == 8


def test_run_transition_between_status_queries_cannot_duplicate_queued_jobs():
    github = runner.GitHub("token")

    def api(method, path, body=None):
        if "/jobs?" in path:
            return {"jobs": [job("klukai-amarillo-small", id=7)]}
        return {"workflow_runs": [{"id": 1}]}

    github.call = api
    assert [row["id"] for row in github.queued_jobs()] == [7]


def test_new_busy_transition_is_credited_until_jobs_snapshot_contains_assignment(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-medium")])
    controller.tick()
    github.busy.add(100)
    # The earlier jobs snapshot still says queued, but runner API now says busy.
    github.assigned_runners = set()
    controller.tick()
    assert len(controller.guests) == 1
    # A later coherent snapshot contains that assignment plus a DIFFERENT queue job.
    github.assigned_runners = {100}
    github.jobs = [job("klukai-amarillo-medium", id=8)]
    controller.tick()
    assert len(controller.guests) == 2


def test_job_snapshot_records_consumed_jit_runner_ids():
    github = runner.GitHub("token")
    calls = []

    def api(method, path, body=None):
        calls.append(path)
        if "/jobs?" in path:
            return {
                "jobs": [
                    job("klukai-amarillo-small", id=7),
                    job(
                        "klukai-amarillo-small",
                        id=8,
                        status="in_progress",
                        runner_id=101,
                    ),
                    job(
                        "klukai-amarillo-small", id=9, status="completed", runner_id=102
                    ),
                ]
            }
        return {"workflow_runs": [{"id": 1}]}

    github.call = api
    assert [row["id"] for row in github.queued_jobs()] == [7]
    assert github.assigned_runners == {101, 102}
    assert sum("/jobs?" in path for path in calls) == 1


def test_a_surplus_idle_guest_frees_capacity_for_a_starving_job(env):
    """2026-10-22: five runs queued five build jobs; GitHub handed them out
    faster than the queue snapshot showed, two build guests got no job and
    held the whole CPU budget for 15 minutes while every serial and medium job
    waited. An idle guest with nothing queued for its kind now yields."""
    make, state, _ = env
    controller, github, host = make(
        [job("klukai-amarillo-build", "2026-10-22T09:00:00Z"),
         job("klukai-amarillo-build", "2026-10-22T09:00:01Z")])
    controller.tick()
    assert len(host.domains) == 2  # 4 + 4 threads: the budget is full
    github.busy.add(100)  # one guest took a build job; nothing left for the other
    github.jobs = [job("klukai-amarillo-serial", "2026-10-22T09:01:00Z")]
    state["now"] += runner.SURPLUS_GRACE_SECONDS - 1
    controller.tick()
    assert len(host.domains) == 2  # still inside its chance to get a job
    state["now"] += 2
    controller.tick()
    labels = sorted(g.profile.label for g in controller.guests.values())
    assert labels == ["klukai-amarillo-build", "klukai-amarillo-serial"]
    assert 101 in github.deleted and 100 not in github.deleted  # the busy guest is untouched


def test_a_surplus_guest_stays_when_nothing_waits_or_github_cannot_say(env):
    make, state, _ = env
    controller, github, host = make(
        [job("klukai-amarillo-build", "2026-10-22T09:00:00Z"),
         job("klukai-amarillo-build", "2026-10-22T09:00:01Z")])
    controller.tick()
    github.busy.add(100)
    github.jobs = []  # nothing starving: no reason to tear it down early
    state["now"] += runner.SURPLUS_GRACE_SECONDS + 5
    controller.tick()
    assert len(host.domains) == 2
    github.jobs = [job("klukai-amarillo-serial", "2026-10-22T09:05:00Z")]
    github.unknown.add(101)  # GitHub cannot say whether it is busy: never on a guess
    controller.tick()
    assert len(host.domains) == 2 and 101 not in github.deleted


def test_an_idle_guest_still_covers_a_queued_job_of_its_kind(env):
    make, state, _ = env
    controller, github, host = make([job("klukai-amarillo-build", "2026-10-22T09:00:00Z")])
    controller.tick()
    github.jobs = [job("klukai-amarillo-build", "2026-10-22T09:00:00Z"),
                   job("klukai-amarillo-serial", "2026-10-22T09:01:00Z")]
    state["now"] += runner.SURPLUS_GRACE_SECONDS + 5
    controller.tick()
    assert 100 not in github.deleted  # its build job is still queued for it
