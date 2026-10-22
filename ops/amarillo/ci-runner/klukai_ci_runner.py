#!/usr/bin/env python3
"""On-demand, one-job-per-VM GitHub Actions runners on amarillo, for Klukai only.

Owner decision 2026-10-19: Klukai CI stops depending on Dominus. Each queued
Klukai job whose labels are exactly ``self-hosted, Linux, X64`` plus one of the
profile labels below gets a fresh VM, booted from a fixed base image with a
throwaway copy-on-write disk and a just-in-time (single-use) runner
registration. The guest powers off after its one job; the controller then
deletes the disk, the seed and any leftover registration.

Limits (owner): CI uses at most 8 CPU threads in total; a VM starts only while
amarillo keeps 4 GiB of memory free beyond it and the CPU package is below
62 C. Running VMs are paused at 68 C (amarillo's limit is 70 C) and resumed
below 60 C (a host config sets its own CPU's limits). Production runs on this host, so guests sit on the firewalled
``klukai-ci`` network (internet only) and share nothing with the host except
the CI evidence tree, mounted read-write at /opt/klukai-ci/artifacts.

Runs as root from klukai-ci-runner.service. The GitHub token comes from the
systemd credential ``github-token``; it and the JIT configs are never logged.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Callable
import xml.etree.ElementTree as ET

REPO = "Al-Sarraf-Tech/klukai"
API = "https://api.github.com"
BASE_LABELS = ("self-hosted", "Linux", "X64")


@dataclass(frozen=True)
class Profile:
    label: str
    vcpus: int
    memory_mib: int

    @property
    def kind(self) -> str:
        return self.label.rsplit("-", 1)[1]


# Every pool offers the same job shapes under its own labels (klukai-<pool>-<kind>).
KINDS = {
    "heavy": (8, 8192),
    "small": (2, 3072),
    "medium": (4, 4096),
    "build": (4, 4096),
    "serial": (2, 4096),
    "frontend": (4, 8192),
}


def make_profiles(pool: str) -> dict[str, Profile]:
    return {
        f"klukai-{pool}-{kind}": Profile(f"klukai-{pool}-{kind}", vcpus, memory)
        for kind, (vcpus, memory) in KINDS.items()
    }


# Owner, 2026-10-22: Klukai CI runs on Dominus when it is online, else amarillo.
# amarillo is the default pool and also serves a Dominus job that has waited
# STEAL_AFTER_SECONDS (Dominus went off, is gaming, or is full). A host config
# (/etc/klukai-ci/host.json, see configure()) turns this module into Dominus's pool.
POOL = "amarillo"
STEAL_FROM: str | None = "dominus"
STEAL_AFTER_SECONDS = 180
PROFILES = make_profiles(POOL)
STEAL_PROFILES = make_profiles(STEAL_FROM) if STEAL_FROM else {}
MAX_MEMORY_MIB: int | None = None  # a pool-wide guest memory budget (Dominus)
SUBNET = "10.231.0"
TEMP_SENSOR = "coretemp"  # k10temp on AMD hosts
TOKEN_SOURCE = "credential"  # or "gh-cli": the owner's GitHub CLI login, read at start
HEARTBEAT_VARIABLE: str | None = None  # repo variable the CI router reads
GAMING_MARKER: Path | None = None  # no new guests while it exists
QUIET_HOURS: tuple[int, int] | None = None  # local [start, end) hours: no new guests
RECORD_DAYS: int | None = None  # a pool whose records amarillo pulls keeps only this many days
HOST_CONFIG = Path("/etc/klukai-ci/host.json")
# Owner, 2026-10-22: 12 of amarillo's 20 threads (no thermal events at 8 in three
# days; MEMORY_HEADROOM_MIB still protects production's memory).
MAX_VCPUS = 12
MEMORY_HEADROOM_MIB = 4096
START_BELOW_C = 62.0
PAUSE_AT_C = 68.0
RESUME_BELOW_C = 60.0
MAX_JOB_SECONDS = 4 * 3600
IDLE_SECONDS = 900  # an idle runner this old never got a job: free its capacity
# A JIT runner gets a queued job of its label within seconds. Idle this long with
# nothing of its kind queued, it is surplus: it yields its CPUs to a waiting job
# of another kind instead of holding them until IDLE_SECONDS.
SURPLUS_GRACE_SECONDS = 120
SLOTS = 4  # static guest addresses <SUBNET>.10-13

ROOT = Path("/var/lib/libvirt/images/klukai-ci")
BASE = ROOT / "base" / "base.qcow2"
BASE_DIGEST = ROOT / "base" / "base.qcow2.sha256"
JOBS = ROOT / "jobs"
# virtlogd may only write virt_log_t paths under SELinux.
CONSOLE_LOGS = Path("/var/log/libvirt/qemu")
CONSOLE_LOG_DAYS = 3
ARTIFACTS = Path("/docker/klukai/ci-artifacts")
GUARD_TABLE = ("inet", "klukai_ci_guard")
NETWORK = "klukai-ci"
BRIDGE = "kci-br0"
NAME_RE = re.compile(r"klukai-ci-[0-9a-f]{12}")
VIRSH = ["virsh", "-q", "-c", "qemu:///system"]

log = logging.getLogger("klukai-ci-runner")


class RunnerError(RuntimeError):
    """A safe diagnostic: never carries tokens, JIT configs or response bodies."""


# ── Host readings ──────────────────────────────────────────────────────────


def cpu_temp_c(hwmon: Path = Path("/sys/class/hwmon")) -> float:
    """CPU package temperature (coretemp, or k10temp's Tctl); unknown reads as too hot."""
    for node in sorted(hwmon.glob("hwmon*")):
        try:
            if (node / "name").read_text().strip() != TEMP_SENSOR:
                continue
            return int((node / "temp1_input").read_text()) / 1000.0
        except (OSError, ValueError):
            continue
    return float("inf")


def mem_available_mib(meminfo: Path = Path("/proc/meminfo")) -> int:
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RunnerError("MemAvailable missing")


# ── GitHub ─────────────────────────────────────────────────────────────────


class GitHub:
    def __init__(self, token: str, opener: Callable[..., Any] = urllib.request.urlopen):
        self._token, self._open = token, opener
        self.assigned_runners: set[int] = set()

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        request = urllib.request.Request(
            API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise RunnerError(
                f"GitHub {method} {path.split('?')[0]}: HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RunnerError(
                f"GitHub {method} {path.split('?')[0]}: unreachable"
            ) from None
        return json.loads(raw) if raw else None

    def queued_jobs(self) -> list[dict[str, Any]]:
        """Queued jobs of Klukai's queued or in-progress runs, oldest first."""
        jobs = []
        assigned: set[int] = set()
        seen_runs: set[int] = set()
        for status in ("queued", "in_progress"):
            runs = self.call(
                "GET", f"/repos/{REPO}/actions/runs?status={status}&per_page=30"
            )
            for run in runs.get("workflow_runs", []):
                run_id = int(run["id"])
                if run_id in seen_runs:
                    continue  # a run can transition between the two API reads
                seen_runs.add(run_id)
                try:
                    listing = self.call(
                        "GET",
                        f"/repos/{REPO}/actions/runs/{int(run['id'])}/jobs?filter=latest&per_page=100",
                    )
                except RunnerError as error:
                    # One unreadable run (GitHub has returned 502 for a wedged
                    # one) must not stop every other run's jobs from starting.
                    log.warning("skipping run %s: %s", int(run["id"]), error)
                    continue
                rows = listing.get("jobs", [])
                jobs += [j for j in rows if j.get("status") == "queued"]
                assigned.update(
                    int(j["runner_id"])
                    for j in rows
                    if j.get("status") != "queued" and j.get("runner_id")
                )
        self.assigned_runners = assigned
        unique = {job["id"]: job for job in jobs}
        return sorted(
            unique.values(), key=lambda j: (j.get("created_at") or "", j.get("id") or 0)
        )

    def jit_config(self, name: str, profile: Profile) -> tuple[int, str]:
        result = self.call(
            "POST",
            f"/repos/{REPO}/actions/runners/generate-jitconfig",
            {
                "name": name,
                "runner_group_id": 1,
                "labels": [*BASE_LABELS, profile.label],
                "work_folder": "_work",
            },
        )
        runner, config = result.get("runner") or {}, result.get("encoded_jit_config")
        if (
            not isinstance(config, str)
            or not config
            or not isinstance(runner.get("id"), int)
        ):
            raise RunnerError("GitHub returned no JIT configuration")
        return runner["id"], config

    def set_variable(self, name: str, value: str) -> None:
        """Create or update a repository Actions variable (the pool heartbeat)."""
        try:
            self.call("PATCH", f"/repos/{REPO}/actions/variables/{name}", {"name": name, "value": value})
        except RunnerError as error:
            if "HTTP 404" not in str(error):
                raise
            self.call("POST", f"/repos/{REPO}/actions/variables", {"name": name, "value": value})

    def delete_runner(self, runner_id: int) -> None:
        try:
            self.call("DELETE", f"/repos/{REPO}/actions/runners/{int(runner_id)}")
        except RunnerError as error:
            if "HTTP 404" not in str(error):  # JIT runners remove themselves
                raise

    def runner_busy(self, runner_id: int) -> bool | None:
        """True/False from GitHub; None when GitHub cannot say (never reap on None)."""
        try:
            runner = self.call("GET", f"/repos/{REPO}/actions/runners/{int(runner_id)}")
        except RunnerError:
            return None
        return bool(runner.get("busy"))


def configure(doc: dict[str, Any]) -> None:
    """Turn this module into one host's pool from its host config."""
    global POOL, STEAL_FROM, PROFILES, STEAL_PROFILES, MAX_VCPUS, MAX_MEMORY_MIB, SLOTS
    global SUBNET, ARTIFACTS, TEMP_SENSOR, TOKEN_SOURCE, HEARTBEAT_VARIABLE
    global GAMING_MARKER, QUIET_HOURS, RECORD_DAYS, START_BELOW_C, PAUSE_AT_C, RESUME_BELOW_C
    known = {"pool", "steal_from", "max_vcpus", "max_memory_mib", "slots", "subnet",
             "artifacts", "temp_sensor", "token_source", "heartbeat_variable",
             "gaming_marker", "quiet_hours", "record_days",
             "start_below_c", "pause_at_c", "resume_below_c"}
    if set(doc) - known:
        raise RunnerError(f"unknown host settings: {sorted(set(doc) - known)}")
    pool = doc.get("pool", POOL)
    steal = doc.get("steal_from", STEAL_FROM)
    if not re.fullmatch(r"[a-z]+", pool) or (steal is not None and not re.fullmatch(r"[a-z]+", steal)):
        raise RunnerError("pool names are lowercase letters")
    slots = int(doc.get("slots", SLOTS))
    if not 1 <= slots <= 16:
        raise RunnerError("slots must be 1-16")
    subnet = doc.get("subnet", SUBNET)
    if not re.fullmatch(r"10\.\d{1,3}\.\d{1,3}", subnet):
        raise RunnerError("subnet must be a private 10.x.y /24 prefix")
    if doc.get("token_source", TOKEN_SOURCE) not in ("credential", "gh-cli"):
        raise RunnerError("token_source must be credential or gh-cli")
    # Each CPU has its own normal range: a Ryzen 7950X runs up to 95 C by design.
    start = float(doc.get("start_below_c", START_BELOW_C))
    pause = float(doc.get("pause_at_c", PAUSE_AT_C))
    resume = float(doc.get("resume_below_c", RESUME_BELOW_C))
    if not (resume < pause and start < pause <= 95):
        raise RunnerError("thermal limits need resume and start below pause, pause at most 95 C")
    START_BELOW_C, PAUSE_AT_C, RESUME_BELOW_C = start, pause, resume
    POOL, STEAL_FROM, SLOTS, SUBNET = pool, steal, slots, subnet
    PROFILES = make_profiles(POOL)
    STEAL_PROFILES = make_profiles(STEAL_FROM) if STEAL_FROM and STEAL_FROM != POOL else {}
    MAX_VCPUS = int(doc.get("max_vcpus", MAX_VCPUS))
    memory = doc.get("max_memory_mib", MAX_MEMORY_MIB)
    MAX_MEMORY_MIB = int(memory) if memory is not None else None
    ARTIFACTS = Path(doc.get("artifacts", str(ARTIFACTS)))
    TEMP_SENSOR = doc.get("temp_sensor", TEMP_SENSOR)
    TOKEN_SOURCE = doc.get("token_source", TOKEN_SOURCE)
    HEARTBEAT_VARIABLE = doc.get("heartbeat_variable", HEARTBEAT_VARIABLE)
    marker = doc.get("gaming_marker")
    GAMING_MARKER = Path(marker) if marker else GAMING_MARKER
    hours = doc.get("quiet_hours")
    QUIET_HOURS = (int(hours[0]), int(hours[1])) if hours else QUIET_HOURS
    days = doc.get("record_days", RECORD_DAYS)
    RECORD_DAYS = int(days) if days is not None else None


def profile_for(job: dict[str, Any], profiles: dict[str, Profile] | None = None) -> Profile | None:
    """Only exact label sets are served: never another pool's or a light-pool job."""
    labels = job.get("labels") or []
    for profile in (PROFILES if profiles is None else profiles).values():
        if sorted(labels) == sorted([*BASE_LABELS, profile.label]):
            return profile
    return None


# ── Guest definition ───────────────────────────────────────────────────────


def mac_for(slot: int) -> str:
    return f"52:54:00:6b:63:{0x10 + slot:02x}"


def address_for(slot: int) -> str:
    return f"{SUBNET}.{10 + slot}"


def domain_xml(name: str, profile: Profile, slot: int, job_dir: Path) -> str:
    if not NAME_RE.fullmatch(name) or not 0 <= slot < SLOTS:
        raise RunnerError("invalid guest identity")
    root = ET.Element("domain", type="kvm")
    ET.SubElement(root, "name").text = name
    ET.SubElement(root, "memory", unit="MiB").text = str(profile.memory_mib)
    ET.SubElement(root, "vcpu", placement="static").text = str(profile.vcpus)
    memtune = ET.SubElement(root, "memtune")
    ET.SubElement(memtune, "hard_limit", unit="MiB").text = str(
        profile.memory_mib + 1024
    )
    backing = ET.SubElement(root, "memoryBacking")  # virtiofs needs shared memory
    ET.SubElement(backing, "source", type="memfd")
    ET.SubElement(backing, "access", mode="shared")
    os_node = ET.SubElement(root, "os")
    ET.SubElement(os_node, "type", arch="x86_64", machine="q35").text = "hvm"
    ET.SubElement(os_node, "boot", dev="hd")
    features = ET.SubElement(root, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(root, "cpu", mode="host-passthrough", migratable="off")
    for event in ("on_poweroff", "on_reboot", "on_crash"):
        ET.SubElement(root, event).text = "destroy"
    devices = ET.SubElement(root, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(
        disk, "driver", name="qemu", type="qcow2", cache="none", discard="unmap"
    )
    ET.SubElement(disk, "source", file=str(job_dir / "disk.qcow2"))
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    seed = ET.SubElement(devices, "disk", type="file", device="cdrom")
    ET.SubElement(seed, "driver", name="qemu", type="raw")
    ET.SubElement(seed, "source", file=str(job_dir / "seed.iso"))
    ET.SubElement(seed, "target", dev="sda", bus="sata")
    ET.SubElement(seed, "readonly")
    share = ET.SubElement(devices, "filesystem", type="mount", accessmode="passthrough")
    ET.SubElement(share, "driver", type="virtiofs")
    ET.SubElement(share, "source", dir=str(ARTIFACTS))
    ET.SubElement(share, "target", dir="klukai-artifacts")
    nic = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(nic, "mac", address=mac_for(slot))
    ET.SubElement(nic, "source", network=NETWORK)
    ET.SubElement(nic, "model", type="virtio")
    ET.SubElement(
        nic, "port", isolated="yes"
    )  # concurrent guests cannot see each other
    serial = ET.SubElement(devices, "serial", type="file")
    ET.SubElement(serial, "source", path=str(CONSOLE_LOGS / f"{name}.console.log"))
    ET.SubElement(serial, "target", port="0")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")
    ET.SubElement(devices, "memballoon", model="none")
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


def network_config(slot: int) -> str:
    """NoCloud network-config v2 (JSON is valid YAML): static, public DNS only."""
    return json.dumps(
        {
            "version": 2,
            "ethernets": {
                "ci0": {
                    "match": {"macaddress": mac_for(slot)},
                    "set-name": "ci0",
                    "dhcp4": False,
                    "dhcp6": False,
                    "accept-ra": False,
                    "addresses": [f"{address_for(slot)}/24"],
                    "routes": [{"to": "0.0.0.0/0", "via": f"{SUBNET}.1"}],
                    "nameservers": {"addresses": ["1.1.1.1", "9.9.9.9"]},
                }
            },
        }
    )


def user_data(jit_config: str) -> str:
    """The guest's klukai-ci-job.service reads the JIT file once and deletes it."""
    return "#cloud-config\n" + json.dumps(
        {
            "write_files": [
                {
                    "path": "/run/klukai-ci/jit",
                    "permissions": "0600",
                    "owner": "root:root",
                    "content": jit_config,
                }
            ],
            "runcmd": [["systemctl", "start", "--no-block", "klukai-ci-job.service"]],
        }
    )


# ── Controller ─────────────────────────────────────────────────────────────


@dataclass
class Guest:
    name: str
    profile: Profile
    slot: int
    runner_id: int
    started: float
    paused: bool = False


class Controller:
    def __init__(
        self,
        github: GitHub,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        temp: Callable[[], float] = cpu_temp_c,
        memory: Callable[[], int] = mem_available_mib,
        clock: Callable[[], float] = time.time,
        state_file: Path = Path("/var/lib/klukai-ci/state.json"),
    ) -> None:
        self.github, self._run, self.temp, self.memory, self.clock = (
            github,
            run,
            temp,
            memory,
            clock,
        )
        self.state_file = state_file
        self.first_seen: dict[int, float] = {}  # other-pool jobs we might take over
        self._beat: tuple[str, float] = ("", 0.0)
        self.guests: dict[str, Guest] = self._load()

    # state survives a controller restart so leftovers are always reaped
    def _load(self) -> dict[str, Guest]:
        try:
            raw = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}
        guests = {}
        for item in raw:
            known = {**STEAL_PROFILES, **PROFILES}
            if (
                NAME_RE.fullmatch(item.get("name", ""))
                and item.get("profile") in known
            ):
                guests[item["name"]] = Guest(
                    item["name"],
                    known[item["profile"]],
                    int(item["slot"]),
                    int(item["runner_id"]),
                    float(item["started"]),
                    bool(item.get("paused")),
                )
        return guests

    def _save(self) -> None:
        self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = [
            {
                "name": g.name,
                "profile": g.profile.label,
                "slot": g.slot,
                "runner_id": g.runner_id,
                "started": g.started,
                "paused": g.paused,
            }
            for g in self.guests.values()
        ]
        fd, tmp = tempfile.mkstemp(dir=self.state_file.parent)
        with os.fdopen(fd, "w") as out:
            json.dump(payload, out)
        os.replace(tmp, self.state_file)

    def sh(self, *argv: str, check: bool = True, timeout: float = 120) -> str:
        result = self._run(list(argv), capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RunnerError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed")
        return result.stdout

    def running_domains(self) -> dict[str, str]:
        out = self.sh(*VIRSH, "list", "--all", "--name", check=False)
        names = [n for n in out.split() if NAME_RE.fullmatch(n)]
        return {n: self.sh(*VIRSH, "domstate", n, check=False).strip() for n in names}

    def ready(self) -> str | None:
        """Why nothing may start right now, or None."""
        if not BASE.is_file() or not BASE_DIGEST.is_file():
            return "base image missing"
        if self.sh("nft", "list", "table", *GUARD_TABLE, check=False) == "":
            return "network guard missing"
        info = dict(
            (key.strip(), value.strip())
            for key, _, value in (
                line.partition(":")
                for line in self.sh(
                    *VIRSH, "net-info", NETWORK, check=False
                ).splitlines()
            )
        )
        if info.get("Active") != "yes" or info.get("Bridge") != BRIDGE:
            return "klukai-ci network inactive"
        if not ARTIFACTS.is_dir():
            return "artifact tree missing"
        return None

    def blocked(self) -> str | None:
        """Owner guards on a shared desktop: gaming and the overnight window."""
        if GAMING_MARKER is not None and GAMING_MARKER.exists():
            return "a game is running"
        if QUIET_HOURS is not None:
            hour = datetime.fromtimestamp(self.clock()).hour
            start, end = QUIET_HOURS
            if (start <= hour < end) if start < end else (hour >= start or hour < end):
                return "quiet hours"
        return None

    def heartbeat(self, healthy: bool) -> None:
        """Tell the CI router this pool can take new runs (epoch) or not ("0")."""
        if not HEARTBEAT_VARIABLE:
            return
        now = self.clock()
        value = str(int(now)) if healthy else "0"
        last, at = self._beat
        if (value == "0" and last == "0") or (healthy and last not in ("", "0") and now - at < 60):
            return
        try:
            self.github.set_variable(HEARTBEAT_VARIABLE, value)
            self._beat = (value, now)
        except RunnerError as error:
            log.warning("heartbeat not written: %s", error)

    def admit(self, profile: Profile) -> str | None:
        # The perf VM measures p99 latency: it never shares the host with CI.
        if profile.kind == "heavy" and self.guests:
            return "perf runs alone"
        if any(g.profile.kind == "heavy" for g in self.guests.values()):
            return "perf is measuring"
        used = sum(g.profile.vcpus for g in self.guests.values())
        if used + profile.vcpus > MAX_VCPUS:
            return "CPU budget in use"
        if MAX_MEMORY_MIB is not None and \
                sum(g.profile.memory_mib for g in self.guests.values()) + profile.memory_mib > MAX_MEMORY_MIB:
            return "memory budget in use"
        if self.memory() - profile.memory_mib < MEMORY_HEADROOM_MIB:
            return "not enough free memory"
        if self.temp() >= START_BELOW_C:
            return "CPU too warm"
        if len(self.guests) >= SLOTS:
            return "all slots in use"
        return None

    def launch(self, profile: Profile) -> Guest:
        name = f"klukai-ci-{secrets.token_hex(6)}"
        slot = min(set(range(SLOTS)) - {g.slot for g in self.guests.values()})
        job_dir = JOBS / name
        job_dir.mkdir(mode=0o750, parents=True)
        runner_id = None
        try:
            shutil.chown(job_dir, "root", "qemu")
            self.sh(
                "qemu-img",
                "create",
                "-q",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(BASE),
                str(job_dir / "disk.qcow2"),
                "80G",
            )
            runner_id, jit = self.github.jit_config(name, profile)
            with tempfile.TemporaryDirectory(dir=job_dir) as seed_src:
                src = Path(seed_src)
                (src / "user-data").write_text(user_data(jit))
                (src / "meta-data").write_text(
                    json.dumps({"instance-id": name, "local-hostname": name})
                )
                (src / "network-config").write_text(network_config(slot))
                self.sh(
                    "genisoimage",
                    "-quiet",
                    "-output",
                    str(job_dir / "seed.iso"),
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    str(src / "user-data"),
                    str(src / "meta-data"),
                    str(src / "network-config"),
                )
            del jit
            (job_dir / "seed.iso").chmod(0o640)
            shutil.chown(job_dir / "seed.iso", "root", "qemu")
            xml = job_dir / "domain.xml"
            xml.write_text(domain_xml(name, profile, slot, job_dir))
            self.sh(*VIRSH, "create", str(xml))
        except BaseException:
            if runner_id is not None:
                with _quiet():
                    self.github.delete_runner(runner_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        guest = Guest(name, profile, slot, runner_id, self.clock())
        self.guests[name] = guest
        self._save()
        log.info("started %s (%s) for a queued job", name, profile.label)
        return guest

    def teardown(self, name: str) -> None:
        guest = self.guests.get(name)
        self.sh(*VIRSH, "destroy", name, check=False)
        if guest is not None:
            self.github.delete_runner(guest.runner_id)
        if NAME_RE.fullmatch(name):
            shutil.rmtree(JOBS / name, ignore_errors=True)
        self.guests.pop(name, None)
        self._save()
        log.info("removed %s", name)

    def reap(self) -> None:
        domains = self.running_domains()
        now = self.clock()
        known = set(self.guests)
        for name in list(self.guests):
            state = domains.get(name, "")
            if state not in ("running", "paused"):
                self.teardown(name)  # its one job is done (guest powered off)
            elif now - self.guests[name].started > MAX_JOB_SECONDS:
                log.warning("%s exceeded %d s; destroying", name, MAX_JOB_SECONDS)
                self.teardown(name)
        for name, state in domains.items():  # never leave an unknown guest running
            if (
                name not in self.guests
                and name not in known
                and state in ("running", "paused")
            ):
                self.teardown(name)
        for path in JOBS.glob("klukai-ci-*") if JOBS.is_dir() else ():
            if path.name not in self.guests and NAME_RE.fullmatch(path.name):
                shutil.rmtree(path, ignore_errors=True)
        # Console logs outlive their guests for diagnosis (boot output only; the
        # JIT registration is never printed), then go after CONSOLE_LOG_DAYS.
        for log_path in (
            CONSOLE_LOGS.glob("klukai-ci-*.console.log")
            if CONSOLE_LOGS.is_dir()
            else ()
        ):
            if now - log_path.stat().st_mtime > CONSOLE_LOG_DAYS * 86400:
                log_path.unlink(missing_ok=True)

    def thermal(self) -> None:
        temp = self.temp()
        for guest in self.guests.values():
            if temp >= PAUSE_AT_C and not guest.paused:
                self.sh(*VIRSH, "suspend", guest.name, check=False)
                guest.paused = True
                log.warning("CPU %.0f C: paused %s", temp, guest.name)
            elif temp < RESUME_BELOW_C and guest.paused:
                self.sh(*VIRSH, "resume", guest.name, check=False)
                guest.paused = False
                log.info("CPU %.0f C: resumed %s", temp, guest.name)
        self._save()

    def demand(self) -> list[Profile]:
        """Queued jobs not yet covered by a warming guest, quick checks first."""
        wanted: dict[str, int] = {}
        now = self.clock()
        seen: set[int] = set()
        for job in self.github.queued_jobs():
            profile = profile_for(job)
            if profile is None and STEAL_PROFILES:
                stolen = profile_for(job, STEAL_PROFILES)
                if stolen is not None:
                    job_id = int(job.get("id") or 0)
                    seen.add(job_id)
                    if now - self.first_seen.setdefault(job_id, now) >= STEAL_AFTER_SECONDS:
                        profile = stolen  # its own pool has not taken it: run it here
            if profile is not None:
                wanted[profile.label] = wanted.get(profile.label, 0) + 1
        self.first_seen = {k: v for k, v in self.first_seen.items() if k in seen}
        self.surplus: list[str] = []
        for guest in list(self.guests.values()):
            busy = self.github.runner_busy(guest.runner_id)
            if busy is False and now - guest.started > IDLE_SECONDS:
                log.info(
                    "%s never got a job in %d s; removing it", guest.name, IDLE_SECONDS
                )
                self.teardown(guest.name)
                continue
            # GitHub's queued list excludes the job already assigned to a busy
            # runner. Counting that runner again hides a distinct waiting job
            # for up to five minutes and leaves otherwise free CPUs idle.
            assignments = getattr(self.github, "assigned_runners", None)
            # Job listings and runner_busy are separate, non-atomic API reads.
            # Use assignment IDs from the same snapshot as queued demand. A
            # newly busy runner absent there still covers its stale queued row;
            # a listed assignment has consumed its JIT guest's only job.
            idle = (
                (guest.runner_id not in assignments)
                if assignments is not None
                else busy is not True
            )
            if idle and wanted.get(guest.profile.label):
                wanted[guest.profile.label] -= 1
            elif idle and busy is False and now - guest.started > SURPLUS_GRACE_SECONDS:
                self.surplus.append(guest.name)
        priority = {"small": 0, "frontend": 1, "build": 2, "serial": 3, "medium": 4, "heavy": 5}
        order = sorted({**STEAL_PROFILES, **PROFILES}.values(), key=lambda p: (priority[p.kind], p.label))
        return [p for p in order for _ in range(wanted.get(p.label, 0))]

    def tick(self) -> None:
        self.reap()
        if not self.guests:  # never re-own files a running guest is writing
            prune_build_caches(ARTIFACTS, now=self.clock())
            if RECORD_DAYS is not None:
                prune_records(ARTIFACTS, days=RECORD_DAYS, now=self.clock())
            normalize_ownership(ARTIFACTS)
        self.thermal()
        reason = self.ready() or self.blocked()
        self.heartbeat(reason is None and self.temp() < START_BELOW_C)
        if reason:
            log.debug("not starting guests: %s", reason)
            return
        for profile in self.demand():
            why = self.admit(profile)
            while why in ("CPU budget in use", "all slots in use") and self.surplus:
                name = self.surplus.pop(0)
                guest = self.guests.get(name)
                # Re-ask GitHub right before: a job assigned since the snapshot
                # makes it busy, and a busy (or unknown) guest is never removed.
                if guest is None or self.github.runner_busy(guest.runner_id) is not False:
                    continue
                log.info(
                    "%s is idle with no %s job queued; freeing it for a waiting %s job",
                    name, guest.profile.label, profile.label,
                )
                self.teardown(name)
                why = self.admit(profile)
            if why:
                log.info("queued %s job waits: %s", profile.label, why)
                continue  # backfill free capacity; the perf VM stays exclusive
            self.launch(profile)


def prune_records(root: Path, *, days: int, now: float) -> int:
    """Drop <sha>/<run> records untouched for `days` on a pool whose records are
    pulled into amarillo's archive (the cache tree is never touched)."""
    removed = 0
    for record in root.glob("*/*"):
        if record.is_symlink() or not record.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", record.parent.name):
            continue
        newest = max((p.lstat().st_mtime for p in [record, *record.rglob("*")]), default=0.0)
        if now - newest > days * 86400:
            shutil.rmtree(record, ignore_errors=True)
            removed += 1
            with _quiet():
                record.parent.rmdir()  # only if now empty
    return removed


def prune_build_caches(root: Path, *, now: float, keep: int = 2) -> int:
    """Prune disposable exports only while no guest can be importing them.

    Preserve current pointers, two newest exports per role and a one-hour grace
    period. Source, terminal manifests, scanned image tars and fonts stay intact.
    """
    pinned = set()
    for record in (root / "cache").glob("*buildkit.json"):
        try:
            if record.is_symlink():
                continue
            path = Path(json.loads(record.read_text())["directory"])
            guest_root = Path("/opt/klukai-ci/artifacts")
            if path.is_relative_to(guest_root):
                path = root / path.relative_to(guest_root)
            pinned.add(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    removed = 0
    for suffix in ("release-buildkit", "ci-image/buildkit-cache"):
        candidates = []
        for path in root.glob("*/*/" + suffix):
            parts = path.relative_to(root).parts
            if (
                re.fullmatch(r"[a-f0-9]{40}", parts[0])
                and re.fullmatch(r"[0-9]+-[0-9]+", parts[1])
                and path.is_dir()
                and not any(p.is_symlink() for p in (path, *path.parents))
            ):
                candidates.append(path)
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates[keep:]:
            if path not in pinned and now - path.stat().st_mtime > 3600:
                shutil.rmtree(path)
                removed += 1
    return removed


def normalize_ownership(root: Path) -> int:
    """Give guest-written evidence to the tree's owner (the relay's user).

    Guests write with their own uids (root included) through virtiofs. chown by
    root also clears setuid/setgid bits; the tree is mounted nosuid,nodev,noexec.
    Symlinks are never followed.
    """
    if not root.is_dir() or root.is_symlink():
        return 0
    owner = root.stat()
    changed = 0
    for directory, dirs, files in os.walk(root):
        for entry in [*dirs, *files]:
            path = os.path.join(directory, entry)
            info = os.lstat(path)
            if (info.st_uid, info.st_gid) != (owner.st_uid, owner.st_gid):
                os.lchown(path, owner.st_uid, owner.st_gid)
                changed += 1
            if not os.path.islink(path):
                mode = info.st_mode & 0o7777 & ~0o6000
                os.chmod(path, mode | (0o700 if os.path.isdir(path) else 0o600))
    return changed


class _quiet:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def read_token() -> str:
    if TOKEN_SOURCE == "gh-cli":
        # The owner's existing GitHub CLI login, read at start and kept in memory only.
        found = subprocess.run(["runuser", "-u", "jalsarraf", "--", "gh", "auth", "token"],
                               capture_output=True, text=True, timeout=30)
        if found.returncode or not found.stdout.strip():
            raise RunnerError("GitHub CLI login unavailable")
        return found.stdout.strip()
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RunnerError(
            "CREDENTIALS_DIRECTORY unset; run from klukai-ci-runner.service"
        )
    token = (Path(directory) / "github-token").read_text().strip()
    if not token:
        raise RunnerError("empty GitHub token credential")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if HOST_CONFIG.is_file():
        configure(json.loads(HOST_CONFIG.read_text()))
    controller = Controller(GitHub(read_token()))
    while True:
        try:
            controller.tick()
        except RunnerError as error:
            log.error("%s", error)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
