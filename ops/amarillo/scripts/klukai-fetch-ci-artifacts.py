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
* `nightly`: copy new nightly performance summaries into a JSON-lines file the
  observability stack ingests; optionally warn the owner when none is recent.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.ci import release_artifact  # noqa: E402

ARTIFACTS = "/opt/klukai-ci/artifacts"
SIGNING_NAMESPACE = "klukai-release"
SCAN_JOB = "Image Scan (trivy)"
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


def green_run(repo: str, revision: str, token: str, api: Any = github) -> str:
    """Return '<run_id>-<attempt>' of the newest fully green CI run for the commit."""
    runs = api(
        f"/repos/{repo}/actions/workflows/ci.yml/runs?head_sha={revision}"
        "&status=success&per_page=20",
        token,
    )["workflow_runs"]
    for run in sorted(runs, key=lambda r: r["run_started_at"], reverse=True):
        if run["head_sha"] != revision or run["conclusion"] != "success":
            continue
        jobs = api(f"/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=50", token)
        if any(
            j["name"] == "Required CI gates" and j["conclusion"] == "success"
            for j in jobs["jobs"]
        ):
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


def ci_manifest_digest(
    repo: str, run: str, token: str, api: Any = github, log: Any = job_log
) -> str:
    """The manifest SHA-256 that the run's own image-scan job printed. GitHub's
    job log is written by the run, so this binds the guest's bytes to it: a
    file written to the shared heavy guest by any other job will not match."""
    run_id, attempt = run.split("-")
    jobs = api(
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=50",
        token,
    )["jobs"]
    scan = [j for j in jobs if j["name"] == SCAN_JOB and j["conclusion"] == "success"]
    if len(scan) != 1:
        raise ValueError("The run has no successful image-scan job")
    found = set(
        re.findall(
            r"KLUKAI_RELEASE_MANIFEST_SHA256=([0-9a-f]{64})",
            log(repo, scan[0]["id"], token),
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
    listing = guest.run(
        [
            "/usr/bin/find",
            f"{ARTIFACTS}/nightly",
            "-maxdepth",
            "2",
            "-name",
            "summary.json",
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
        local = state / f"{name}.json"
        guest.read(path, local)
        try:
            doc = json.loads(local.read_text())
        except ValueError:
            # Partly written or corrupt: retried next run, never blocks others.
            doc = None
        if not valid_summary(doc):
            local.unlink()
            continue
        with sink.open("a") as out:
            for row in rows(doc):
                out.write(json.dumps(row, sort_keys=True) + "\n")
        with seen.open("a") as out:
            out.write(name + "\n")
        added.append(name)
    return added


def stale(state: Path, max_age: timedelta, now: datetime | None = None) -> bool:
    newest = None
    for path in state.glob("*.json"):
        finished = _finished(json.loads(path.read_text()))
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
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser("image")
    i.add_argument("revision")
    i.add_argument("--repo", default="Al-Sarraf-Tech/klukai")
    i.add_argument("--signing-key", type=Path, default=SIGNING_KEY)
    i.add_argument(
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
    args = parser.parse_args()
    guest = Guest(VirshAgent(args.host, args.domain))
    try:
        if args.command == "image":
            token = os.environ.get("GH_TOKEN") or os.environ.get(
                "GITHUB_PERSONAL_ACCESS_TOKEN"
            )
            if not token:
                raise ValueError("Set GH_TOKEN to check CI status")
            if not args.signing_key.is_file():
                raise ValueError(f"Release signing key missing: {args.signing_key}")
            run = green_run(args.repo, args.revision, token)
            result = fetch_image(
                guest,
                run,
                args.revision,
                args.dest / args.revision,
                manifest_sha256=ci_manifest_digest(args.repo, run, token),
                key=args.signing_key,
            )
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
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"fetch refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
