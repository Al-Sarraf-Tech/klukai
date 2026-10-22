#!/usr/bin/env python3
"""Disposable staging for deploy-on-merge: the real pipeline against throwaway targets.

Real: the controller (klukai-deploy-on-merge.py) and its decisions; GitHub's CI
records for real commits on main (read-only); the CI artifacts those runs
retained on amarillo; relay verification and signing (with a throwaway key);
pg_dump plus isolated restore of a throwaway PostgreSQL; klukai-release's
rollout, hold and rollback engine; the atomic web release; and the CI-built
core image serving the web root through a read-only bind mount exactly as
production mounts it (StaticFiles + the `/` login route).

Disposable: a staging git remote and checkout, a stand-in core service running
the CI-built images, the database, the web root, all state and the signing key.
Production (companion-core, its database, ~/git/klukai, the release key and
production state) is never touched; staging rollback tags use their own
repository name so production's tag pruning never sees them.

    GH_TOKEN=... klukai-deploy-staging.py --root <empty dir> all
    klukai-deploy-staging.py --root <dir> cleanup
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))


def _load(name: str, filename: str) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


dom = _load("klukai_deploy_on_merge", "klukai-deploy-on-merge.py")
release, relay, web = dom.release, dom.relay, dom.web
import klukai_deploy_lock  # noqa: E402

SOURCE_REPO = Path.home() / "git/klukai"
PRODUCTION_PIN = Path.home() / ".local/state/klukai/release/core-image.json"
PROJECT = "klukai-staging"
APP, PG, WEBSRV = f"{PROJECT}-app", f"{PROJECT}-pg", f"{PROJECT}-web"
CORE_PORT, WEB_PORT = 18401, 18402
CORE_URL, WEB_URL = f"http://127.0.0.1:{CORE_PORT}", f"http://127.0.0.1:{WEB_PORT}"
# Real commits and what their real CI runs say. Artifacts must still be retained
# on amarillo (the archive keeps the newest few), so these move forward over time.
BASE = "edb30e1e22209fa731bbf470a80ff63b466e17be"  # full CI green (run 39729400999)
C1 = "07808296aa069f283ab034520fc57dc928c75e70"  # full CI green, artifacts retained
C2 = "4ccc070c5c681cb9c118164f34e3ee8106ccec96"  # full CI green, artifacts retained
SELECTIVE = "376f4cd9febe0b3a52c0c7e1565695b151471171"  # its run failed (frontend), then was cancelled
CANCELLED = "66829d0f5925cef6a469ad9e73d81faf5fa960da"  # its CI run was cancelled

STAND_IN = (
    "import http.server,os,sys\n"
    "if os.environ.get('KLUKAI_DRILL_FAIL'): sys.exit(3)\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        ok = self.path in ('/ready', '/api/health/ready')\n"
    "        self.send_response(200 if ok else 404); self.end_headers()\n"
    "    def log_message(self, *a): pass\n"
    "http.server.HTTPServer(('0.0.0.0', 8080), H).serve_forever()\n"
)
# Core's own static serving: CachedStaticFiles at /app and the `/` login route
# (docker/core/app/main.py and routes_entry.py), from the image under test.
STATIC_APP = (
    "import uvicorn\n"
    "from pathlib import Path\n"
    "from fastapi import FastAPI\n"
    "from fastapi.responses import FileResponse\n"
    "from app.static_cache import CachedStaticFiles\n"
    "app = FastAPI()\n"
    "@app.get('/')\n"
    "async def root():\n"
    "    p = Path('/app/static/login.html')\n"
    "    if p.exists():\n"
    "        return FileResponse(p, media_type='text/html', headers={'Cache-Control': 'no-cache'})\n"
    "    return {'status': 'no login'}\n"
    "app.mount('/app', CachedStaticFiles(directory='/app/static', html=True), name='pwa')\n"
    "uvicorn.run(app, host='0.0.0.0', port=8080, log_level='warning')\n"
)


def sh(argv: list[str], **kw: Any) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=kw.pop("timeout", 600), **kw)
    if result.returncode:
        raise RuntimeError(f"{argv[:3]} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return sh(["git", "-C", str(repo), *args])


class Stage:
    """Paths, disposable services and fault switches for one staging run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote, self.checkout = root / "remote.git", root / "checkout"
        self.releases, self.state, self.webstate = root / "releases", root / "state", root / "webstate"
        self.webroot, self.backups, self.keys = root / "webroot", root / "backups", root / "keys"
        self.compose_file, self.pin = root / "compose.json", root / "pin.json"
        self.artifacts = relay.LOCAL_ROOTS[relay.ARTIFACTS]
        self.signers = self.keys / "allowed_signers"
        self.notes: list[str] = []
        self.hold = 15.0
        self.fault: str | None = None

    # ── services ──────────────────────────────────────────────────────────────

    def compose(self, args: list[str]) -> None:
        sh(["docker", "compose", "-p", PROJECT, "-f", str(self.compose_file), "-f", str(self.pin), *args])

    def target(self) -> Any:
        return release.Target(
            compose=self.compose, container=APP, pin=self.pin, service="app",
            ready=lambda: release.http_ready(f"{CORE_URL}/ready"), rollback_repo=PROJECT)

    def baseline_image(self) -> str:
        return json.loads(PRODUCTION_PIN.read_text())["services"]["companion-core"]["image"]

    def setup(self) -> dict[str, Any]:
        for path in (self.releases, self.state, self.webstate, self.backups, self.keys):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        sh(["git", "clone", "-q", "--bare", "--shared", str(SOURCE_REPO), str(self.remote)])
        git(self.remote, "update-ref", "refs/heads/main", BASE)
        sh(["git", "clone", "-q", "--shared", str(self.remote), str(self.checkout)])
        git(self.checkout, "checkout", "-q", "--detach", BASE)
        key = self.keys / "release"
        sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "staging", "-f", str(key)])
        self.signers.write_text('klukai-release@amarillo namespaces="klukai-release" '
                                + key.with_suffix(".pub").read_text())
        # A throwaway "live" database with the tables the backup gate counts.
        sh(["docker", "run", "-d", "--name", PG, "--network", "none",
            "-e", f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}", "postgres:17-alpine"])
        for _ in range(60):
            if subprocess.run(["docker", "exec", PG, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(1)
        sh(["docker", "exec", PG, "createdb", "-h", "127.0.0.1", "-U", "postgres", "aichat"])
        ddl = "".join(f"create table {t} (id serial primary key, body text);"
                      f"insert into {t} (body) select 'row ' || g from generate_series(1, 25) g;"
                      for t in release.KEY_TABLES)
        sh(["docker", "exec", PG, "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", "aichat", "-qc", ddl])
        # The stand-in core service, first on the image production runs now.
        self.compose_file.write_text(json.dumps({"services": {"app": {
            "container_name": APP, "image": "placeholder",
            "entrypoint": ["/opt/venv/bin/python", "-c", STAND_IN],
            "ports": [f"127.0.0.1:{CORE_PORT}:8080"],
            "healthcheck": {"test": ["CMD", "/opt/venv/bin/python", "-c",
                                     "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/ready',timeout=3)"],
                            "interval": "2s", "timeout": "3s", "retries": 3}}}}))
        target = self.target()
        target.write_pin(self.baseline_image())
        target.recreate()
        baseline_id = sh(["docker", "image", "inspect", "-f", "{{.Id}}", self.baseline_image()])
        ok, detail = release.watch(target, baseline_id, ready_timeout=90, hold=4, interval=1)
        if not ok:
            raise RuntimeError(f"stand-in baseline did not start: {detail}")
        # The web root, adopted from a copy of the release production serves
        # today, and served by the real core image through a read-only bind mount.
        live = self.root / "web-build-copy"
        shutil.copytree((Path.home() / ".local/share/klukai/web/.current").resolve(), live)
        adopted = web.adopt(live, root=self.webroot, state=self.webstate)
        sh(["docker", "run", "-d", "--name", WEBSRV, "--user", "1000:1000",
            "-p", f"127.0.0.1:{WEB_PORT}:8080", "-v", f"{self.webroot}:/app/static:ro",
            "--entrypoint", "/opt/venv/bin/python", self.baseline_image(), "-c", STATIC_APP])
        for _ in range(60):
            try:
                if web.http_get(f"{WEB_URL}/")[0] == 200:
                    break
            except OSError:
                pass  # docker's port proxy resets until uvicorn listens
            time.sleep(1)
        smoke = web.smoke(WEB_URL, self.webroot, adopted["release"])
        deployer = self.deployer()
        st = deployer.load()
        st["deployed"] = {"revision": BASE, "baseline": True}
        deployer.save(st)
        return {"baseline_image_id": baseline_id, "adopted": adopted, "adopted_smoke": len(smoke)}

    def reset(self) -> None:
        """Back to the baseline: core image, web release, checkout and state."""
        target = self.target()
        target.write_pin(self.baseline_image())
        target.recreate()
        baseline_id = sh(["docker", "image", "inspect", "-f", "{{.Id}}", self.baseline_image()])
        ok, detail = release.watch(target, baseline_id, ready_timeout=90, hold=2, interval=1)
        if not ok:
            raise RuntimeError(f"reset: {detail}")
        adopted = next(r for r in web.status(self.webroot, self.webstate)["staged"] if r.startswith("adopted-"))
        if web.current(self.webroot) != adopted:
            web.switch(self.webroot, adopted)
        saved = json.loads((self.webstate / "state.json").read_text())
        saved.update(current=adopted, previous=None)
        (self.webstate / "state.json").write_text(json.dumps(saved))
        git(self.remote, "update-ref", "refs/heads/main", BASE)
        git(self.checkout, "checkout", "-q", "--detach", BASE)
        # Each scenario relays afresh: reused artifacts would mask what it tests.
        shutil.rmtree(self.releases, ignore_errors=True)
        self.releases.mkdir(mode=0o700)
        (self.state / "state.json").write_text(json.dumps({"deployed": {"revision": BASE, "baseline": True},
                                                           "candidates": {}, "notified": []}))
        self.fault = None

    def cleanup(self) -> None:
        subprocess.run(["docker", "rm", "-f", PG, WEBSRV], capture_output=True)
        if self.compose_file.exists():
            subprocess.run(["docker", "compose", "-p", PROJECT, "-f", str(self.compose_file), "down",
                            "--remove-orphans"], capture_output=True)
        tags = sh(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]).split()
        staged = [t for t in tags if t.startswith(f"{PROJECT}:")
                  or any(t.startswith(f"klukai-core:{kind}-{sha[:7]}") for sha in (C1, C2)
                         for kind in ("ci", "verified"))]
        if staged:
            subprocess.run(["docker", "image", "rm", *staged], capture_output=True)
        for big in self.releases.glob("*/core-image.tar"):
            big.unlink()

    def deployer(self, **overrides: Any) -> StagingDeployer:
        settings: dict[str, Any] = {
            "remote_url": str(self.remote), "checkout": self.checkout, "state": self.state,
            "releases": self.releases, "signing_key": self.keys / "release",
            "hold": self.hold, "ready_timeout": 60, "e2e": False}
        cfg = dom.Config(**(settings | overrides))
        return StagingDeployer(cfg, os.environ.get("GH_TOKEN"), self)


class StagingDeployer(dom.Deployer):
    """The production controller with every effect pointed at the stage."""

    def __init__(self, cfg: Any, token: str | None, stage: Stage) -> None:
        super().__init__(cfg, token)
        self.stage = stage

    core_container = APP

    def main_head(self) -> str:
        return git(self.stage.remote, "rev-parse", "refs/heads/main")

    def guest(self) -> Any:
        return relay.LocalGuest({relay.ARTIFACTS: self.stage.artifacts})

    def core_target(self) -> Any:
        return self.stage.target()

    def deploy_core(self, revision: str, accept: bool, plans: dict[str, str] | None) -> dict[str, Any]:
        """klukai-release.deploy itself, pointed at the stage: signature,
        source, rollback safety, backup + isolated restore, rollout, hold."""
        verify = release.verify_artifact
        if self.stage.fault == "core-unhealthy":
            def broken(rev: str, releases: Path, signers: Path) -> dict[str, Any]:
                # The verified release fails its health checks once running.
                found = verify(rev, releases, signers)
                tag = f"{PROJECT}:broken-{rev[:7]}"
                sh(["docker", "build", "-q", "-t", tag, "-"], input=f"FROM {found['tag']}\nENV KLUKAI_DRILL_FAIL=1\n")
                return {**found, "tag": tag, "image_id": sh(["docker", "image", "inspect", "-f", "{{.Id}}", tag])}
            release.verify_artifact = broken
        try:
            return release.deploy(
                revision, self.cfg.hold, self.cfg.ready_timeout, accept_manual_rollback=accept, plans=plans,
                releases=self.cfg.releases, signers=self.stage.signers, repo=self.cfg.checkout,
                target=self.stage.target(), container=APP,
                backup=lambda: release.backup_and_verify(self.stage.backups, live=PG))
        finally:
            release.verify_artifact = verify

    def deploy_web(self, revision: str) -> dict[str, Any]:
        original = web.switch
        if self.stage.fault == "web-unreadable":
            def broken(root: Path, rid: str) -> None:
                if rid.startswith(revision[:12]):
                    web.tree(root, rid).chmod(0)  # core's user can no longer read the new release
                original(root, rid)
            web.switch = broken
        try:
            return web.deploy(revision, root=self.stage.webroot, releases=self.cfg.releases,
                              state=self.stage.webstate, base=WEB_URL, signers=self.stage.signers)
        finally:
            web.switch = original

    def web_current(self) -> str | None:
        return web.current(self.stage.webroot)

    def web_switch_back(self, rid: str) -> dict[str, Any]:
        return web.rollback(rid, root=self.stage.webroot, state=self.stage.webstate, base=WEB_URL,
                            allow_live=True)

    def verify(self) -> dict[str, Any]:
        ready = release.http_ready(f"{CORE_URL}/ready")
        live = web.current(self.stage.webroot)
        checks = web.smoke(WEB_URL, self.stage.webroot, live) if live else []
        ok = ready and bool(checks) and self.stage.fault != "verify-fails"
        return {"ok": ok, "ready": ready, "web_checks": len(checks), "fault": self.stage.fault}

    def notify(self, text: str) -> None:
        self.stage.notes.append(text)


# ── observations ──────────────────────────────────────────────────────────────


def observe(stage: Stage) -> dict[str, Any]:
    st = json.loads((stage.state / "state.json").read_text())
    image = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", APP], capture_output=True,
                           text=True).stdout.strip()
    live = web.current(stage.webroot)
    try:
        status, body, _ = web.http_get(f"{WEB_URL}/app/")
    except OSError:
        status, body = 0, b""
    served_index = (web.hashlib.sha256(body).hexdigest() if status == 200 else None)
    try:
        expected_index = web.digest(web.tree(stage.webroot, live) / "index.html") if live else None
    except OSError:
        expected_index = "unreadable"
    return {
        "checkout": git(stage.checkout, "rev-parse", "HEAD"),
        "deployed": st.get("deployed", {}).get("revision"),
        "inflight": st.get("inflight"),
        "core_image": image,
        "core_revision": subprocess.run(["docker", "inspect", "-f",
                                         '{{index .Config.Labels "org.opencontainers.image.revision"}}', APP],
                                        capture_output=True, text=True).stdout.strip(),
        "web_release": live,
        "served_index_matches_live": served_index == expected_index,
    }


def scenario(name: str, run: Callable[[], dict[str, Any]], check: Callable[[dict[str, Any]], bool],
             stage: Stage, results: list[dict[str, Any]]) -> None:
    started = time.monotonic()
    notes_before = len(stage.notes)
    try:
        outcome = run()
        error = None
    except Exception as exc:  # noqa: BLE001 - recorded as a failed scenario
        outcome, error = {}, f"{type(exc).__name__}: {exc}"[:400]
    seen = observe(stage)
    record = {"scenario": name, "outcome": outcome, "observed": seen, "notes": stage.notes[notes_before:],
              "seconds": round(time.monotonic() - started, 1), "error": error}
    record["passed"] = error is None and bool(check({**record}))
    results.append(record)
    print(f"{'PASS' if record['passed'] else 'FAIL'} {name} ({record['seconds']}s)"
          f" → {outcome.get('status', error)}", flush=True)


def cycle_subprocess(root: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "cycle"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=os.environ.copy())


def wait_for(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def run_all(stage: Stage) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    setup = stage.setup()
    results.append({"scenario": "setup", "outcome": setup, "observed": observe(stage), "passed": True})
    print(f"setup: baseline {setup['baseline_image_id'][7:19]}, adopted {setup['adopted']['release']}", flush=True)
    d = stage.deployer
    base_state = observe(stage)

    def unchanged(r: dict[str, Any]) -> bool:
        o = r["observed"]
        return (o["checkout"] == BASE and o["core_image"] == base_state["core_image"]
                and o["web_release"] == base_state["web_release"] and o["inflight"] is None)

    # 1-2. Failed or non-certifying CI never deploys.
    git(stage.remote, "update-ref", "refs/heads/main", SELECTIVE)
    scenario("failed-ci: a failed run is not certification", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "ci_failed" and unchanged(r), stage, results)
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", CANCELLED)
    scenario("failed-ci: cancelled run", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "ci_failed" and unchanged(r), stage, results)

    # 3-5. Invalid artifacts are refused before production changes.
    for label, mutate in (("tampered PWA file", "pwa"), ("core manifest not the one CI logged", "manifest")):
        stage.reset()
        copy = stage.root / f"artifacts-{mutate}"
        shutil.rmtree(copy, ignore_errors=True)
        run_dir = next((relay.LOCAL_ROOTS[relay.ARTIFACTS] / C1).iterdir())
        target = copy / C1 / run_dir.name
        shutil.copytree(run_dir, target, ignore=shutil.ignore_patterns("release-buildkit", "perf", "backends",
                                                                       "core", "ci-image", "ci-tool-image"))
        if mutate == "pwa":
            (target / "frontend/pwa/login.html").write_text("tampered\n")
        else:
            manifest = target / "release/manifest.json"
            doc = json.loads(manifest.read_text())
            doc["tools"]["tampered"] = "yes"
            manifest.write_text(json.dumps(doc))
        stage.artifacts = copy
        git(stage.remote, "update-ref", "refs/heads/main", C1)
        scenario(f"invalid-artifact: {label}", lambda: d().run_once(),
                 lambda r: r["outcome"]["status"] == "error" and unchanged(r), stage, results)
        stage.artifacts = relay.LOCAL_ROOTS[relay.ARTIFACTS]
        shutil.rmtree(copy, ignore_errors=True)
    stage.reset()
    rogue = stage.keys / "rogue"
    sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(rogue)])
    git(stage.remote, "update-ref", "refs/heads/main", C1)
    scenario("invalid-artifact: provenance signed by an untrusted key",
             lambda: d(signing_key=rogue).run_once(),
             lambda r: r["outcome"]["status"] == "core_refused" and unchanged(r), stage, results)

    # 5b. Reused artifacts are re-verified at deploy: an edit after signing is refused.
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", C1)

    def altered() -> dict[str, Any]:
        deployer = d()
        run = deployer.certification(C1)["run"]
        deployer.relay_artifacts(C1, run, True)
        provenance = stage.releases / C1 / "provenance.json"
        doc = json.loads(provenance.read_text())
        doc["verified_by"] = "someone else"
        provenance.write_text(json.dumps(doc))
        assert deployer.relayed(C1, run)["core"]  # looks relayed, so it is reused...
        return d().run_once()  # ...and must still be refused by the signature check

    scenario("invalid-artifact: relayed provenance edited after signing", altered,
             lambda r: r["outcome"]["status"] == "core_refused" and unchanged(r), stage, results)

    # 6. A certified merge deploys end to end.
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", C1)
    scenario("success: certified merge deploys core and web, verified", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "deployed" and r["observed"]["checkout"] == C1
             and r["observed"]["core_revision"] == C1 and r["observed"]["deployed"] == C1
             and r["observed"]["web_release"].startswith(C1[:12])
             and r["observed"]["served_index_matches_live"], stage, results)
    after_c1 = observe(stage)

    # 7. An older commit can never overwrite a newer release.
    git(stage.remote, "update-ref", "refs/heads/main", BASE)
    scenario("stale: main rewound to an older commit is refused", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "refused" and "does not descend" in r["outcome"]["detail"]
             and r["observed"]["checkout"] == C1 and r["observed"]["core_image"] == after_c1["core_image"],
             stage, results)

    # 8. Concurrent merges: a running deployment finishes; a second run is refused.
    git(stage.remote, "update-ref", "refs/heads/main", C2)

    def concurrent() -> dict[str, Any]:
        first = cycle_subprocess(stage.root)
        recreated = wait_for(lambda: observe(stage)["core_revision"] == C2, 240)
        second = cycle_subprocess(stage.root)
        second_out = second.communicate(timeout=120)[0]
        # Another merge lands while the first is mid-hold (after its preflight).
        git(stage.remote, "update-ref", "refs/heads/main", SELECTIVE)
        first_out = first.communicate(timeout=600)[0]
        follow_up = d().run_once()
        return {"status": "ok", "core_recreated_during_first": recreated,
                "first": first_out.strip().splitlines()[-1:], "first_exit": first.returncode,
                "second_exit": second.returncode, "second_refused_busy": "another Klukai deployment" in second_out,
                "follow_up": follow_up}

    scenario("concurrent: running deploy completes, parallel run refused, newer merge waits for its CI",
             concurrent,
             lambda r: r["outcome"].get("core_recreated_during_first") and r["outcome"]["first_exit"] == 0
             and r["outcome"]["second_refused_busy"] and r["observed"]["deployed"] == C2
             and r["outcome"]["follow_up"]["status"] == "ci_failed" and r["observed"]["checkout"] == C2,
             stage, results)

    # 9. Failed core health: automatic rollback, checkout restored; two attempts then held.
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", C1)
    stage.fault = "core-unhealthy"
    scenario("failed-health: unhealthy core is rolled back (attempt 1)", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "rolled_back" and unchanged(r), stage, results)
    stage.fault = "core-unhealthy"
    scenario("failed-health: second attempt rolled back too", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "rolled_back" and unchanged(r), stage, results)
    scenario("failed-health: the commit is then held", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "held" and unchanged(r), stage, results)

    # 10. Failed web checks: the web switches back, core and checkout are restored.
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", C1)
    stage.fault = "web-unreadable"
    scenario("failed-health: unreadable web release is switched back; core restored", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "web_rolled_back" and unchanged(r)
             and r["observed"]["served_index_matches_live"], stage, results)
    for leftover in (stage.webroot / ".releases").iterdir():
        leftover.chmod(0o755)

    # 11. Failed post-deploy verification: everything goes back.
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", C1)
    stage.fault = "verify-fails"
    scenario("rollback: failed post-deploy checks restore web, core and checkout", lambda: d().run_once(),
             lambda r: r["outcome"]["status"] == "verify_failed" and unchanged(r)
             and r["observed"]["served_index_matches_live"], stage, results)

    # 12. A run killed outright mid-deployment is restored, then retried safely.
    stage.reset()
    git(stage.remote, "update-ref", "refs/heads/main", C1)

    def killed() -> dict[str, Any]:
        proc = cycle_subprocess(stage.root)
        in_hold = wait_for(lambda: observe(stage)["core_revision"] == C1, 240)
        proc.send_signal(signal.SIGKILL)
        proc.wait(30)
        left = observe(stage)
        recovered = d().run_once()
        restored = observe(stage)
        retried = d().run_once()
        return {"status": retried["status"], "killed_during_hold": in_hold,
                "left_inflight": bool(left["inflight"]), "left_core_revision": left["core_revision"],
                "recovered": recovered, "restored_checkout": restored["checkout"],
                "restored_core_revision": restored["core_revision"]}

    scenario("killed: SIGKILL mid-hold is restored next cycle, then the retry deploys", killed,
             lambda r: r["outcome"]["killed_during_hold"] and r["outcome"]["left_inflight"]
             and r["outcome"]["recovered"]["status"] == "interrupted"
             and r["outcome"]["restored_checkout"] == BASE
             and r["outcome"]["restored_core_revision"] == base_state["core_revision"]
             and r["outcome"]["status"] == "deployed" and r["observed"]["deployed"] == C1, stage, results)

    # 13. The owner's explicit web rollback returns the previous release.
    def web_rollback() -> dict[str, Any]:
        doc = web.rollback(root=stage.webroot, state=stage.webstate, base=WEB_URL)
        return {"status": doc["status"], "to": doc["to"], "checks": len(doc.get("checks", []))}

    scenario("rollback: owner web rollback to the previous release", web_rollback,
             lambda r: r["outcome"]["status"] == "rolled_back"
             and r["observed"]["web_release"] == base_state["web_release"]
             and r["observed"]["served_index_matches_live"], stage, results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("all")
    sub.add_parser("cycle")
    sub.add_parser("cleanup")
    args = parser.parse_args()
    root = args.root.resolve()
    # Staging must never share production's lock or state.
    klukai_deploy_lock.LOCK = root / "deploy.lock"
    stage = Stage(root)
    if args.command == "cycle":
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
        try:
            doc = stage.deployer().run_once()
        except klukai_deploy_lock.Busy as error:
            print(f"refused: {error}")
            return 1
        print(json.dumps(doc, default=str))
        return 0
    if args.command == "cleanup":
        stage.cleanup()
        return 0
    if any(root.iterdir()) if root.exists() else False:
        print("staging root must be empty", file=sys.stderr)
        return 1
    root.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("GH_TOKEN"):
        print("set GH_TOKEN (read-only) for GitHub's CI records", file=sys.stderr)
        return 1
    try:
        results = run_all(stage)
    finally:
        stage.cleanup()
    evidence = root / "staging-evidence.json"
    evidence.write_text(json.dumps(results, indent=2, sort_keys=True, default=str) + "\n")
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} passed; evidence {evidence}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
