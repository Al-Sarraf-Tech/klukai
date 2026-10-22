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
below 60 C. Production runs on this host, so guests sit on the firewalled
``klukai-ci`` network (internet only) and share nothing with the host except
the CI evidence tree, mounted read-write at /opt/klukai-ci/artifacts.

Runs as root from klukai-ci-runner.service. The GitHub token comes from the
systemd credential ``github-token``; it and the JIT configs are never logged.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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


PROFILES = {
    p.label: p
    for p in (
        Profile("klukai-amarillo-heavy", 8, 8192),
        Profile("klukai-amarillo-small", 2, 3072),
        Profile("klukai-amarillo-medium", 4, 4096),
        Profile("klukai-amarillo-build", 4, 4096),
        Profile("klukai-amarillo-serial", 2, 4096),
        Profile("klukai-amarillo-frontend", 4, 8192),
    )
}
MAX_VCPUS = 8
MEMORY_HEADROOM_MIB = 4096
START_BELOW_C = 62.0
PAUSE_AT_C = 68.0
RESUME_BELOW_C = 60.0
MAX_JOB_SECONDS = 4 * 3600
IDLE_SECONDS = 900  # an idle runner this old never got a job: free its capacity
SLOTS = 4  # static guest addresses 10.231.0.10-13

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
    """CPU package temperature from coretemp; unknown reads as too hot."""
    for node in sorted(hwmon.glob("hwmon*")):
        try:
            if (node / "name").read_text().strip() != "coretemp":
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
        for status in ("queued", "in_progress"):
            runs = self.call(
                "GET", f"/repos/{REPO}/actions/runs?status={status}&per_page=30"
            )
            for run in runs.get("workflow_runs", []):
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
                jobs += [
                    j for j in listing.get("jobs", []) if j.get("status") == "queued"
                ]
        return sorted(jobs, key=lambda j: (j.get("created_at") or "", j.get("id") or 0))

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


def profile_for(job: dict[str, Any]) -> Profile | None:
    """Only exact label sets are served: never a Dominus or light-pool job."""
    labels = job.get("labels") or []
    for profile in PROFILES.values():
        if sorted(labels) == sorted([*BASE_LABELS, profile.label]):
            return profile
    return None


# ── Guest definition ───────────────────────────────────────────────────────


def mac_for(slot: int) -> str:
    return f"52:54:00:6b:63:{0x10 + slot:02x}"


def address_for(slot: int) -> str:
    return f"10.231.0.{10 + slot}"


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
                    "routes": [{"to": "0.0.0.0/0", "via": "10.231.0.1"}],
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
        self.guests: dict[str, Guest] = self._load()

    # state survives a controller restart so leftovers are always reaped
    def _load(self) -> dict[str, Guest]:
        try:
            raw = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}
        guests = {}
        for item in raw:
            if (
                NAME_RE.fullmatch(item.get("name", ""))
                and item.get("profile") in PROFILES
            ):
                guests[item["name"]] = Guest(
                    item["name"],
                    PROFILES[item["profile"]],
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

    def admit(self, profile: Profile) -> str | None:
        used = sum(g.profile.vcpus for g in self.guests.values())
        if used + profile.vcpus > MAX_VCPUS:
            return "CPU budget in use"
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
        for job in self.github.queued_jobs():
            profile = profile_for(job)
            if profile is not None:
                wanted[profile.label] = wanted.get(profile.label, 0) + 1
        now = self.clock()
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
            idle = busy is not True
            if idle and wanted.get(guest.profile.label):
                wanted[guest.profile.label] -= 1
        priority = {
            "klukai-amarillo-small": 0,
            "klukai-amarillo-frontend": 1,
            "klukai-amarillo-build": 2,
            "klukai-amarillo-serial": 3,
            "klukai-amarillo-medium": 4,
            "klukai-amarillo-heavy": 5,
        }
        order = sorted(PROFILES.values(), key=lambda p: priority[p.label])
        return [p for p in order for _ in range(wanted.get(p.label, 0))]

    def tick(self) -> None:
        self.reap()
        if not self.guests:  # never re-own files a running guest is writing
            prune_build_caches(ARTIFACTS, now=self.clock())
            normalize_ownership(ARTIFACTS)
        self.thermal()
        reason = self.ready()
        if reason:
            log.debug("not starting guests: %s", reason)
            return
        for profile in self.demand():
            why = self.admit(profile)
            if why:
                log.info("queued %s job waits: %s", profile.label, why)
                continue  # backfill free capacity; an 8-thread perf VM stays exclusive
            self.launch(profile)


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
