#!/usr/bin/env python3
"""Deploy a verified CI-built Klukai core image with automatic rollback.

    klukai-release.py deploy <full sha> [--hold 180]
    klukai-release.py drill                       # prove automatic rollback, disposable

`deploy` runs staged preflight, then the rollout:

1. Artifact: `provenance.json` carries a valid amarillo release signature
   (ops/amarillo/release/allowed_signers), binds the CI manifest by digest, the
   provenance materials match, the licence verdict passed, and the local tag
   `klukai-core:ci-<sha7>` has exactly the manifest's layers and revision.
2. Source: the main checkout is at the same commit with an unmodified
   docker-compose.yml, so runtime configuration matches the release.
3. Backup: a fresh logical dump, restored into an isolated throwaway PostgreSQL
   (same image, no network), must have every table and at least the live row
   counts of the user/memory tables. Nothing is ever restored to production.
4. Rollout: only companion-core is recreated, from the encrypted secret store
   (klukai-compose) plus a non-secret image pin. Settings are otherwise as they
   were. The new container must run the verified image ID, become healthy and
   stay healthy and ready for the hold window with no restarts and no 5xx burst.
5. Rollback: any failure restores the previous image pin and container, waits
   for it to be healthy again and exits non-zero. Data and volumes are never
   touched, so writes made in between are kept.

Every run writes a receipt under ~/.local/state/klukai/release/receipts/.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from tools.ci import release_artifact  # noqa: E402

STATE = Path.home() / ".local/state/klukai/release"
RELEASES = Path.home() / ".local/state/klukai/ci-releases"
BACKUPS = Path("/mnt/nvmeINT/backups/klukai/releases")
SIGNERS = REPO / "ops/amarillo/release/allowed_signers"
SIGNER_ID = "klukai-release@amarillo"
NAMESPACE = "klukai-release"
SERVICE = "companion-core"
READY_URL = "http://127.0.0.1:8300/api/health/ready"
PROMETHEUS = "http://127.0.0.1:19090"
KEY_TABLES = (
    "users",
    "companion_messages",
    "companion_episodes",
    "companion_memories",
    "companion_affection",
    "memory",
)


class ReleaseError(RuntimeError):
    pass


def run(argv: list[str], *, timeout: int = 600, **kwargs: Any) -> str:
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, **kwargs
    )
    if result.returncode:
        raise ReleaseError(
            f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: "
            f"{result.stderr.strip()[:300]}"
        )
    return result.stdout


# ── 1. artifact ──────────────────────────────────────────────────────────────


def verify_signature(path: Path, signers: Path = SIGNERS) -> None:
    signature = path.with_name(path.name + ".sig")
    if not signature.is_file():
        raise ReleaseError("provenance is not signed")
    with path.open("rb") as data:
        result = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(signers),
                "-I",
                SIGNER_ID,
                "-n",
                NAMESPACE,
                "-s",
                str(signature),
            ],
            stdin=data,
            capture_output=True,
            timeout=60,
        )
    if result.returncode:
        raise ReleaseError("provenance signature does not verify")


def verify_artifact(
    revision: str, releases: Path = RELEASES, signers: Path = SIGNERS
) -> dict[str, Any]:
    directory = releases / revision
    provenance_path = directory / "provenance.json"
    verify_signature(provenance_path, signers)
    provenance = json.loads(provenance_path.read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    release_artifact.check_manifest(manifest, revision)
    if provenance.get("revision") != revision or provenance.get(
        "manifest_sha256"
    ) != release_artifact.sha256(directory / "manifest.json"):
        raise ReleaseError("signed provenance does not bind this manifest")
    materials = release_artifact.verify_materials(directory, manifest)
    tag = f"klukai-core:ci-{revision[:7]}"
    found = release_artifact.identity(tag)
    if found["layers"] != manifest["layers"] or found["revision"] != revision:
        raise ReleaseError("local image differs from the signed CI manifest")
    image_id = run(["docker", "image", "inspect", "-f", "{{.Id}}", tag]).strip()
    return {
        "tag": tag,
        "image_id": image_id,
        "ci_run": provenance.get("ci_run"),
        "layers": len(manifest["layers"]),
        **materials,
    }


# ── 2. source ────────────────────────────────────────────────────────────────


def verify_source(revision: str, repo: Path = REPO) -> None:
    head = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()
    if head != revision:
        raise ReleaseError(f"main checkout is at {head[:12]}, not {revision[:12]}")
    changed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--", "docker-compose.yml"]
    ).returncode
    if changed:
        raise ReleaseError("docker-compose.yml differs from the release revision")


# ── 3. backup + isolated restore ─────────────────────────────────────────────


def _counts(container: str, database: str) -> dict[str, int]:
    tables = " ".join(f"union all select '{t}', count(*) from {t}" for t in KEY_TABLES)
    query = (
        "select 'tables', count(*) from information_schema.tables "
        "where table_schema='public' and table_type='BASE TABLE' " + tables
    )
    out = run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-AtF",
            "\t",
            "-c",
            query,
        ]
    )
    return {
        k: int(v) for k, v in (line.split("\t") for line in out.splitlines() if line)
    }


def backup_and_verify(
    backups: Path = BACKUPS, live: str = "infra-postgres"
) -> dict[str, Any]:
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = _counts(live, "aichat")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump = backups / f"aichat-{stamp}.dump"
    fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        result = subprocess.run(
            ["docker", "exec", live, "pg_dump", "-U", "postgres", "-Fc", "aichat"],
            stdout=out,
            stderr=subprocess.PIPE,
            timeout=900,
        )
    if result.returncode or dump.stat().st_size == 0:
        raise ReleaseError("logical backup failed")
    image = run(["docker", "inspect", "-f", "{{.Image}}", live]).strip()
    name = f"klukai-restore-verify-{stamp.lower()}"
    run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "-e",
            f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
            image,
        ]
    )
    try:
        # The image's entrypoint first runs a temporary init server on the
        # unix socket only, then restarts; only the final server listens on
        # TCP, so readiness and every later command use 127.0.0.1.
        for _ in range(90):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                ],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise ReleaseError("isolated restore database did not start")
        run(
            [
                "docker",
                "exec",
                name,
                "createdb",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "aichat",
            ]
        )
        with dump.open("rb") as data:
            restored = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "pg_restore",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "aichat",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                stdin=data,
                capture_output=True,
                timeout=900,
            )
        if restored.returncode:
            raise ReleaseError("backup does not restore cleanly")
        after = _counts(name, "aichat")
    finally:
        # -v: the postgres image declares a data VOLUME; without it every
        # deploy would leave an anonymous volume holding a restored copy.
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
    if after["tables"] != before["tables"] or any(
        after[t] < before[t] for t in KEY_TABLES
    ):
        raise ReleaseError("restored backup is missing tables or rows")
    return {
        "dump": str(dump),
        "bytes": dump.stat().st_size,
        "live_before": before,
        "restored": after,
    }


# ── 4/5. rollout engine (shared with the drill) ──────────────────────────────


@dataclass
class Target:
    """Where a release lands: how to recreate the service and probe it."""

    compose: Callable[[list[str]], None]
    container: str
    pin: Path
    ready: Callable[[], bool]
    errors: Callable[[float], float | None] = lambda since: None
    service: str = SERVICE
    rollback_repo: str = "klukai-core"
    log: list[str] = field(default_factory=list)

    def inspect(self, template: str) -> str:
        result = subprocess.run(
            ["docker", "inspect", "-f", template, self.container],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def write_pin(self, image: str | None) -> None:
        if image is None:
            self.pin.unlink(missing_ok=True)
            return
        self.pin.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = self.pin.with_suffix(".tmp")
        staged.write_text(
            json.dumps({"services": {self.service: {"image": image}}}, indent=2) + "\n"
        )
        os.replace(staged, self.pin)

    def read_pin(self) -> str | None:
        if not self.pin.is_file():
            return None
        return json.loads(self.pin.read_text())["services"][self.service]["image"]

    def recreate(self) -> None:
        self.compose(["up", "-d", "--no-build", "--no-deps", self.service])


def watch(
    target: Target,
    image_id: str,
    *,
    ready_timeout: float,
    hold: float,
    interval: float = 5,
    max_failures: int = 3,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    deadline = clock() + ready_timeout
    while True:
        healthy = (
            target.inspect("{{.Image}}") == image_id
            and target.inspect("{{.State.Health.Status}}") == "healthy"
        )
        if healthy and target.ready():
            break
        if clock() > deadline:
            return False, "candidate did not become healthy and ready in time"
        sleep(interval)
    restarts = target.inspect("{{.RestartCount}}")
    started = time.time()
    end, failures = clock() + hold, 0
    while clock() < end:
        sleep(interval)
        ok = (
            target.inspect("{{.Image}}") == image_id
            and target.inspect("{{.State.Health.Status}}") == "healthy"
            and target.inspect("{{.RestartCount}}") == restarts
            and target.ready()
        )
        failures = 0 if ok else failures + 1
        if failures >= max_failures:
            return False, f"{failures} consecutive failed checks during the hold"
    if failures:
        return False, f"the hold ended with {failures} failed check(s)"
    burst = target.errors(started)
    if burst is None:
        target.log.append("5xx check not evaluated (no request data in the window)")
    else:
        target.log.append(f"5xx ratio {burst:.2%} during the hold")
        if burst > 0.05:
            return False, f"5xx ratio {burst:.1%} during the hold"
    return True, "healthy and ready for the whole hold window"


def rollout(
    target: Target,
    image: str,
    image_id: str,
    *,
    ready_timeout: float,
    hold: float,
    **watch_args: Any,
) -> dict[str, Any]:
    previous_id = target.inspect("{{.Image}}")
    if not previous_id:
        raise ReleaseError(
            f"{target.container} is not running; nothing to roll back to"
        )
    # An immutable tag for exactly the running image: the rollback target
    # cannot move even if the previous tag is reused meanwhile.
    previous_pin = (
        f"{target.rollback_repo}:rollback-"
        f"{datetime.now(UTC).strftime('%Y%m%dt%H%M%S')}-{previous_id[7:19]}"
    )
    run(["docker", "tag", previous_id, previous_pin])
    common = {
        "image_id": image_id,
        "previous_image_id": previous_id,
        "rollback_tag": previous_pin,
    }
    # From here production may already be changed: any failure, timeout,
    # SIGTERM (main turns it into SystemExit) or Ctrl-C must restore the
    # previous image rather than escape with the candidate pinned.
    target.write_pin(image)
    try:
        target.recreate()
        ok, detail = watch(
            target, image_id, ready_timeout=ready_timeout, hold=hold, **watch_args
        )
    except BaseException as error:  # noqa: BLE001 - restore on every escape
        ok, detail = (
            False,
            f"rollout interrupted: {type(error).__name__}: {error}"[:300],
        )
    if ok:
        return {"status": "deployed", "detail": detail, "checks": target.log, **common}
    try:
        target.write_pin(previous_pin)
        target.recreate()
        back, back_detail = watch(
            target,
            previous_id,
            ready_timeout=ready_timeout,
            hold=min(hold, 30),
            **watch_args,
        )
    except BaseException as error:  # noqa: BLE001 - report, never hide
        back, back_detail = (
            False,
            f"rollback raised {type(error).__name__}: {error}"[:300],
        )
    return {
        "status": "rolled_back" if back else "rollback_failed",
        "detail": detail,
        "rollback_detail": back_detail,
        "checks": target.log,
        **common,
    }


# ── production wiring ────────────────────────────────────────────────────────


def http_ready(url: str = READY_URL) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def error_query(window: int) -> str:
    """Core labels responses by status_class ("5xx"), not by status code."""
    errors = (
        'sum(increase(klukai_http_requests_total{job="klukai-core",'
        f'status_class="5xx"}}[{window}s]))'
    )
    total = f'sum(increase(klukai_http_requests_total{{job="klukai-core"}}[{window}s]))'
    return f"({errors} or vector(0)) / {total}"


def error_ratio(since: float, prometheus: str = PROMETHEUS) -> float | None:
    """5xx share of core requests since the hold began; None if unknown."""
    query = error_query(max(int(time.time() - since), 60))
    try:
        with urllib.request.urlopen(
            f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": query}),
            timeout=10,
        ) as response:
            result = json.load(response)["data"]["result"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    if not result:
        return None
    value = float(result[0]["value"][1])
    return value if value == value else None  # NaN when there was no traffic


def production_target() -> Target:
    import klukai_compose
    import klukai_secrets

    def compose(args: list[str]) -> None:
        with klukai_secrets.materialized(klukai_secrets.NAMES) as paths:
            run(klukai_compose.argv(paths, args), cwd=REPO, timeout=900)

    return Target(
        compose=compose,
        container=SERVICE,
        pin=klukai_compose.PIN,
        ready=http_ready,
        errors=error_ratio,
    )


def receipt(doc: dict[str, Any]) -> Path:
    directory = STATE / "receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = (
        directory
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{doc['command']}.json"
    )
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


IRREVERSIBLE_PATHS = ("docker/core/migrations", "docker-compose.yml")


def rollback_safety(target_revision: str, repo: Path = REPO) -> dict[str, Any]:
    """Rolling back restores only the image. Core applies migrations at startup
    and refuses to start when an applied migration file is missing, and a
    rollback recreates with the new revision's compose file, so a change to
    either cannot be undone automatically."""
    running = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            SERVICE,
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if (
        not re_full_sha(running)
        or subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{running}^{{commit}}"],
            capture_output=True,
        ).returncode
    ):
        return {
            "running_revision": running or None,
            "safe": False,
            "reason": "running image has no known source revision",
        }
    changed = run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            running,
            target_revision,
            "--",
            *IRREVERSIBLE_PATHS,
        ]
    ).split()
    return {
        "running_revision": running,
        "safe": not changed,
        "changed": changed,
        "reason": "migrations or compose changed" if changed else "",
    }


def re_full_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def prune(
    backups: Path = BACKUPS, keep_dumps: int = 10, keep_tags: int = 5
) -> dict[str, int]:
    """Bound release dumps and rollback tags (newest kept)."""
    dumps = sorted(backups.glob("aichat-*.dump"))
    for old in dumps[:-keep_dumps]:
        old.unlink()
    tags = sorted(
        t
        for t in run(
            [
                "docker",
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
                "klukai-core",
            ]
        ).split()
        if ":rollback-20" in t
    )
    stale = tags[:-keep_tags]
    if stale:
        subprocess.run(["docker", "image", "rm", *stale], capture_output=True)
    return {
        "dumps_removed": max(len(dumps) - keep_dumps, 0),
        "tags_removed": len(stale),
    }


def deploy(
    revision: str,
    hold: float,
    ready_timeout: float,
    accept_manual_rollback: bool = False,
) -> dict[str, Any]:
    release_artifact._revision(revision)
    doc: dict[str, Any] = {
        "command": "deploy",
        "revision": revision,
        "started_at": datetime.now(UTC).isoformat(),
    }
    doc["artifact"] = verify_artifact(revision)
    verify_source(revision)
    doc["rollback_safety"] = rollback_safety(revision)
    if not doc["rollback_safety"]["safe"]:
        if not accept_manual_rollback:
            raise ReleaseError(
                "automatic rollback cannot undo this release ("
                + doc["rollback_safety"]["reason"]
                + "); review it and rerun with --accept-manual-rollback"
            )
        doc["rollback_safety"]["accepted_by_operator"] = True
    doc["backup"] = backup_and_verify()
    target = production_target()
    if not target.pin.exists():
        # First managed release: pin what is running now so a rollback has a target.
        current = target.inspect("{{.Config.Image}}")
        if not current:
            raise ReleaseError("companion-core is not running")
        target.write_pin(current)
    doc["rollout"] = rollout(
        target,
        doc["artifact"]["tag"],
        doc["artifact"]["image_id"],
        ready_timeout=ready_timeout,
        hold=hold,
    )
    if doc["rollout"]["status"] == "deployed":
        try:
            doc["pruned"] = prune()
        except (ReleaseError, OSError) as error:  # housekeeping never masks a deploy
            doc["pruned"] = {"error": str(error)[:200]}
    doc["finished_at"] = datetime.now(UTC).isoformat()
    return doc


# ── drill: the same engine against a disposable service ──────────────────────

DRILL_APP = (
    "import http.server,os,sys\n"
    "if os.environ.get('KLUKAI_DRILL_FAIL'): sys.exit(3)\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200 if self.path == '/ready' else 404); self.end_headers()\n"
    "    def log_message(self, *a): pass\n"
    "http.server.HTTPServer(('0.0.0.0', 8080), H).serve_forever()\n"
)


def drill(base_image: str, port: int = 18399) -> dict[str, Any]:
    """Good release succeeds; a broken release is rolled back automatically."""
    project = "klukai-release-drill"
    work = Path(tempfile.mkdtemp(prefix="klukai-release-drill-"))
    compose_file = work / "compose.yml"
    compose_file.write_text(
        json.dumps(
            {
                "services": {
                    "app": {
                        "image": base_image,
                        "container_name": f"{project}-app",
                        "entrypoint": ["/opt/venv/bin/python", "-c", DRILL_APP],
                        "ports": [f"127.0.0.1:{port}:8080"],
                        "restart": "unless-stopped",
                        "healthcheck": {
                            "test": [
                                "CMD",
                                "/opt/venv/bin/python",
                                "-c",
                                "import urllib.request as u;"
                                "u.urlopen('http://127.0.0.1:8080/ready',timeout=3)",
                            ],
                            "interval": "2s",
                            "timeout": "3s",
                            "retries": 3,
                        },
                    }
                }
            }
        )
    )
    good, bad = f"{project}:good", f"{project}:bad"
    run(["docker", "tag", base_image, good])
    run(
        ["docker", "build", "-q", "-t", bad, "-"],
        input=f"FROM {base_image}\nENV KLUKAI_DRILL_FAIL=1\n",
    )

    def compose(args: list[str]) -> None:
        run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(compose_file),
                "-f",
                str(work / "pin.json"),
                *args,
            ]
        )

    target = Target(
        compose=compose,
        container=f"{project}-app",
        pin=work / "pin.json",
        ready=lambda: http_ready(f"http://127.0.0.1:{port}/ready"),
        service="app",
        rollback_repo=project,
    )

    def image_id(tag: str) -> str:
        return run(["docker", "image", "inspect", "-f", "{{.Id}}", tag]).strip()

    try:
        target.write_pin(good)
        target.recreate()
        ok, detail = watch(target, image_id(good), ready_timeout=60, hold=4, interval=1)
        if not ok:
            raise ReleaseError(f"drill baseline did not start: {detail}")
        broken = rollout(
            target, bad, image_id(bad), ready_timeout=20, hold=4, interval=1
        )
        after = target.inspect("{{.Image}}")
        result = {
            "command": "drill",
            "broken_release": broken,
            "running_after": "good" if after == image_id(good) else after,
            "pin_after": target.read_pin(),
        }
        result["passed"] = (
            broken["status"] == "rolled_back"
            and result["running_after"] == "good"
            and result["pin_after"] == broken["rollback_tag"]
        )
        return result
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(compose_file),
                "down",
                "--remove-orphans",
            ],
            capture_output=True,
        )
        drill_tags = run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", project]
        ).split()
        subprocess.run(
            ["docker", "image", "rm", good, bad, *drill_tags], capture_output=True
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("deploy")
    d.add_argument("revision")
    d.add_argument("--hold", type=float, default=180)
    d.add_argument("--ready-timeout", type=float, default=180)
    d.add_argument(
        "--accept-manual-rollback",
        action="store_true",
        help="deploy although migrations or docker-compose.yml changed since the "
        "running image (automatic rollback cannot undo those)",
    )
    r = sub.add_parser("drill")
    r.add_argument("--image", required=True, help="a verified klukai-core:ci-* tag")
    args = parser.parse_args()
    # SIGTERM becomes SystemExit, so an interrupted rollout still rolls back
    # and plaintext secrets in tmpfs are still removed.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    try:
        doc = (
            deploy(
                args.revision,
                args.hold,
                args.ready_timeout,
                accept_manual_rollback=args.accept_manual_rollback,
            )
            if args.command == "deploy"
            else drill(args.image)
        )
    except (ReleaseError, ValueError, OSError, subprocess.SubprocessError) as error:
        # Raised only before production changes: rollout() handles its own.
        doc = {"command": args.command, "status": "refused", "error": str(error)}
        print(json.dumps(doc, indent=2), file=sys.stderr)
        receipt(doc)
        return 1
    path = receipt(doc)
    print(json.dumps(doc, indent=2, sort_keys=True))
    print(f"receipt: {path}")
    status = (
        doc.get("rollout", {}).get("status")
        if args.command == "deploy"
        else ("passed" if doc.get("passed") else "failed")
    )
    return 0 if status in ("deployed", "passed") else 2


if __name__ == "__main__":
    sys.exit(main())
