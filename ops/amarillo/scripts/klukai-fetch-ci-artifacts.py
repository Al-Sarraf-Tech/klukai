#!/usr/bin/env python3
"""Pull CI evidence out of the isolated heavy guest onto amarillo.

The CI guest has no route to amarillo by design. Amarillo already reaches it
through the Dominus host's QEMU guest agent (ssh + `sudo virsh
qemu-agent-command`), so the guest stays isolated and Dominus needs no extra
software:

* `image <revision>`: require a successful GitHub CI run for that exact commit
  (including the aggregate "Required CI gates" job), stream its retained
  `release/core-image.tar`, `manifest.json` and provenance materials (SBOM,
  licence inventory and verdict, reproducibility), verify and load them with
  tools.ci.release_artifact (material digests, tar hash, layer diff IDs,
  revision label), then sign `provenance.json` with the amarillo release key
  (ssh-keygen, namespace klukai-release) for the release deployer.
* `web <revision>`: the same green run's frontend job retained the CI-built PWA
  (`frontend/pwa`, every file digested in its manifest). Its tree digest must
  equal the `KLUKAI_PWA_SHA256=` line that job printed to its own GitHub log;
  each file is copied and checked, the deterministic content-hash transform
  (tools/ci/web_release.py) turns it into the release tree, and
  `web-provenance.json` (the CI run, the CI tree digest and every release file
  digest) is signed with the same release key for the web deployer.
* `nightly`: copy new nightly performance summaries into a JSON-lines file the
  observability stack ingests; optionally warn the owner when none is recent.
* `archive`: move finished CI records off the guest's small disk into
  /docker/klukai/ci-records on amarillo (owner decision 2026-10-19). The guest
  keeps its newest three revisions (so `image` can still relay them) and the
  newest two nightly runs; older ones that have been idle for two hours (and,
  for a nightly run, whose summary `nightly` has already relayed) are
  tarred on the guest (image tars excluded: their manifests are the record),
  copied, checked by SHA-256, and only then deleted on the guest. Archives
  older than 90 days are removed from amarillo.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.ci import release_artifact  # noqa: E402
from tools.ci import web_release  # noqa: E402
from tools.ci.frontend_evidence import inventory, tree_digest  # noqa: E402

ARTIFACTS = "/opt/klukai-ci/artifacts"
SIGNING_NAMESPACE = "klukai-release"
SCAN_JOB = "Image Scan (trivy)"
FRONTEND_JOB = "Frontend validation and immutable PWA evidence"
WEB_KIND = "klukai-web"
SIGNING_KEY = Path.home() / ".config/klukai/release-signing/release_ed25519"
# Two libvirt limits bound each guest-file-read. virtqemud rejects any agent
# reply over 10 MiB and then disables the agent channel until the stale reply is
# drained ("No complete agent response found in 10485760 bytes"); virsh's RPC
# then carries the reply as one string capped at 4 MiB ("Unable to encode
# message payload"). Replies are base64 (4/3 larger) inside JSON.
LIBVIRT_AGENT_REPLY_MAX = 10 * 1024 * 1024
LIBVIRT_RPC_STRING_MAX = 4 * 1024 * 1024
CHUNK = 2 * 1024 * 1024
WARN_EVERY = timedelta(days=1)
ARCHIVE = Path("/docker/klukai/ci-records")
OUTBOX = "/opt/klukai-ci/outbox"
KEEP_REVISIONS = 3
KEEP_NIGHTS = 2
IDLE = timedelta(hours=2)
ARCHIVE_DAYS = 90
REVISION_NAME = re.compile(r"[0-9a-f]{40}")
NIGHT_NAME = re.compile(r"nightly/\d{4}-\d\d-\d\dT\d\d-\d\dZ-[0-9a-f]{7}")
LOCK = Path.home() / ".local/state/klukai/ci-relay.lock"
# Amarillo's own CI VMs (ops/amarillo/ci-runner) write the same tree here.
LOCAL_ROOTS = {
    ARTIFACTS: Path("/docker/klukai/ci-artifacts"),
    OUTBOX: Path("/docker/klukai/ci-outbox"),
}
# The guest clock, then "<newest mtime> <record>" for every top-level record.
LIST_RECORDS = r"""cd "$1" || exit 1
date +%s
for d in */ nightly/*/; do
  d=${d%/}
  [ -d "$d" ] || continue
  printf '%s %s\n' "$(find "$d" -printf '%T@\n' | sort -n | tail -n 1)" "$d"
done
"""
PACK_RECORD = r"""set -e
mkdir -p "$1"
tar -C "$2" --exclude='*/release/core-image.tar' -cf "$3" "$4"
sha256sum "$3"
"""


class HostUnreachable(RuntimeError):
    """ssh could not reach the Dominus host (normal while it is powered off)."""


class Transport(Protocol):
    def agent(self, command: dict[str, Any]) -> Any: ...


class VirshAgent:
    """QEMU guest agent of the heavy guest, via ssh to the Dominus host."""

    def __init__(self, host: str, domain: str) -> None:
        self.host, self.domain = host, domain

    def agent(self, command: dict[str, Any]) -> Any:
        remote = shlex.join(
            [
                "sudo",
                "-n",
                "virsh",
                "-c",
                "qemu:///system",
                "qemu-agent-command",
                self.domain,
                json.dumps(command),
            ]
        )
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                self.host,
                remote,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 255:
            raise HostUnreachable(
                f"{self.host} unreachable: {result.stderr.strip()[:200]}"
            )
        if result.returncode:
            raise RuntimeError(f"guest agent failed: {result.stderr.strip()[:200]}")
        return json.loads(result.stdout)["return"]


class LocalGuest:
    """The same guest interface over amarillo's local CI tree (no agent, no ssh)."""

    def __init__(self, roots: dict[str, Path] | None = None) -> None:
        self.roots = dict(LOCAL_ROOTS if roots is None else roots)

    def _map(self, value: str) -> str:
        for remote, local in self.roots.items():
            if value == remote or value.startswith(remote + "/"):
                return str(local) + value[len(remote) :]
        return value

    def run(self, argv: list[str], timeout: float = 120) -> str:
        result = subprocess.run(
            [self._map(a) for a in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"local command failed: {argv[:2]}")
        return "".join(self._unmap(line) for line in result.stdout.splitlines(True))

    def _unmap(self, line: str) -> str:
        for remote, local in self.roots.items():
            line = line.replace(str(local), remote)
        return line

    def read(self, path: str, dest: Path) -> None:
        source = Path(self._map(path))
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"no such CI file: {path}")
        with source.open("rb") as src, dest.open("wb") as out:
            for block in iter(lambda: src.read(CHUNK), b""):
                out.write(block)


class Guest:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def run(self, argv: list[str], timeout: float = 120) -> str:
        started = self.transport.agent(
            {
                "execute": "guest-exec",
                "arguments": {"path": argv[0], "arg": argv[1:], "capture-output": True},
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            status = self.transport.agent(
                {"execute": "guest-exec-status", "arguments": {"pid": started["pid"]}}
            )
            if status.get("exited"):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("guest command timed out")
            time.sleep(0.5)
        if status.get("exitcode") != 0:
            raise RuntimeError(f"guest command failed: {argv[:2]}")
        return base64.b64decode(status.get("out-data", "")).decode()

    def read(self, path: str, dest: Path) -> None:
        handle = self.transport.agent(
            {"execute": "guest-file-open", "arguments": {"path": path, "mode": "r"}}
        )
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = self.transport.agent(
                        {
                            "execute": "guest-file-read",
                            "arguments": {"handle": handle, "count": CHUNK},
                        }
                    )
                    data = base64.b64decode(chunk.get("buf-b64", ""))
                    out.write(data)
                    if chunk.get("eof"):
                        break
                    if not data:
                        raise RuntimeError("guest agent returned no data before EOF")
        finally:
            self.transport.agent(
                {"execute": "guest-file-close", "arguments": {"handle": handle}}
            )


def github(path: str, token: str) -> Any:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


FULL_CI_GATES = {
    "Authorized exact source",
    "Pinned Python CI executor",
    "Isolated component suites",
    "Test Suite",
    "Real PostgreSQL, Valkey and Qdrant recovery",
    "Lint",
    "Type Check (mypy)",
    "Security Scan (bandit + safety)",
    "Image Scan (trivy)",
    "Perf Gate (p99 regression check)",
    "Frontend validation and immutable PWA evidence",
    "Required CI gates",
}


def _attempt_jobs(repo: str, run: dict[str, Any], token: str, api: Any) -> list[dict[str, Any]]:
    """Read one complete, attempt-pinned jobs snapshot, never a partial page."""
    jobs: list[dict[str, Any]] = []
    ids: set[int] = set()
    total: int | None = None
    page = 1
    while True:
        response = api(
            f"/repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
            f"?per_page=100&page={page}",
            token,
        )
        if not isinstance(response, dict):
            raise ValueError("Invalid jobs response")  # noqa: TRY004 - remote evidence validation
        count, batch = response.get("total_count"), response.get("jobs")
        if type(count) is not int or count < 0 or not isinstance(batch, list):
            raise ValueError("Incomplete jobs response")
        if total is not None and count != total:
            raise ValueError("Jobs changed while paging")
        total = count
        for job in batch:
            if not isinstance(job, dict) or type(job.get("id")) is not int \
                    or job["id"] <= 0 or job["id"] in ids:
                raise ValueError("Missing or duplicate job identity")
            # run_attempt is optional in GitHub's job schema. The pinned
            # endpoint binds an absent field; a supplied value must match.
            if type(job.get("run_id")) is not int or job["run_id"] != run["id"] \
                    or job.get("head_sha") != run["head_sha"] \
                    or type(job.get("run_attempt", run["run_attempt"])) is not int \
                    or job.get("run_attempt", run["run_attempt"]) != run["run_attempt"] \
                    or job.get("status") != "completed" or not isinstance(job.get("name"), str):
                raise ValueError("Job evidence does not match the complete exact run attempt")
            ids.add(job["id"])
        jobs.extend(batch)
        if len(batch) > 100 or len(jobs) > total:
            raise ValueError("Jobs exceed the reported total")
        if len(jobs) == total:
            return jobs
        if len(batch) < 100:
            raise ValueError("Incomplete jobs page")
        page += 1


def green_run(repo: str, revision: str, token: str, api: Any = github) -> str:
    """Return '<run_id>-<attempt>' of the newest fully green CI run for the commit."""
    runs = api(
        f"/repos/{repo}/actions/workflows/ci.yml/runs?head_sha={revision}"
        "&status=success&per_page=20",
        token,
    )
    if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
        raise ValueError("Incomplete workflow runs response")  # noqa: TRY004 - remote evidence validation
    candidates = [r for r in runs["workflow_runs"] if isinstance(r, dict)]
    for run in sorted(candidates, key=lambda r: str(r.get("run_started_at", "")), reverse=True):
        if run.get("head_sha") != revision or run.get("conclusion") != "success" \
                or run.get("status") != "completed" or type(run.get("id")) is not int \
                or run["id"] <= 0 or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0:
            continue
        # A rerun can start after the runs response. Bind every gate to the
        # same attempt whose artifacts and job logs will be relayed below.
        try:
            jobs = _attempt_jobs(repo, run, token, api)
        except ValueError:
            continue
        gates = [j for j in jobs if j["name"] in FULL_CI_GATES]
        certified = {j["name"] for j in gates if j.get("conclusion") == "success"}
        if len(gates) == len(FULL_CI_GATES) and FULL_CI_GATES == certified:
            return f"{run['id']}-{run['run_attempt']}"
    raise ValueError("No successful CI run with Required CI gates for this revision")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def job_log(repo: str, job_id: int, token: str) -> str:
    """Plain-text log of one Actions job. GitHub answers with a redirect to a
    pre-signed URL; follow it without forwarding the API token."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/jobs/{int(job_id)}/logs",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location", "")
        error.close()
        if error.code not in (301, 302, 303, 307, 308) or not location.startswith(
            "https://"
        ):
            raise RuntimeError(f"job log unavailable (HTTP {error.code})") from error
    with urllib.request.urlopen(location, timeout=60) as response:
        return response.read().decode(errors="replace")


def _successful_attempt_job(
    repo: str, run: str, revision: str, token: str, name: str, api: Any
) -> dict[str, Any]:
    """Resolve one named job only after validating the complete pinned attempt."""
    release_artifact._revision(revision)
    if not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", run):
        raise ValueError("Invalid CI run identity")
    run_id, attempt = map(int, run.split("-"))
    jobs = _attempt_jobs(
        repo, {"id": run_id, "run_attempt": attempt, "head_sha": revision}, token, api
    )
    named = [job for job in jobs if job["name"] == name]
    if len(named) != 1 or named[0].get("conclusion") != "success":
        raise ValueError(f"The run has no unique successful {name} job")
    return named[0]


def ci_manifest_digest(
    repo: str, run: str, token: str, api: Any = github, log: Any = job_log, *, revision: str
) -> str:
    """The manifest SHA-256 that the run's own image-scan job printed. GitHub's
    job log is written by the run, so this binds the guest's bytes to it: a
    file written to the shared heavy guest by any other job will not match."""
    scan = _successful_attempt_job(repo, run, revision, token, SCAN_JOB, api)
    found = set(
        re.findall(
            r"KLUKAI_RELEASE_MANIFEST_SHA256=([0-9a-f]{64})",
            log(repo, scan["id"], token),
        )
    )
    if len(found) != 1:
        raise ValueError("The image-scan job log does not record one manifest digest")
    return found.pop()


def sign(path: Path, key: Path) -> Path:
    """ssh-keygen signature over one file in the klukai-release namespace."""
    signature = path.with_name(path.name + ".sig")
    signature.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-Y",
            "sign",
            "-f",
            str(key),
            "-n",
            SIGNING_NAMESPACE,
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode or not signature.is_file():
        raise RuntimeError(f"signing failed: {result.stderr.strip()[:200]}")
    return signature


def fetch_image(
    guest: Guest,
    run: str,
    revision: str,
    dest: Path,
    *,
    key: Path | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    release_artifact._revision(revision)
    if not re.fullmatch(r"\d+-\d+", run):
        raise ValueError("Invalid CI run identity")
    remote = f"{ARTIFACTS}/{revision}/{run}/release"
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    (dest / "provenance.json.sig").unlink(missing_ok=True)
    guest.read(f"{remote}/manifest.json", dest / "manifest.json")
    manifest = json.loads((dest / "manifest.json").read_text())
    release_artifact.check_manifest(manifest, revision)
    if manifest.get("run") != run:
        raise ValueError("Manifest was saved by a different CI run")
    if manifest_sha256 is not None and (
        release_artifact.sha256(dest / "manifest.json") != manifest_sha256
    ):
        raise ValueError("Manifest differs from the digest the CI job logged")
    for name in release_artifact.MATERIALS:
        guest.read(f"{remote}/{name}", dest / name)
    materials = release_artifact.verify_materials(dest, manifest)
    guest.read(f"{remote}/core-image.tar", dest / "core-image.tar")
    result = release_artifact.verify(
        dest / "core-image.tar",
        dest / "manifest.json",
        revision,
        f"klukai-core:ci-{revision[:7]}",
    )
    result.update(materials)
    if key is not None:
        # Signed only after every check passed: the green CI run, the manifest,
        # each provenance material and the loaded image's layers and label.
        provenance = {
            "schema": 1,
            "revision": revision,
            "ci_run": run,
            "manifest_sha256": release_artifact.sha256(dest / "manifest.json"),
            "tar_sha256": manifest["tar_sha256"],
            "layers": manifest["layers"],
            "config_sha256": manifest["config_sha256"],
            "materials": manifest["materials"],
            "verified": result,
            "verified_by": "amarillo klukai-fetch-ci-artifacts",
            "verified_at": datetime.now(UTC).isoformat(),
        }
        (dest / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        result["signature"] = str(sign(dest / "provenance.json", key))
    return result


def ci_pwa_digest(
    repo: str, run: str, token: str, api: Any = github, log: Any = job_log, *, revision: str
) -> str:
    """The PWA tree digest the run's own frontend job printed. Runs from before
    the explicit KLUKAI_PWA_SHA256= line carry it only in the evidence tool's
    JSON summary; either way exactly one digest must appear."""
    frontend = _successful_attempt_job(repo, run, revision, token, FRONTEND_JOB, api)
    text = log(repo, frontend["id"], token)
    found = set(re.findall(r"KLUKAI_PWA_SHA256=([0-9a-f]{64})", text))
    found |= set(re.findall(r'"pwa_sha256": "([0-9a-f]{64})"', text))
    if len(found) != 1:
        raise ValueError("The frontend job log does not record one PWA digest")
    return found.pop()


def fetch_web(
    guest: Guest,
    run: str,
    revision: str,
    dest: Path,
    *,
    pwa_sha256: str,
    key: Path | None = None,
) -> dict[str, Any]:
    """Copy and verify the run's CI-built PWA, then build and sign the release tree."""
    release_artifact._revision(revision)
    if not re.fullmatch(r"\d+-\d+", run):
        raise ValueError("Invalid CI run identity")
    if not re.fullmatch(r"[0-9a-f]{64}", pwa_sha256):
        raise ValueError("Invalid PWA digest")
    remote = f"{ARTIFACTS}/{revision}/{run}/frontend"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(mode=0o700, parents=True)
    guest.read(f"{remote}/manifest.json", dest / "frontend-manifest.json")
    manifest = json.loads((dest / "frontend-manifest.json").read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unknown frontend manifest schema")
    if manifest.get("source", {}).get("revision") != revision:
        raise ValueError("Frontend manifest is for a different revision")
    files = manifest.get("pwa", {}).get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Frontend manifest lists no PWA files")
    if tree_digest(files) != manifest["pwa"].get("sha256") or manifest["pwa"][
        "sha256"
    ] != pwa_sha256:
        raise ValueError("PWA differs from the digest the CI job logged")
    ci_tree = dest / "ci-pwa"
    ci_tree.mkdir(mode=0o700)
    for name, digest in sorted(files.items()):
        target = ci_tree / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        guest.read(f"{remote}/pwa/{name}", target)
        if release_artifact.sha256(target) != digest:
            raise ValueError(f"PWA file differs from its manifest digest: {name}")
    if inventory(ci_tree) != manifest["pwa"]:
        raise ValueError("Copied PWA differs from its manifest")
    release = dest / "release"
    shutil.copytree(ci_tree, release)
    shutil.rmtree(ci_tree)
    entry = web_release.content_hash(release)
    built = inventory(release)
    result: dict[str, Any] = {
        "revision": revision,
        "ci_run": run,
        "ci_pwa_sha256": pwa_sha256,
        "release_sha256": built["sha256"],
        "files": len(built["files"]),
        "entry": entry,
    }
    if key is not None:
        # Signed only after every CI file matched the logged digest and the
        # release tree was rebuilt from exactly those bytes.
        provenance = {
            "schema": 1,
            "kind": WEB_KIND,
            "revision": revision,
            "ci_run": run,
            "ci_pwa_sha256": pwa_sha256,
            "entry": entry,
            "release": built,
            "verified_by": "amarillo klukai-fetch-ci-artifacts",
            "verified_at": datetime.now(UTC).isoformat(),
        }
        (dest / "web-provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        result["signature"] = str(sign(dest / "web-provenance.json", key))
    return result


def _finished(doc: dict[str, Any]) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(doc.get("finished_at")))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def valid_summary(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and re.fullmatch(r"[0-9a-f]{40}", str(doc.get("revision", ""))) is not None
        and _finished(doc) is not None
        and isinstance(doc.get("routes"), list)
        and all(
            isinstance(r, dict) and isinstance(r.get("candidate_p99_ms"), (int, float))
            for r in doc["routes"]
        )
    )


def rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """One verdict line per night plus one flat line per route, for Loki/Grafana."""
    common = {
        "source": "klukai-perf-nightly",
        "revision": doc["revision"],
        "finished_at": doc["finished_at"],
        "passed": doc.get("passed") is True,
    }
    night = {
        **common,
        "kind": "night",
        "max_p99_ratio": doc.get("max_p99_ratio"),
        "failure": doc.get("failure"),
    }
    return [night] + [
        {
            **common,
            "kind": "route",
            "method": r.get("method"),
            "route": r.get("route"),
            "candidate_p99_ms": r["candidate_p99_ms"],
            "baseline_p99_ms": r.get("baseline_p99_ms"),
            "p99_ratio": r.get("p99_ratio"),
        }
        for r in doc["routes"]
    ]


def fetch_nightly(guest: Guest, state: Path, sink: Path) -> list[str]:
    # A tree no nightly has written yet (a new runner) simply has no summaries.
    listing = guest.run(
        [
            "/bin/sh",
            "-c",
            '[ -d "$1" ] || exit 0; exec /usr/bin/find "$1" -maxdepth 2 -name summary.json',
            "sh",
            f"{ARTIFACTS}/nightly",
        ]
    )
    seen = state / "seen.txt"
    known = set(seen.read_text().split()) if seen.exists() else set()
    added = []
    for path in sorted(
        line for line in listing.split() if line.endswith("/summary.json")
    ):
        name = path.split("/")[-2]
        if name in known or not re.fullmatch(r"[0-9TZ:-]+-[0-9a-f]{7}", name):
            continue
        # Download beside the state (not matched by "*.json"), validate, then
        # publish atomically: a lost connection never leaves a torn summary.
        staged = state / f".{name}.part"
        try:
            guest.read(path, staged)
            try:
                doc = json.loads(staged.read_text())
            except ValueError:
                # Partly written or corrupt on the guest: retried next run.
                doc = None
            if not valid_summary(doc):
                continue
            os.replace(staged, state / f"{name}.json")
        finally:
            staged.unlink(missing_ok=True)
        with sink.open("a") as out:
            for row in rows(doc):
                out.write(json.dumps(row, sort_keys=True) + "\n")
        with seen.open("a") as out:
            out.write(name + "\n")
        added.append(name)
    return added


def archive_candidates(
    listing: str, relayed: frozenset[str] = frozenset()
) -> list[str]:
    """Records safe to move: beyond the newest kept ones and idle long enough.

    A nightly record also waits until `nightly` has relayed its summary.
    """
    lines = listing.splitlines()
    now = float(lines[0])
    revisions: list[tuple[float, str]] = []
    nights: list[tuple[float, str]] = []
    for line in lines[1:]:
        stamp, _, record = line.partition(" ")
        try:
            newest = float(stamp)
        except ValueError:
            continue
        if REVISION_NAME.fullmatch(record):
            revisions.append((newest, record))
        elif NIGHT_NAME.fullmatch(record):
            nights.append((newest, record))
    idle = IDLE.total_seconds()
    chosen = []
    for group, keep in ((revisions, KEEP_REVISIONS), (nights, KEEP_NIGHTS)):
        group.sort(reverse=True)
        chosen += [
            r
            for newest, r in group[keep:]
            if now - newest >= idle
            and (group is revisions or r.split("/")[1] in relayed)
        ]
    return sorted(chosen)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        os.fsync(source.fileno())
    return digest.hexdigest()


def archive_record(guest: Guest, record: str, dest: Path) -> dict[str, Any]:
    """Copy one record to amarillo and delete it on the guest only once verified."""
    if not (REVISION_NAME.fullmatch(record) or NIGHT_NAME.fullmatch(record)):
        raise ValueError(f"not a CI record: {record!r}")
    name = record.replace("/", "-")
    packed = f"{OUTBOX}/{name}.tar"
    final = dest / f"{name}.tar"
    partial = dest / f".{name}.tar.partial"
    try:
        out = guest.run(
            ["/bin/sh", "-c", PACK_RECORD, "sh", OUTBOX, ARTIFACTS, packed, record],
            timeout=900,
        )
        digest = out.split()[0] if out.split() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"guest returned no digest for {record}")
        guest.read(packed, partial)
        if _sha256(partial) != digest:
            raise RuntimeError(f"archive of {record} does not match the guest copy")
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
        with contextlib.suppress(RuntimeError, HostUnreachable):
            guest.run(["/bin/rm", "-f", "--", packed])
    guest.run(["/bin/rm", "-rf", "--", f"{ARTIFACTS}/{record}"])
    return {"record": record, "file": str(final), "bytes": final.stat().st_size}


def prune_archive(
    dest: Path, days: int = ARCHIVE_DAYS, now: float | None = None
) -> list[str]:
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = []
    for path in sorted(dest.glob("*.tar")):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def archive(
    guest: Guest, dest: Path, limit: int, relayed: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not dest.is_dir():
        raise ValueError(f"archive directory missing: {dest}")
    moved = [
        archive_record(guest, record, dest)
        for record in archive_candidates(
            guest.run(["/bin/sh", "-c", LIST_RECORDS, "sh", ARTIFACTS]), relayed
        )[:limit]
    ]
    return {"archived": moved, "pruned": prune_archive(dest)}


@contextlib.contextmanager
def guest_lock(path: Path = LOCK) -> Any:
    """One relay command talks to the guest agent at a time."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def stale(state: Path, max_age: timedelta, now: datetime | None = None) -> bool:
    newest = None
    for path in state.glob("*.json"):
        try:
            finished = _finished(json.loads(path.read_text()))
        except (OSError, ValueError, AttributeError):
            continue  # unreadable: it must not stop the missing-summary warning
        if finished is not None and (newest is None or finished > newest):
            newest = finished
    return newest is None or (now or datetime.now(UTC)) - newest > max_age


def should_warn(state: Path, now: datetime | None = None) -> bool:
    """At most one missing-summary note per day, however often the timer runs."""
    now = now or datetime.now(UTC)
    marker = state / "warned_at"
    try:
        last = datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        last = None
    if last is not None and last.tzinfo is not None and now - last < WARN_EVERY:
        return False
    marker.write_text(now.isoformat())
    return True


def warn_missing(days: int) -> None:
    subprocess.run(
        [
            os.path.expanduser("~/.local/bin/bus"),
            "send",
            "owner@amarillo",
            "--project",
            "klukai",
            "--note",
            f"KLUKAI_PERF_NIGHTLY missing: no nightly performance summary in "
            f"{days} days (Dominus off at the nightly window?)",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="dominus-nobara")
    parser.add_argument("--domain", default="fleet-ci-heavy-nobara")
    parser.add_argument(
        "--source",
        choices=("auto", "amarillo", "dominus"),
        default="auto",
        help="amarillo: local CI VMs' tree; dominus: the heavy guest; auto: "
        "image from wherever its run is (amarillo first), nightly/archive from dominus",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser("image")
    i.add_argument("revision")
    i.add_argument("--repo", default="Al-Sarraf-Tech/klukai")
    i.add_argument("--signing-key", type=Path, default=SIGNING_KEY)
    i.add_argument(
        "--dest", type=Path, default=Path.home() / ".local/state/klukai/ci-releases"
    )
    w = sub.add_parser("web")
    w.add_argument("revision")
    w.add_argument("--repo", default="Al-Sarraf-Tech/klukai")
    w.add_argument("--signing-key", type=Path, default=SIGNING_KEY)
    w.add_argument(
        "--dest", type=Path, default=Path.home() / ".local/state/klukai/ci-releases"
    )
    n = sub.add_parser("nightly")
    n.add_argument(
        "--state", type=Path, default=Path.home() / ".local/state/klukai/perf-nightly"
    )
    n.add_argument(
        "--sink",
        type=Path,
        default=Path("/mnt/nvmeINT/obs/klukai/perf-nightly/summaries.jsonl"),
    )
    n.add_argument("--warn-after-days", type=int, default=0)
    a = sub.add_parser("archive")
    a.add_argument("--dest", type=Path, default=ARCHIVE)
    # About 1 MB/s through the agent: two records fit one hourly window.
    a.add_argument("--limit", type=int, default=2, help="records per invocation")
    a.add_argument(
        "--state", type=Path, default=Path.home() / ".local/state/klukai/perf-nightly"
    )
    args = parser.parse_args()
    guest: Any = Guest(VirshAgent(args.host, args.domain))
    if args.source == "amarillo" or (
        args.source == "auto"
        and args.command in ("image", "web")
        and (LOCAL_ROOTS[ARTIFACTS] / args.revision).is_dir()
    ):
        guest = LocalGuest()
    try:
        with guest_lock():
            return _run(args, guest)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"fetch refused: {error}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, guest: Guest) -> int:
    if args.command in ("image", "web"):
        token = os.environ.get("GH_TOKEN") or os.environ.get(
            "GITHUB_PERSONAL_ACCESS_TOKEN"
        )
        if not token:
            raise ValueError("Set GH_TOKEN to check CI status")
        if not args.signing_key.is_file():
            raise ValueError(f"Release signing key missing: {args.signing_key}")
        run = green_run(args.repo, args.revision, token)
        if args.command == "image":
            result = fetch_image(
                guest,
                run,
                args.revision,
                args.dest / args.revision,
                manifest_sha256=ci_manifest_digest(args.repo, run, token, revision=args.revision),
                key=args.signing_key,
            )
        else:
            result = fetch_web(
                guest,
                run,
                args.revision,
                args.dest / args.revision / "web",
                pwa_sha256=ci_pwa_digest(args.repo, run, token, revision=args.revision),
                key=args.signing_key,
            )
    elif args.command == "archive":
        try:
            seen = args.state / "seen.txt"
            relayed = frozenset(seen.read_text().split() if seen.exists() else ())
            result = archive(guest, args.dest, args.limit, relayed)
        except HostUnreachable:
            # Dominus is off most of the day by design: not a failure.
            result = {"archived": [], "skipped": "Dominus host unreachable"}
    else:
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.sink.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = {"added": fetch_nightly(guest, args.state, args.sink)}
        except HostUnreachable:
            # Dominus is off most of the day by design: not a failure.
            result = {"added": [], "skipped": "Dominus host unreachable"}
        if args.warn_after_days and stale(
            args.state, timedelta(days=args.warn_after_days)
        ):
            result["stale"] = True
            if should_warn(args.state):
                warn_missing(args.warn_after_days)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
