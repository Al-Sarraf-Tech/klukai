#!/usr/bin/env python3
"""Deploy-on-merge for Klukai: certified commits on main reach production by themselves.

    klukai-deploy-on-merge.py run-once          # the timer's entry: one decision, at most one deploy
    klukai-deploy-on-merge.py status
    klukai-deploy-on-merge.py pause | resume    # owner switch, effective on the next cycle
    klukai-deploy-on-merge.py retry <sha>       # allow one more attempt for a held commit
    klukai-deploy-on-merge.py baseline <sha>    # record what production runs (activation)
    klukai-deploy-on-merge.py install --revision <sha>   # pin this deployer's own code (owner)

The deployer pulls; nothing reaches into amarillo. GitHub holds no production
credential and no CI job runs with production rights. It runs from a pinned
copy of its own code that only the owner installs, so a merged change to the
deployer takes effect only when the owner installs it.

One cycle, under the shared deployment lock (klukai_deploy_lock):

1. Candidate: the current head of main (read-only GitHub token).
2. Order: the candidate must descend from the last deployed commit. An older
   commit or a rewritten history is never deployed.
3. Policy (optional, `require_approved_pr`): the candidate is the merge commit
   of a PR approved, on its final head, by an allowed reviewer.
4. Certification: a successful Klukai CI run for exactly this commit in which
   every full gate passed (klukai-fetch-ci-artifacts.green_run; "Required CI
   gates" runs only for full plans, so selective or skipped checks never
   count). Pending: wait. Failed: hold the commit and tell the owner.
5. Artifacts, with no production change: the core image (when core inputs
   changed) and the PWA are relayed from amarillo's own CI tree, verified
   against the run's own job log, and signed with the amarillo release key.
6. Re-check: if main moved meanwhile, stop; the next cycle takes the newer head.
   Migrations or compose changes need reviewed recovery plans
   (klukai-release.recovery_plans) or the commit is held for the owner.
7. Production: the checkout moves (detached) to the commit, then
   klukai-release.deploy (signature, fresh backup + isolated restore check,
   rollout, 180 s ready + 180 s hold + 5xx gate, automatic rollback), then the
   atomic web release (switch, byte checks through core, rollback), then the
   post-deploy checks (readiness and read-only e2e checks as the `claude` test
   user). Any failure puts back the previous web release, core image and
   checkout. A deployment in progress is never cancelled by a newer merge;
   SIGTERM restores instead of leaving production half-changed, and a run
   killed outright is restored at the start of the next cycle.
8. Record: state.json, deployments.jsonl, a receipt per attempt, an owner bus note.

Each commit gets at most `max_attempts` (2) production attempts; a commit that
failed after a production change is held until a newer commit arrives or the
owner runs `retry`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any

HERE = Path(__file__).resolve().parent
CODE = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CODE))
if __name__ == "__main__":
    # klukai-release and klukai_compose read this at import: the pinned code
    # must drive the production checkout, never its own copy.
    os.environ.setdefault("KLUKAI_PRODUCTION_CHECKOUT", str(Path.home() / "git/klukai"))
import klukai_deploy_lock  # noqa: E402
import klukai_web_release as web  # noqa: E402


def _module(name: str, filename: str) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


release = _module("klukai_release", "klukai-release.py")
relay = _module("klukai_fetch_ci_artifacts", "klukai-fetch-ci-artifacts.py")

CONFIG = Path.home() / ".config/klukai/deployer.json"
INSTALL = Path.home() / ".local/share/klukai/deployer"
TOKEN_NAME = "deployer-github-token"
SHA = re.compile(r"[0-9a-f]{40}\Z")
# Inputs that change what companion-core runs: the image, its compose wiring
# and the configuration bind-mounted into it.
CORE_INPUTS = ("docker/core", "docker-compose.yml", "config")
E2E_CHECKS = "health,auth-required,bad-token,authed-read"
# Outcomes of a production attempt. The same commit gets at most
# `max_attempts` of them; refusals and CI failures are re-evaluated each cycle.
HELD = {"rolled_back", "rollback_failed", "core_refused", "web_refused", "web_rolled_back",
        "verify_failed", "restore_failed", "interrupted"}
# Outcomes that may have left production degraded: the owner is told loudly.
LOUD = {"rollback_failed", "restore_failed"}
BUS = Path.home() / ".local/bin/bus"
# Refusals and relay errors are re-evaluated with growing gaps, capped.
BACKOFF_MAX = 1800
# SIGTERM (shutdown, `systemctl stop`) asks the deployer to stop. It never cuts
# a production step short: it finishes or restores the deployment in progress
# and only then exits, so child processes (docker compose, pg_dump, git) are
# never killed halfway. systemd's TimeoutStopSec bounds the wait; a run killed
# outright is restored by recover() on the next cycle.
STOP = {"requested": False}


def _request_stop(signum: int, frame: Any) -> None:
    STOP["requested"] = True


class Refused(RuntimeError):
    """This cycle stops before any production change."""


@dataclass
class Config:
    repo: str = "Al-Sarraf-Tech/klukai"
    branch: str = "main"
    remote_url: str = "https://github.com/Al-Sarraf-Tech/klukai.git"
    checkout: Path = field(default_factory=lambda: Path(
        os.environ.get("KLUKAI_PRODUCTION_CHECKOUT") or Path.home() / "git/klukai"))
    state: Path = Path.home() / ".local/state/klukai/release/deployer"
    releases: Path = Path.home() / ".local/state/klukai/ci-releases"
    signing_key: Path = Path.home() / ".config/klukai/release-signing/release_ed25519"
    require_approved_pr: bool = False
    reviewers: tuple[str, ...] = ("jalsarraf0",)
    max_attempts: int = 2
    hold: float = 180
    ready_timeout: float = 180
    e2e: bool = True
    notify: bool = True

    @classmethod
    def load(cls, path: Path = CONFIG) -> Config:
        doc = json.loads(path.read_text()) if path.is_file() else {}
        known = {f.name: f for f in fields(cls)}
        unknown = set(doc) - set(known)
        if unknown:
            raise ValueError(f"unknown deployer settings: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for name, value in doc.items():
            if name in {"checkout", "state", "releases", "signing_key"}:
                value = Path(value).expanduser()
            elif name == "reviewers":
                value = tuple(value)
            values[name] = value
        return cls(**values)


def now() -> str:
    return datetime.now(UTC).isoformat()


def checkout_detached(repo: Path, sha: str) -> None:
    """Move HEAD; refuse to overwrite local, untracked or ignored files."""
    git(repo, "checkout", "-q", "--no-overwrite-ignore", "--detach", sha)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300)
    if check and result.returncode:
        raise Refused(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def github_token() -> tuple[str, str]:
    """(token, source). The dedicated read-only credential when it exists.

    Until the owner creates it, the owner's existing GitHub CLI login is read
    at runtime (`gh auth token`), as manual releases already do: in memory
    only, never stored, never logged. It is broader than read-only, so status
    reports which source is in use."""
    import klukai_secrets

    try:
        return klukai_secrets.decrypt(TOKEN_NAME).decode().strip(), TOKEN_NAME
    except (klukai_secrets.SecretError, UnicodeError):
        pass
    try:
        found = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused(f"no deployer GitHub token: {type(error).__name__}") from error
    token = found.stdout.strip()
    if found.returncode or not token:
        raise Refused("no deployer GitHub token: create deployer-github-token (see runbook)")
    return token, "gh auth token (interim)"


class Deployer:
    """One deploy-on-merge cycle. Every external effect is a method, so the
    staging harness and the tests can substitute disposable targets."""

    core_container = release.SERVICE

    def __init__(self, cfg: Config, token: str | None = None) -> None:
        self.cfg = cfg
        self._token = token

    # ── external effects (substituted in staging and tests) ─────────────────

    token_source = "injected"

    @property
    def token(self) -> str:
        if self._token is None:
            self._token, self.token_source = github_token()
        return self._token

    def api(self, path: str) -> Any:
        return relay.github(path, self.token)

    def job_log(self, repo: str, job_id: int, token: str) -> str:
        return relay.job_log(repo, job_id, token)

    def guest(self) -> Any:
        return relay.LocalGuest()

    def main_head(self) -> str:
        sha = self.api(f"/repos/{self.cfg.repo}/commits/{self.cfg.branch}")["sha"]
        if not SHA.fullmatch(sha):
            raise Refused("GitHub returned an invalid head")
        return sha

    def running_core_revision(self) -> str:
        return subprocess.run(
            ["docker", "inspect", "-f",
             '{{index .Config.Labels "org.opencontainers.image.revision"}}', self.core_container],
            capture_output=True, text=True,
        ).stdout.strip()

    def image_id(self, tag: str) -> str:
        found = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", tag],
                               capture_output=True, text=True)
        return found.stdout.strip() if found.returncode == 0 else ""

    def core_target(self) -> Any:
        if release.PRODUCTION.resolve() != self.cfg.checkout.resolve():
            raise Refused(f"compose would run from {release.PRODUCTION}, not the production checkout "
                          f"{self.cfg.checkout}; set KLUKAI_PRODUCTION_CHECKOUT")
        return release.production_target()

    def deploy_core(self, revision: str, accept: bool, plans: dict[str, str] | None) -> dict[str, Any]:
        return release.deploy(revision, self.cfg.hold, self.cfg.ready_timeout,
                              accept_manual_rollback=accept, plans=plans, releases=self.cfg.releases)

    def deploy_web(self, revision: str) -> dict[str, Any]:
        return web.deploy(revision, releases=self.cfg.releases)

    def web_current(self) -> str | None:
        return web.current(web.ROOT)

    def web_switch_back(self, rid: str) -> dict[str, Any]:
        return web.rollback(rid, allow_live=True)

    def verify(self) -> dict[str, Any]:
        """Readiness, then the read-only e2e checks as the `claude` test user."""
        checks: dict[str, Any] = {"ready": release.http_ready()}
        if not checks["ready"]:
            return {"ok": False, **checks}
        if self.cfg.e2e:
            e2e = CODE / "scripts/e2e_live.py"
            result = subprocess.run(
                [sys.executable, str(e2e), "--user", "claude", "--only", E2E_CHECKS],
                capture_output=True, text=True, timeout=300, cwd=self.cfg.checkout,
            )
            checks["e2e"] = {"exit": result.returncode, "tail": result.stdout.strip().splitlines()[-3:]}
            if result.returncode:
                return {"ok": False, **checks}
        return {"ok": True, **checks}

    def notify(self, text: str) -> None:
        if not self.cfg.notify:
            return
        bus = str(BUS) if BUS.is_file() else "bus"
        try:
            subprocess.run([bus, "send", "owner", "--note", f"klukai deploy-on-merge: {text}"[:900]],
                           capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass  # the receipt and journal still record it

    # ── state ─────────────────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        path = self.cfg.state / "state.json"
        doc = json.loads(path.read_text()) if path.is_file() else {}
        doc.setdefault("candidates", {})
        doc.setdefault("notified", [])
        return doc

    def save(self, doc: dict[str, Any]) -> None:
        """Atomic and durable: `inflight` must survive a power loss."""
        self.cfg.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = self.cfg.state / "state.json.tmp"
        with staged.open("w") as out:
            out.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(staged, self.cfg.state / "state.json")
        web._fsync_dir(self.cfg.state)

    @property
    def pause_marker(self) -> Path:
        return self.cfg.state / "paused"

    def paused(self) -> str | None:
        """The owner's (or a loud failure's) pause, readable without the lock."""
        try:
            return self.pause_marker.read_text().strip() or "paused"
        except FileNotFoundError:
            return None

    def pause(self, reason: str) -> None:
        self.cfg.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pause_marker.write_text(f"{now()} {reason}\n")

    def history(self, doc: dict[str, Any]) -> Path:
        self.cfg.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.cfg.state / "deployments.jsonl").open("a") as out:
            out.write(json.dumps(doc, sort_keys=True) + "\n")
        receipts = self.cfg.state / "receipts"
        receipts.mkdir(mode=0o700, exist_ok=True)
        path = receipts / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{doc['revision'][:12]}.json"
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        return path

    def tell_once(self, st: dict[str, Any], key: str, text: str) -> None:
        if key not in st["notified"]:
            st["notified"] = (st["notified"] + [key])[-200:]
            self.notify(text)

    # ── decisions (no production change) ─────────────────────────────────────

    def fetch_commit(self, head: str) -> None:
        """Bring the candidate into the production checkout's object store,
        anonymously over HTTPS (no write credential), without moving HEAD."""
        git(self.cfg.checkout, "fetch", "-q", "--no-tags", "--no-write-fetch-head",
            self.cfg.remote_url, f"+refs/heads/{self.cfg.branch}:refs/klukai-deploy/{self.cfg.branch}")
        fetched = git(self.cfg.checkout, "rev-parse", f"refs/klukai-deploy/{self.cfg.branch}")
        if fetched != head:
            raise Refused(f"main moved while fetching ({head[:12]} → {fetched[:12]}); next cycle")

    def descends(self, older: str, newer: str) -> bool:
        return subprocess.run(["git", "-C", str(self.cfg.checkout), "merge-base", "--is-ancestor",
                               older, newer], capture_output=True).returncode == 0

    def approved_merge(self, head: str) -> dict[str, Any] | None:
        for pr in self.api(f"/repos/{self.cfg.repo}/commits/{head}/pulls"):
            if not (pr.get("merged_at") and pr.get("merge_commit_sha") == head
                    and pr.get("base", {}).get("ref") == self.cfg.branch):
                continue
            reviews = self.api(f"/repos/{self.cfg.repo}/pulls/{pr['number']}/reviews?per_page=100")
            latest: dict[str, dict[str, Any]] = {}
            for review in reviews:
                if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    latest[review["user"]["login"]] = review
            approvers = sorted(
                login for login, review in latest.items()
                if review["state"] == "APPROVED"
                and review.get("commit_id") == pr["head"]["sha"]
                and login in self.cfg.reviewers
                and login != pr.get("user", {}).get("login")
            )
            if approvers:
                return {"pr": pr["number"], "approved_by": approvers}
        return None

    def certification(self, head: str) -> dict[str, Any]:
        """certified (with run), pending, or failed for exactly this commit."""
        try:
            return {"state": "certified", "run": relay.green_run(self.cfg.repo, head, self.token, self.api_for_relay)}
        except ValueError:
            pass
        runs = self.api(f"/repos/{self.cfg.repo}/actions/workflows/ci.yml/runs?head_sha={head}&per_page=20")
        runs = [r for r in runs["workflow_runs"] if r["head_sha"] == head]
        if any(r["status"] != "completed" for r in runs) or not runs:
            return {"state": "pending"}
        return {"state": "failed", "runs": sorted(f"{r['id']}:{r['conclusion']}" for r in runs)}

    def api_for_relay(self, path: str, token: str) -> Any:
        return self.api(path)

    def core_needed(self, head: str) -> bool:
        running = self.running_core_revision()
        if not SHA.fullmatch(running) or not self.descends(running, head):
            return True
        return subprocess.run(["git", "-C", str(self.cfg.checkout), "diff", "--quiet", running, head,
                               "--", *CORE_INPUTS]).returncode != 0

    def relay_artifacts(self, head: str, run: str, core: bool, web_needed: bool = True) -> dict[str, Any]:
        guest = self.guest()
        out: dict[str, Any] = {}
        if not web_needed:
            core_only = {"web": "already relayed and signed"}
        else:
            core_only = {}
        if core:
            out["core"] = relay.fetch_image(
                guest, run, head, self.cfg.releases / head,
                manifest_sha256=relay.ci_manifest_digest(self.cfg.repo, run, self.token,
                                                         self.api_for_relay, self.job_log),
                key=self.cfg.signing_key)
        if core_only:
            return {**out, **core_only}
        out["web"] = relay.fetch_web(
            guest, run, head, self.cfg.releases / head / "web",
            pwa_sha256=relay.ci_pwa_digest(self.cfg.repo, run, self.token, self.api_for_relay, self.job_log),
            key=self.cfg.signing_key)
        return out

    def checkout_conflicts(self, head: str) -> list[str]:
        """Local changes `git checkout --detach <head>` would refuse to overwrite."""
        def names(*args: str) -> set[str]:
            return {n for n in git(self.cfg.checkout, *args, "-z").split("\0") if n}
        modified = names("diff", "--name-only", "HEAD") | names("diff", "--name-only", "--cached")
        changing = names("diff", "--name-only", "HEAD", head)
        added = names("diff", "--name-only", "--diff-filter=A", "HEAD", head)
        untracked = names("ls-files", "--others", "--exclude-standard")
        ignored = names("ls-files", "--others", "--ignored", "--exclude-standard")
        return sorted((modified & changing) | (added & (untracked | ignored)))

    def irreversible(self, head: str) -> tuple[bool, dict[str, str] | None]:
        safety = release.rollback_safety(head, self.cfg.checkout, self.core_container)
        if safety["safe"]:
            return False, None
        if not safety.get("changed"):
            raise Refused(f"rollback safety unknown: {safety.get('reason')}")
        found = release.recovery_plans(safety["changed"], head, self.cfg.checkout)
        if found["missing"]:
            raise Refused("needs a reviewed recovery plan for: " + ", ".join(found["missing"]))
        # A plan is reviewed only if the PR that carried it was approved.
        if self.approved_merge(head) is None:
            raise Refused("migration/compose changes deploy automatically only as the merge of an "
                          "approved PR; deploy it manually after review")
        return True, found["plans"]

    def relayed(self, head: str, run: str) -> dict[str, bool]:
        """Artifacts already relayed and signed for exactly this commit and run."""
        found = {}
        for kind, path in (("core", self.cfg.releases / head / "provenance.json"),
                           ("web", self.cfg.releases / head / "web" / "web-provenance.json")):
            try:
                doc = json.loads(path.read_text())
                found[kind] = doc.get("revision") == head and doc.get("ci_run") == run \
                    and path.with_name(path.name + ".sig").is_file()
            except (OSError, ValueError):
                found[kind] = False
        return found

    # ── one cycle ─────────────────────────────────────────────────────────────

    def run_once(self) -> dict[str, Any]:
        if self.paused():
            return {"status": "paused", "detail": self.paused()}
        with klukai_deploy_lock.held("deploy-on-merge"):
            if self.paused():
                return {"status": "paused", "detail": self.paused()}
            st = self.load()
            if st.get("inflight"):
                return self.recover(st)
            deployed = st.get("deployed", {}).get("revision")
            if not deployed:
                return {"status": "refused", "detail": "no baseline; run `baseline <sha>`"}
            head = self.main_head()
            if head == deployed:
                return {"status": "idle", "revision": head}
            record = st["candidates"].get(head, {})
            if record.get("outcome") in HELD and record.get("attempts", 0) >= record.get("allowed", 0):
                return {"status": "held", "revision": head, "outcome": record["outcome"]}
            if record.get("outcome") in {"refused", "error"} and \
                    datetime.now(UTC).timestamp() < record.get("next_at", 0):
                return {"status": "backoff", "revision": head, "outcome": record["outcome"]}
            try:
                return self.attempt(st, head, deployed)
            except Refused as error:
                outcome = "deferred" if "next cycle" in str(error) else "refused"
                key = f"{head}:refused:{str(error)[:60]}"
                text = f"{head[:12]} not deployed: {error}"
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                outcome = "error"
                key = f"{head}:error:{type(error).__name__}"
                text = f"{head[:12]} not deployed yet ({type(error).__name__}: {str(error)[:200]}); retrying"
            st = self.load()
            record = st["candidates"].get(head, {})
            misses = record.get("misses", 0) + 1 if record.get("outcome") == outcome else 1
            st["candidates"][head] = {**record, "outcome": outcome, "detail": text[:300], "at": now(),
                                      "misses": misses,
                                      "next_at": datetime.now(UTC).timestamp()
                                      + min(BACKOFF_MAX, 60 * 2 ** (misses - 1))}
            if outcome != "deferred":
                self.tell_once(st, key, text)
            self.save(st)
            return {"status": outcome, "revision": head, "detail": text}

    def attempt(self, st: dict[str, Any], head: str, deployed: str) -> dict[str, Any]:
        doc: dict[str, Any] = {"revision": head, "previous_revision": deployed, "started_at": now()}
        self.fetch_commit(head)
        if not self.descends(deployed, head):
            raise Refused(f"{head[:12]} does not descend from deployed {deployed[:12]}")
        if self.cfg.require_approved_pr:
            approval = self.approved_merge(head)
            if approval is None:
                raise Refused("not the merge of a PR approved by " + ", ".join(self.cfg.reviewers))
            doc["approval"] = approval
        cert = self.certification(head)
        if cert["state"] == "pending":
            return {"status": "waiting", "revision": head, "detail": "CI for this commit is not finished"}
        if cert["state"] == "failed":
            # Re-evaluated every cycle: a re-run that certifies it deploys it.
            st["candidates"][head] = {**st["candidates"].get(head, {}), "outcome": "ci_failed",
                                      "runs": cert["runs"], "at": now()}
            self.tell_once(st, f"{head}:ci_failed",
                           f"{head[:12]} not deployed: no full CI run of it passed ({', '.join(cert['runs'])})")
            self.save(st)
            return {"status": "ci_failed", "revision": head, "runs": cert["runs"]}
        doc["ci_run"] = cert["run"]
        core = self.core_needed(head)
        doc["core_needed"] = core
        # Cheap local checks first: a refusal must not copy and load artifacts.
        if git(self.cfg.checkout, "symbolic-ref", "-q", "HEAD", check=False):
            raise Refused("production checkout is on a branch; it must be detached (activation step)")
        conflicts = self.checkout_conflicts(head)
        if conflicts:
            raise Refused("production checkout has local changes the release would overwrite: "
                          + ", ".join(conflicts[:5]))
        accept, plans = self.irreversible(head) if core else (False, None)
        have = self.relayed(head, cert["run"])
        doc["artifacts"] = self.relay_artifacts(head, cert["run"], core and not have["core"],
                                                web_needed=not have["web"])
        if self.main_head() != head:
            raise Refused("main moved during preflight; next cycle")
        if STOP["requested"]:
            raise Refused("stop requested before production changes; next cycle")
        return self.produce(st, head, doc, core, accept, plans)

    def produce(self, st: dict[str, Any], head: str, doc: dict[str, Any], core: bool,
                accept: bool, plans: dict[str, str] | None) -> dict[str, Any]:
        record = st["candidates"].get(head, {})
        attempts = record.get("attempts", 0) + 1
        target = self.core_target()
        previous = {
            "checkout": git(self.cfg.checkout, "rev-parse", "HEAD"),
            "core_image_id": target.inspect("{{.Image}}"),
            "core_pin": target.read_pin(),
            "web_release": self.web_current(),
        }
        st["inflight"] = {"revision": head, "previous": previous, "started_at": now()}
        st["candidates"][head] = {**record, "attempts": attempts, "outcome": "deploying", "at": now(),
                                  "allowed": max(record.get("allowed", 0), self.cfg.max_attempts)}
        self.save(st)
        doc.update(attempt=attempts, previous=previous)
        changed: dict[str, Any] = {}
        interrupt: BaseException | None = None
        try:
            outcome = self.steps(head, doc, core, accept, plans, changed)
        except BaseException as error:  # noqa: BLE001 - Ctrl-C or a crash: restore, then report
            doc["error"] = f"{type(error).__name__}: {error}"[:300]
            outcome, interrupt = "interrupted", error
        # Outside the try: once a deployment is recorded, nothing may send it
        # down the restore path.
        result = self.finish(head, doc, outcome, changed)
        if interrupt is not None and not isinstance(interrupt, Exception):
            raise interrupt
        return result

    def steps(self, head: str, doc: dict[str, Any], core: bool, accept: bool,
              plans: dict[str, str] | None, changed: dict[str, Any]) -> str:
        """The production changes, in order; returns the outcome."""
        checkout_detached(self.cfg.checkout, head)
        changed["checkout"] = True
        if core:
            try:
                doc["core"] = self.deploy_core(head, accept, plans)
            except (release.ReleaseError, ValueError, OSError, subprocess.SubprocessError) as error:
                # klukai-release refuses only before its pin switch.
                doc["core"] = {"status": "refused", "error": str(error)[:300]}
                return "core_refused"
            status = doc["core"].get("rollout", {}).get("status")
            if status != "deployed":
                return status or "core_refused"
            changed["core"] = doc["core"]["rollout"]
        try:
            doc["web"] = self.deploy_web(head)
        except (web.WebError, ValueError, OSError) as error:
            # The web release refuses only before its switch.
            doc["web"] = {"status": "refused", "error": str(error)[:300]}
            return "web_refused"
        if doc["web"]["status"] == "rolled_back":
            return "web_rolled_back"
        if doc["web"]["status"] not in {"deployed", "unchanged"}:
            # Its own switch back failed its checks: try once more, loudly.
            changed["web"] = doc["web"].get("previous")
            return "rollback_failed"
        if doc["web"]["status"] == "deployed":
            changed["web"] = doc["web"]["previous"]
        doc["verify"] = self.verify()
        return "deployed" if doc["verify"]["ok"] else "verify_failed"

    def finish(self, head: str, doc: dict[str, Any], outcome: str,
               changed: dict[str, Any]) -> dict[str, Any]:
        if outcome != "deployed":
            # Ctrl-C must not cut the restore short (SIGTERM never does).
            previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                doc["restore"] = self.restore(doc["previous"], changed)
            finally:
                signal.signal(signal.SIGINT, previous_handler)
            if any(v.get("status", "").endswith("failed") for v in doc["restore"].values()):
                outcome = "restore_failed"
        doc.update(outcome=outcome, finished_at=now())
        st = self.load()
        st["inflight"] = None
        record = st["candidates"].get(head, {})
        st["candidates"][head] = {**record, "outcome": outcome, "at": now()}
        if outcome == "deployed":
            st["deployed"] = {"revision": head, "at": now(), "ci_run": doc.get("ci_run"),
                              "web_release": self.web_current(),
                              "core_image_id": self.core_target().inspect("{{.Image}}")}
            st["candidates"] = {k: v for k, v in st["candidates"].items() if k == head}
        elif outcome in LOUD:
            # Production may be degraded: no automatic attempt of anything
            # until the owner has looked and resumed.
            st["candidates"][head]["allowed"] = st["candidates"][head].get("attempts", 0)
            self.pause(f"{outcome} on {head}")
        self.save(st)
        doc["receipt"] = str(self.history(doc))
        if outcome == "deployed":
            note = f"{head[:12]} deployed (CI run {doc.get('ci_run')})"
        elif outcome in LOUD:
            note = (f"{head[:12]} {outcome.upper()}: production may need you now; deploy-on-merge "
                    f"paused itself (resume after checking). Receipt {doc['receipt']}")
        else:
            note = f"{head[:12]} {outcome}; production is back on {doc['previous']['checkout'][:12]}"
        self.notify(note)
        return {"status": outcome, "revision": head, "receipt": doc["receipt"]}

    def restore(self, previous: dict[str, Any], changed: dict[str, Any]) -> dict[str, Any]:
        """Put back what this attempt changed: web, then the checkout (so core
        restarts with the previous compose file and config), then core."""
        out: dict[str, Any] = {}
        if changed.get("web"):
            out["web"] = self._guard("web", lambda: self.web_switch_back(changed["web"]))
        if changed.get("checkout") and previous.get("checkout"):
            out["checkout"] = self._guard("checkout", lambda: self._checkout_back(previous["checkout"]))
        if "core" in changed:
            rollout = changed["core"]
            out["core"] = self._guard("core", lambda: release.restore(
                self.core_target(), rollout["rollback_tag"], rollout["previous_image_id"],
                ready_timeout=self.cfg.ready_timeout))
        return out

    def _checkout_back(self, sha: str) -> dict[str, str]:
        checkout_detached(self.cfg.checkout, sha)
        return {"status": "restored"}

    @staticmethod
    def _guard(name: str, step: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return step()
        except Exception as error:  # noqa: BLE001 - a failed restore is reported, never hidden
            return {"status": f"{name}_restore_failed", "detail": f"{type(error).__name__}: {error}"[:300]}

    def recover(self, st: dict[str, Any]) -> dict[str, Any]:
        """A previous run died mid-deployment (SIGKILL, power loss): restore its
        recorded previous state before anything else happens. If that cannot be
        completed, stay paused and loud, keeping `inflight` for the next try."""
        inflight = st["inflight"]
        previous, head = inflight["previous"], inflight["revision"]
        doc: dict[str, Any] = {"revision": head, "recovered_at": now(), "previous": previous,
                               "started_at": inflight.get("started_at")}
        out: dict[str, Any] = {}
        if previous.get("web_release") and self.web_current() != previous["web_release"]:
            out["web"] = self._guard("web", lambda: self.web_switch_back(previous["web_release"]))
        out["checkout"] = self._guard("checkout", lambda: (
            self._checkout_back(previous["checkout"])
            if git(self.cfg.checkout, "rev-parse", "HEAD") != previous["checkout"]
            else {"status": "unchanged"}))
        out["core"] = self._guard("core", lambda: self._core_back(previous))
        doc["restore"] = out
        failed = any(v.get("status", "").endswith("failed") for v in out.values())
        outcome = "restore_failed" if failed else "interrupted"
        doc.update(outcome=outcome, finished_at=now())
        record = st["candidates"].get(head, {})
        st["candidates"][head] = {**record, "outcome": outcome, "at": now()}
        if failed:
            self.pause(f"recovery of {head} incomplete")
            st["candidates"][head]["allowed"] = record.get("attempts", 0)
        else:
            st["inflight"] = None
        self.save(st)
        doc["receipt"] = str(self.history(doc))
        if failed:
            self.tell_once(st, f"{head}:recover:{doc['receipt']}",
                           f"{head[:12]} was interrupted and RECOVERY IS INCOMPLETE: deploy-on-merge paused; "
                           f"check {doc['receipt']} (a left-over .git/index.lock is the usual cause)")
            self.save(st)
        else:
            self.notify(f"{head[:12]} was interrupted mid-deployment; previous production restored")
        return {"status": outcome, "revision": head, "receipt": doc["receipt"]}

    def _core_back(self, previous: dict[str, Any]) -> dict[str, Any]:
        """The previous image running under the previous pin."""
        target = self.core_target()
        image, pin = previous.get("core_image_id"), previous.get("core_pin")
        if not image:
            return {"status": "unchanged"}
        if target.inspect("{{.Image}}") != image:
            tag = pin if pin and self.image_id(pin) == image \
                else f"{target.rollback_repo}:rollback-recover-{image[7:19]}"
            if tag != pin:
                release.run(["docker", "tag", image, tag])
            return release.restore(target, tag, image, ready_timeout=self.cfg.ready_timeout)
        if pin and target.read_pin() != pin:
            target.write_pin(pin)  # killed between the pin switch and the recreate
            return {"status": "pin_restored"}
        return {"status": "unchanged"}


# ── owner commands ────────────────────────────────────────────────────────────


def install(revision: str, cfg: Config, root: Path = INSTALL) -> dict[str, Any]:
    """Pin the deployer's code to a reviewed commit on main (owner action)."""
    if not SHA.fullmatch(revision):
        raise Refused("install needs a full commit SHA")
    git(cfg.checkout, "fetch", "-q", "--no-tags", "--no-write-fetch-head", cfg.remote_url,
        f"+refs/heads/{cfg.branch}:refs/klukai-deploy/{cfg.branch}")
    if subprocess.run(["git", "-C", str(cfg.checkout), "merge-base", "--is-ancestor", revision,
                       f"refs/klukai-deploy/{cfg.branch}"], capture_output=True).returncode:
        raise Refused(f"{revision[:12]} is not on {cfg.branch}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    final = root / revision
    if not final.is_dir():
        partial = Path(tempfile.mkdtemp(prefix=f".{revision[:12]}-", dir=root))
        archive = subprocess.run(
            ["git", "-C", str(cfg.checkout), "archive", "--format=tar", revision,
             "ops/amarillo/scripts", "ops/amarillo/release", "tools/ci", "scripts/e2e_live.py"],
            capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(partial)], input=archive.stdout, check=True)
        os.rename(partial, final)
    link = root / f".current-{os.getpid()}"
    os.symlink(revision, link)
    os.replace(link, root / "current")
    return {"command": "install", "status": "installed", "revision": revision, "path": str(final)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-once")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    r = sub.add_parser("retry")
    r.add_argument("revision")
    b = sub.add_parser("baseline")
    b.add_argument("revision")
    i = sub.add_parser("install")
    i.add_argument("--revision", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _request_stop)
    cfg = Config.load()
    deployer = Deployer(cfg)
    try:
        if args.command == "run-once":
            doc = deployer.run_once()
        elif args.command == "status":
            st = deployer.load()
            doc = {"paused": deployer.paused(), "deployed": st.get("deployed"),
                   "inflight": st.get("inflight"), "candidates": st["candidates"],
                   "config": {f.name: str(getattr(cfg, f.name)) for f in fields(cfg)},
                   "code": str(CODE), "web": web.status(),
                   "token_source": github_token()[1]}
        elif args.command == "install":
            doc = install(args.revision, cfg)
        elif args.command == "pause":
            # No lock: pausing must work while a deployment is running; the
            # next cycle (not the running one) honours it.
            deployer.pause("paused by the owner")
            doc = {"command": "pause", "status": "ok"}
        elif args.command == "resume":
            deployer.pause_marker.unlink(missing_ok=True)
            doc = {"command": "resume", "status": "ok"}
        else:
            with klukai_deploy_lock.held(f"deploy-on-merge {args.command}"):
                st = deployer.load()
                if args.command == "retry":
                    record = st["candidates"].get(args.revision)
                    if not record:
                        raise Refused(f"{args.revision[:12]} has no record")
                    record["allowed"] = record.get("attempts", 0) + 1
                else:
                    if not SHA.fullmatch(args.revision):
                        raise Refused("baseline needs a full commit SHA")
                    st["deployed"] = {"revision": args.revision, "at": now(), "baseline": True}
                deployer.save(st)
                doc = {"command": args.command, "status": "ok"}
    except (Refused, klukai_deploy_lock.Busy, release.ReleaseError, web.WebError, ValueError, OSError,
            subprocess.SubprocessError) as error:
        print(json.dumps({"command": args.command, "status": "refused", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2, sort_keys=True, default=str))
    return 0 if doc.get("status") not in {"rolled_back", "rollback_failed", "web_rolled_back",
                                          "verify_failed", "restore_failed", "interrupted"} else 2


if __name__ == "__main__":
    sys.exit(main())
