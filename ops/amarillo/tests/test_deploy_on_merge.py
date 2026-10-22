"""Deploy-on-merge decisions against real git and disposable production stand-ins."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys

import pytest

OLD_IMAGE = "sha256:" + "a" * 64

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import klukai_deploy_lock  # noqa: E402

SPEC = importlib.util.spec_from_file_location("klukai_deploy_on_merge", SCRIPTS / "klukai-deploy-on-merge.py")
assert SPEC is not None and SPEC.loader is not None
dom = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dom
SPEC.loader.exec_module(dom)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Remote:
    """GitHub's main branch: a bare repository plus a work clone to commit from."""

    def __init__(self, root: Path) -> None:
        self.bare, self.work = root / "remote.git", root / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.work)], check=True, capture_output=True)
        git(self.work, "config", "user.email", "t@t")
        git(self.work, "config", "user.name", "t")
        git(self.work, "checkout", "-q", "-b", "main")

    def commit(self, files: dict[str, str], push: bool = True) -> str:
        for name, body in files.items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(body)
        git(self.work, "add", ".")
        git(self.work, "commit", "-qm", "change")
        if push:
            git(self.work, "push", "-q", "-f", "origin", "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def reset(self, sha: str) -> None:
        git(self.work, "reset", "-q", "--hard", sha)
        git(self.work, "push", "-q", "-f", "origin", "HEAD:main")

    def head(self) -> str:
        return git(self.bare, "rev-parse", "main")


class Target:
    rollback_repo = "klukai-core"

    def __init__(self, world: World) -> None:
        self.world = world

    def inspect(self, template: str) -> str:
        return {"{{.Image}}": self.world.image, "{{.Id}}": self.world.container_id,
                "{{.Id}} {{.Image}} {{.State.Running}}":
                f"{self.world.container_id} {self.world.image} {str(self.world.running).lower()}"}.get(template, "")

    def ready(self) -> bool:
        return self.world.ready

    def read_pin(self) -> str:
        return self.world.pin


class World:
    """Production stand-ins and a record of everything the deployer did."""

    def __init__(self) -> None:
        self.image, self.pin, self.web = OLD_IMAGE, "klukai-core:old", "aaaaaaaaaaaa-000000000000"
        self.container_id = "a" * 64
        self.running = self.ready = True
        self.cert: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.notes: list[str] = []
        self.core_result = "deployed"
        self.web_result = "deployed"
        self.verify_ok = True
        self.irreversible: dict[str, tuple] = {}
        self.move_main_after_relay: str | None = None
        self.raise_in_web: BaseException | None = None
        self.raise_in_notify: BaseException | None = None


class Fake(dom.Deployer):
    def __init__(self, cfg, world: World, remote: Remote) -> None:
        super().__init__(cfg, token="test")
        self.w, self.remote = world, remote

    def main_head(self) -> str:
        return self.remote.head()

    def certification(self, head):
        return self.w.cert.get(head, {"state": "pending"})

    def running_core_revision(self) -> str:
        return self.w.core_rev

    def relay_artifacts(self, head, run, core, web_needed=True):
        self.w.calls.append(("relay", head, core))
        if self.w.move_main_after_relay:
            self.remote.commit({"late.txt": self.w.move_main_after_relay})
        return {"core": core}

    def irreversible(self, head):
        return self.w.irreversible.get(head, (False, None))

    def core_target(self):
        return Target(self.w)

    def deploy_core(self, revision, accept, plans):
        self.w.calls.append(("core", revision, accept, plans))
        if self.w.core_result == "deployed":
            self.w.image = "sha256:" + revision + "0" * 24
        return {"rollout": {"status": self.w.core_result, "rollback_tag": "klukai-core:rollback-x",
                            "previous_image_id": OLD_IMAGE}}

    def deploy_web(self, revision):
        self.w.calls.append(("web", revision))
        if self.w.raise_in_web is not None:
            raise self.w.raise_in_web
        previous = self.w.web
        if self.w.web_result == "deployed":
            self.w.web = f"{revision[:12]}-111111111111"
        return {"status": self.w.web_result, "previous": previous}

    def web_current(self):
        return self.w.web

    def web_switch_back(self, rid):
        self.w.calls.append(("web-back", rid))
        self.w.web = rid
        return {"status": "rolled_back"}

    def verify(self):
        self.w.calls.append(("verify",))
        return {"ok": self.w.verify_ok}

    def notify(self, text):
        self.w.notes.append(text)
        if self.w.raise_in_notify is not None:
            error, self.w.raise_in_notify = self.w.raise_in_notify, None
            raise error

    def image_id(self, tag):
        return ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = Remote(tmp_path)
    base = remote.commit({"README.md": "base\n", "docker/core/app.py": "v1\n", "flutter_app/main.dart": "v1\n"})
    checkout = tmp_path / "production"
    subprocess.run(["git", "clone", "-q", str(remote.bare), str(checkout)], check=True, capture_output=True)
    git(checkout, "checkout", "-q", "--detach", base)
    monkeypatch.setattr(klukai_deploy_lock, "LOCK", tmp_path / "deploy.lock")
    world = World()
    world.core_rev = base

    def restore(target, tag, previous_id, **kw):
        world.calls.append(("core-restore", tag, previous_id))
        world.image = previous_id
        world.running = world.ready = True
        return {"status": "restored"}

    def unexpected_run(*args, **kwargs):
        raise AssertionError("unmocked release command in offline fixture")

    monkeypatch.setattr(dom.release, "run", unexpected_run)
    monkeypatch.setattr(dom.release, "restore", restore)
    cfg = dom.Config(remote_url=str(remote.bare), checkout=checkout, state=tmp_path / "state",
                     releases=tmp_path / "releases", notify=True)
    deployer = Fake(cfg, world, remote)
    st = deployer.load()
    st["deployed"] = {"revision": base, "baseline": True}
    deployer.save(st)
    return {"remote": remote, "base": base, "checkout": checkout, "world": world, "deployer": deployer,
            "cfg": cfg, "tmp": tmp_path}


def certify(env, sha):
    env["world"].cert[sha] = {"state": "certified", "run": "42-1"}


def head_of(checkout: Path) -> str:
    return git(checkout, "rev-parse", "HEAD")


def state(env) -> dict:
    return json.loads((env["cfg"].state / "state.json").read_text())


def test_a_certified_merge_is_deployed_and_recorded(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    result = env["deployer"].run_once()
    assert result["status"] == "deployed", result
    assert head_of(env["checkout"]) == sha
    assert [c[0] for c in env["world"].calls] == ["relay", "core", "web", "verify"]
    assert state(env)["deployed"]["revision"] == sha
    assert state(env)["inflight"] is None
    history = (env["cfg"].state / "deployments.jsonl").read_text().splitlines()
    assert json.loads(history[-1])["outcome"] == "deployed"
    assert env["world"].notes and "deployed" in env["world"].notes[-1]
    assert env["deployer"].run_once()["status"] == "idle"


def test_a_web_only_merge_does_not_recreate_core(env):
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    assert env["deployer"].run_once()["status"] == "deployed"
    assert ("relay", sha, False) in env["world"].calls
    assert not any(c[0] == "core" for c in env["world"].calls)


def test_pending_ci_waits_without_touching_production(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    assert env["deployer"].run_once()["status"] == "waiting"
    assert head_of(env["checkout"]) == env["base"]
    assert env["world"].calls == []


def test_failed_ci_is_reported_once_and_a_later_green_rerun_deploys(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    env["world"].cert[sha] = {"state": "failed", "runs": ["9:failure"]}
    assert env["deployer"].run_once()["status"] == "ci_failed"
    assert env["deployer"].run_once()["status"] == "ci_failed"
    assert len([n for n in env["world"].notes if "no full CI" in n]) == 1
    assert head_of(env["checkout"]) == env["base"]
    certify(env, sha)
    assert env["deployer"].run_once()["status"] == "deployed"


def test_an_older_or_rewritten_main_is_never_deployed(env):
    first = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, first)
    assert env["deployer"].run_once()["status"] == "deployed"
    env["remote"].reset(env["base"])  # main rewound to an older commit
    certify(env, env["base"])
    result = env["deployer"].run_once()
    assert result["status"] == "refused" and "does not descend" in result["detail"]
    assert head_of(env["checkout"]) == first


def test_main_moving_during_preflight_stops_before_production(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].move_main_after_relay = "newer"
    result = env["deployer"].run_once()
    assert result["status"] == "deferred"
    assert head_of(env["checkout"]) == env["base"]
    assert not any(c[0] in {"core", "web"} for c in env["world"].calls)
    newer = env["remote"].head()
    env["world"].move_main_after_relay = None
    certify(env, newer)
    assert env["deployer"].run_once()["status"] == "deployed"
    assert head_of(env["checkout"]) == newer


def test_a_core_rollback_restores_the_checkout_and_retries_once_then_holds(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].core_result = "rolled_back"
    assert env["deployer"].run_once()["status"] == "rolled_back"
    assert head_of(env["checkout"]) == env["base"]
    assert env["deployer"].run_once()["status"] == "rolled_back"  # second and last attempt
    assert env["deployer"].run_once()["status"] == "held"
    assert len([c for c in env["world"].calls if c[0] == "core"]) == 2
    subprocess.run([sys.executable, str(SCRIPTS / "klukai-deploy-on-merge.py"), "--help"],
                   check=True, capture_output=True)


def test_a_failed_web_release_restores_core_and_checkout(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n", "flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    env["world"].web_result = "rolled_back"
    assert env["deployer"].run_once()["status"] == "web_rolled_back"
    assert ("core-restore", "klukai-core:rollback-x", OLD_IMAGE) in env["world"].calls
    assert env["world"].image == OLD_IMAGE
    assert head_of(env["checkout"]) == env["base"]


def test_failed_post_deploy_checks_restore_web_core_and_checkout(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].verify_ok = False
    before_web = env["world"].web
    assert env["deployer"].run_once()["status"] == "verify_failed"
    assert env["world"].web == before_web and env["world"].image == OLD_IMAGE
    assert head_of(env["checkout"]) == env["base"]
    assert "production is back on" in env["world"].notes[-1]


def test_an_interrupt_mid_release_restores_before_exiting(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].raise_in_web = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        env["deployer"].run_once()
    assert env["world"].image == OLD_IMAGE
    assert head_of(env["checkout"]) == env["base"]
    assert state(env)["inflight"] is None
    assert state(env)["candidates"][sha]["outcome"] == "interrupted"


def test_a_killed_run_is_restored_at_the_start_of_the_next_cycle(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    st = state(env)
    # What a SIGKILL right after the core switch leaves behind.
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": {
        "checkout": env["base"], "core_image_id": OLD_IMAGE, "core_pin": "klukai-core:old",
        "core_container_id": env["world"].container_id,
        "web_release": env["world"].web}}
    st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
    env["deployer"].save(st)
    git(env["checkout"], "fetch", "-q", str(env["remote"].bare), "main")
    git(env["checkout"], "checkout", "-q", "--detach", sha)
    env["world"].image = "sha256:" + "b" * 64
    tagged = []
    original = dom.release.run
    dom.release.run = lambda argv, **kw: tagged.append(argv) or ""
    try:
        assert env["deployer"].run_once()["status"] == "interrupted"
    finally:
        dom.release.run = original
    assert env["world"].image == OLD_IMAGE and head_of(env["checkout"]) == env["base"]
    assert tagged and tagged[0][:2] == ["docker", "tag"]
    assert tagged[0][3].startswith("klukai-core:rollback-recover-")
    assert env["deployer"].run_once()["status"] == "deployed"  # attempt 2 of 2


def test_a_migration_needs_a_plan_and_is_accepted_with_one(env):
    sha = env["remote"].commit({"docker/core/migrations/240_x.sql": "select 1;\n"})
    certify(env, sha)

    def without(head):
        raise dom.Refused("needs a reviewed recovery plan for: docker/core/migrations/240_x.sql")

    env["deployer"].irreversible = without
    result = env["deployer"].run_once()
    assert result["status"] == "refused" and "recovery plan" in result["detail"]
    assert head_of(env["checkout"]) == env["base"]
    del env["deployer"].irreversible
    st = state(env)
    st["candidates"][sha]["next_at"] = 0  # the backoff after the refusal has passed
    env["deployer"].save(st)
    env["world"].irreversible[sha] = (True, {"docker/core/migrations/240_x.sql": "docs/releases/recovery/240_x.md"})
    assert env["deployer"].run_once()["status"] == "deployed"
    core = next(c for c in env["world"].calls if c[0] == "core")
    assert core[2] is True and core[3] == {"docker/core/migrations/240_x.sql": "docs/releases/recovery/240_x.md"}


def test_local_changes_the_release_would_overwrite_stop_it_before_production(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    (env["checkout"] / "docker/core/app.py").write_text("hand edit\n")
    result = env["deployer"].run_once()
    assert result["status"] == "refused" and "docker/core/app.py" in result["detail"]
    assert env["world"].calls == []  # refused before any artifact was copied or loaded
    assert env["deployer"].run_once()["status"] == "backoff"


def test_a_checkout_on_a_branch_is_refused(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    git(env["checkout"], "checkout", "-q", "-B", "main", env["base"])
    result = env["deployer"].run_once()
    assert result["status"] == "refused" and "detached" in result["detail"]


def _hold(lock: str, ready, done) -> None:
    with klukai_deploy_lock.held("other deployment", Path(lock)):
        ready.set()
        done.wait(10)


def test_a_concurrent_deployment_is_refused_not_queued(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold, args=(str(env["tmp"] / "deploy.lock"), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(klukai_deploy_lock.Busy):
            env["deployer"].run_once()
    finally:
        done.set()
        holder.join(10)
    assert head_of(env["checkout"]) == env["base"]
    assert env["deployer"].run_once()["status"] == "deployed"


def test_pause_stops_everything(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["deployer"].pause("owner")
    assert env["deployer"].run_once()["status"] == "paused"
    assert env["world"].calls == []


def test_no_baseline_means_no_deployment(env):
    st = state(env)
    del st["deployed"]
    env["deployer"].save(st)
    assert env["deployer"].run_once()["status"] == "refused"


class Api:
    def __init__(self, prs, reviews):
        self.prs, self.reviews = prs, reviews

    def __call__(self, path):
        if path.endswith("/pulls"):
            return self.prs
        return self.reviews


def pr(head_sha="h" * 40, merge="m" * 40, author="cyberwolf"):
    return {"number": 31, "merged_at": "2026-10-22T00:00:00Z", "merge_commit_sha": merge,
            "base": {"ref": "main"}, "head": {"sha": head_sha}, "user": {"login": author}}


def review(login, state="APPROVED", commit="h" * 40):
    return {"user": {"login": login}, "state": state, "commit_id": commit}


@pytest.mark.parametrize("prs,reviews,approved", [
    ([pr()], [review("jalsarraf0")], True),
    ([pr()], [review("someone-else")], False),
    ([pr()], [review("jalsarraf0", commit="o" * 40)], False),  # approved an older head
    ([pr()], [review("jalsarraf0"), review("jalsarraf0", "CHANGES_REQUESTED")], False),
    ([pr(author="jalsarraf0")], [review("jalsarraf0")], False),  # self-approval
    ([pr(merge="x" * 40)], [review("jalsarraf0")], False),  # not this merge
    ([], [], False),  # a direct push
])
def test_only_a_pr_approved_on_its_final_head_counts(env, prs, reviews, approved):
    deployer = env["deployer"]
    deployer.api = Api(prs, reviews)
    assert (deployer.approved_merge("m" * 40) is not None) is approved


def test_the_approved_pr_policy_refuses_a_direct_push(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["deployer"].cfg.require_approved_pr = True
    env["deployer"].api = Api([], [])
    result = env["deployer"].run_once()
    assert result["status"] == "refused" and "approved" in result["detail"]
    assert env["world"].calls == []


def test_settings_reject_unknown_keys(tmp_path):
    path = tmp_path / "deployer.json"
    path.write_text(json.dumps({"require_approved_pr": True, "reviewers": ["jalsarraf0"]}))
    cfg = dom.Config.load(path)
    assert cfg.require_approved_pr is True and cfg.reviewers == ("jalsarraf0",)
    path.write_text(json.dumps({"auto_accept_migrations": True}))
    with pytest.raises(ValueError):
        dom.Config.load(path)


def test_a_failed_web_switch_back_is_retried_and_reported_loudly(env):
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    env["world"].web_result = "rollback_failed"
    env["world"].web = "bbbbbbbbbbbb-222222222222"  # stuck on a broken release
    result = env["deployer"].run_once()
    assert result["status"] == "rollback_failed"
    assert ("web-back", "bbbbbbbbbbbb-222222222222") in env["world"].calls
    assert "ROLLBACK_FAILED" in env["world"].notes[-1]



def test_a_loud_failure_pauses_the_deployer_until_the_owner_resumes(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].core_result = "rollback_failed"
    assert env["deployer"].run_once()["status"] == "rollback_failed"
    assert env["deployer"].paused()
    newer = env["remote"].commit({"README.md": "newer\n"})
    certify(env, newer)
    assert env["deployer"].run_once()["status"] == "paused"  # not even a newer commit
    env["deployer"].pause_marker.unlink()
    env["world"].core_result = "deployed"
    assert env["deployer"].run_once()["status"] == "deployed"
    assert state(env)["deployed"]["revision"] == newer


def test_pause_works_while_a_deployment_holds_the_lock(env):
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold, args=(str(env["tmp"] / "deploy.lock"), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        env["deployer"].pause("owner")
        assert env["deployer"].paused()
    finally:
        done.set()
        holder.join(10)
    assert env["deployer"].run_once()["status"] == "paused"


def test_a_recorded_deployment_is_never_rolled_back_by_a_late_interrupt(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].raise_in_notify = KeyboardInterrupt()  # e.g. Ctrl-C while the bus note is sent
    with pytest.raises(KeyboardInterrupt):
        env["deployer"].run_once()
    assert state(env)["deployed"]["revision"] == sha and state(env)["inflight"] is None
    assert head_of(env["checkout"]) == sha and env["world"].image == "sha256:" + sha + "0" * 24
    assert not any(c[0] == "core-restore" for c in env["world"].calls)


def test_an_incomplete_recovery_stays_paused_and_keeps_its_record(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    st = state(env)
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": {
        "checkout": env["base"], "core_image_id": OLD_IMAGE, "core_pin": "klukai-core:old",
        "core_container_id": env["world"].container_id,
        "web_release": env["world"].web}}
    env["deployer"].save(st)
    git(env["checkout"], "fetch", "-q", str(env["remote"].bare), "main")
    git(env["checkout"], "checkout", "-q", "--detach", sha)
    lock = env["checkout"] / ".git" / "index.lock"
    lock.write_text("")  # what a run killed inside `git checkout` leaves behind
    assert env["deployer"].run_once()["status"] == "restore_failed"
    assert state(env)["inflight"] is not None and env["deployer"].paused()
    assert "RECOVERY IS INCOMPLETE" in env["world"].notes[-1]
    lock.unlink()
    env["deployer"].pause_marker.unlink()
    assert env["deployer"].run_once()["status"] == "interrupted"
    assert head_of(env["checkout"]) == env["base"] and state(env)["inflight"] is None


def test_recovery_puts_back_a_pin_switched_just_before_the_kill(env, monkeypatch):
    st = state(env)
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": {
        "checkout": env["base"], "core_image_id": OLD_IMAGE, "core_pin": "klukai-core:old",
        "core_container_id": env["world"].container_id,
        "web_release": env["world"].web}}
    env["deployer"].save(st)
    env["world"].pin = "klukai-core:verified-new"  # pin switched, core not yet recreated
    written = []
    monkeypatch.setattr(Target, "write_pin", lambda self, image: written.append(image), raising=False)
    assert env["deployer"].run_once()["status"] == "interrupted"
    assert written == ["klukai-core:old"]


def test_a_commit_that_adds_an_ignored_local_file_is_refused(env):
    (env["remote"].work / ".gitignore").write_text("secret.env\n")
    git(env["remote"].work, "add", ".gitignore")
    git(env["remote"].work, "commit", "-qm", "ignore")
    git(env["remote"].work, "push", "-q", "origin", "HEAD:main")
    certify(env, env["remote"].head())
    assert env["deployer"].run_once()["status"] == "deployed"
    (env["checkout"] / "secret.env").write_text("production value\n")
    (env["remote"].work / "secret.env").write_text("repo value\n")
    git(env["remote"].work, "add", "-f", "secret.env")
    git(env["remote"].work, "commit", "-qm", "force-add")
    git(env["remote"].work, "push", "-q", "origin", "HEAD:main")
    certify(env, env["remote"].head())
    result = env["deployer"].run_once()
    assert result["status"] == "refused" and "secret.env" in result["detail"]
    assert (env["checkout"] / "secret.env").read_text() == "production value\n"


def test_a_stop_request_before_production_defers_without_changes(env, monkeypatch):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    monkeypatch.setitem(dom.STOP, "requested", True)
    assert env["deployer"].run_once()["status"] == "deferred"
    assert head_of(env["checkout"]) == env["base"]
    assert not any(c[0] in {"core", "web"} for c in env["world"].calls)


def test_already_relayed_artifacts_are_not_copied_again(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    for path in (env["cfg"].releases / sha / "provenance.json", env["cfg"].releases / sha / "web" / "web-provenance.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"revision": sha, "ci_run": "42-1"}))
        path.with_name(path.name + ".sig").write_text("sig")
    assert env["deployer"].relayed(sha, "42-1") == {"core": True, "web": True}
    assert env["deployer"].relayed(sha, "43-1") == {"core": False, "web": False}


def test_a_migration_plan_counts_only_through_an_approved_pr(env, monkeypatch):
    deployer = dom.Deployer(env["cfg"], token="t")
    monkeypatch.setattr(dom.release, "rollback_safety",
                        lambda *a: {"safe": False, "changed": ["docker/core/migrations/240_x.sql"]})
    monkeypatch.setattr(dom.release, "recovery_plans",
                        lambda *a: {"plans": {"docker/core/migrations/240_x.sql": "p"}, "missing": []})
    deployer.api = Api([], [])
    with pytest.raises(dom.Refused, match="approved PR"):
        deployer.irreversible("m" * 40)
    deployer.api = Api([pr()], [review("jalsarraf0")])
    assert deployer.irreversible("m" * 40) == (True, {"docker/core/migrations/240_x.sql": "p"})



def test_the_token_prefers_the_dedicated_credential_then_the_gh_login(monkeypatch):
    import klukai_secrets

    monkeypatch.setattr(klukai_secrets, "decrypt", lambda name: b"dedicated\n")
    assert dom.github_token() == ("dedicated", "deployer-github-token")

    def missing(name):
        raise klukai_secrets.SecretError("missing")

    monkeypatch.setattr(klukai_secrets, "decrypt", missing)
    monkeypatch.setattr(dom.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "gho_interim\n", ""))
    assert dom.github_token() == ("gho_interim", "gh auth token (interim)")
    monkeypatch.setattr(dom.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no"))
    with pytest.raises(dom.Refused, match="no deployer GitHub token"):
        dom.github_token()


def test_a_core_exception_after_switching_still_restores_the_snapshot(env, monkeypatch):
    """Core pruning can time out after rollout succeeded but before it returns."""
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    deployer, world = env["deployer"], env["world"]
    original = deployer.deploy_core

    def switched_then_failed(*args):
        original(*args)
        raise subprocess.TimeoutExpired(["docker", "image", "ls"], 600)

    monkeypatch.setattr(deployer, "deploy_core", switched_then_failed)
    monkeypatch.setattr(dom.release, "run", lambda *a, **kw: "")
    result = deployer.run_once()
    assert result["status"] != "deployed"
    assert world.image == OLD_IMAGE
    assert head_of(env["checkout"]) == env["base"]
    assert state(env)["inflight"] is None


def test_an_interrupt_after_the_web_switch_restores_the_snapshot(env, monkeypatch):
    """An interrupt while pruning happens after the new PWA became live."""
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    deployer, world = env["deployer"], env["world"]
    previous = world.web
    original = deployer.deploy_web

    def switched_then_interrupted(*args):
        original(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(deployer, "deploy_web", switched_then_interrupted)
    with pytest.raises(KeyboardInterrupt):
        deployer.run_once()
    assert world.web == previous
    assert head_of(env["checkout"]) == env["base"]
    assert state(env)["inflight"] is None


def test_a_failed_normal_restore_keeps_the_snapshot_for_recovery(env, monkeypatch):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].verify_ok = False
    restore = dom.release.restore
    monkeypatch.setattr(dom.release, "restore", lambda *a, **kw: {"status": "restore_failed"})
    assert env["deployer"].run_once()["status"] == "restore_failed"
    assert env["deployer"].paused()
    assert state(env)["inflight"] is not None
    assert state(env)["inflight"]["previous"]["checkout"] == env["base"]
    monkeypatch.setattr(dom.release, "restore", restore)
    monkeypatch.setattr(dom.release, "run", lambda *a, **kw: "")
    env["deployer"].pause_marker.unlink()
    env["world"].calls.clear()
    assert env["deployer"].run_once()["status"] == "interrupted"
    assert env["world"].image == OLD_IMAGE
    assert not any(call[0] == "relay" for call in env["world"].calls)
    assert state(env)["inflight"] is None


@pytest.mark.parametrize("step", ["core", "web"])
def test_a_step_refused_before_mutation_leaves_untouched_targets_alone(env, monkeypatch, step):
    name = "docker/core/app.py" if step == "core" else "flutter_app/main.dart"
    sha = env["remote"].commit({name: "v2\n"})
    certify(env, sha)
    world = env["world"]
    previous = world.web

    def refused(*args):
        raise OSError("preflight disk read failed")

    monkeypatch.setattr(env["deployer"], f"deploy_{step}", refused)
    result = env["deployer"].run_once()
    assert result["status"] == f"{step}_refused"
    assert world.image == OLD_IMAGE and world.web == previous
    assert not any(call[0] == "core-restore" for call in world.calls)
    # Equal-pointer web rollback is a read-only serving check, not a switch.
    assert (("web-back", previous) in world.calls) == (step == "web")
    assert head_of(env["checkout"]) == env["base"]
    assert state(env)["inflight"] is None


def test_a_core_rollback_recreates_using_the_previous_checkout(env, monkeypatch):
    """The nested rollback ran old image under the candidate compose/config."""
    sha = env["remote"].commit({"docker-compose.yml": "services: {core: {environment: {NEW: yes}}}\n"})
    certify(env, sha)
    deployer, world = env["deployer"], env["world"]
    world.core_result = "rolled_back"
    runtime_checkout = [env["base"]]
    original_deploy, original_restore = deployer.deploy_core, dom.release.restore

    def nested_rollback(*args):
        runtime_checkout[0] = head_of(env["checkout"])
        return original_deploy(*args)

    def restore_with_checkout(*args, **kwargs):
        runtime_checkout[0] = head_of(env["checkout"])
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(deployer, "deploy_core", nested_rollback)
    monkeypatch.setattr(dom.release, "restore", restore_with_checkout)
    assert deployer.run_once()["status"] == "rolled_back"
    assert head_of(env["checkout"]) == env["base"]
    assert runtime_checkout[0] == env["base"]


def test_snapshot_recovery_removes_a_pin_that_did_not_exist_before(env, monkeypatch):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    world = env["world"]
    world.pin = None

    def switch_pin_then_failed(*args):
        world.pin = "klukai-core:verified-new"
        raise OSError("stopped before recreate")

    monkeypatch.setattr(env["deployer"], "deploy_core", switch_pin_then_failed)
    monkeypatch.setattr(Target, "write_pin", lambda self, image: setattr(world, "pin", image), raising=False)
    result = env["deployer"].run_once()
    assert result["status"] == "core_refused"
    assert world.image == OLD_IMAGE
    assert world.pin is None


@pytest.mark.parametrize("restore_blocked", [False, True])
def test_an_interrupt_after_checkout_restores_or_retains_recovery(env, monkeypatch, restore_blocked):
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    checkout = dom.checkout_detached
    local_note = env["checkout"] / "operator-note.txt"
    local_note.write_text("keep this local note\n")

    def switch_then_interrupt(repo, revision):
        checkout(repo, revision)
        if revision == sha:
            if restore_blocked:
                (repo / ".git" / "index.lock").write_text("")
            raise KeyboardInterrupt

    monkeypatch.setattr(dom, "checkout_detached", switch_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        env["deployer"].run_once()
    assert local_note.read_text() == "keep this local note\n"
    assert not any(c[0] in {"core", "web"} for c in env["world"].calls)
    if restore_blocked:
        assert head_of(env["checkout"]) == sha
        assert state(env)["inflight"]["previous"]["checkout"] == env["base"]
        assert state(env)["candidates"][sha]["outcome"] == "restore_failed"
        assert env["deployer"].paused()
    else:
        assert head_of(env["checkout"]) == env["base"]
        assert state(env)["inflight"] is None


def test_a_checkout_refusal_before_mutation_preserves_user_files(env, monkeypatch):
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    checkout = dom.checkout_detached
    local_note = env["checkout"] / "operator-note.txt"
    local_note.write_text("keep this local note\n")

    def refuse_candidate(repo, revision):
        if revision == sha:
            raise OSError("checkout not started")
        checkout(repo, revision)

    monkeypatch.setattr(dom, "checkout_detached", refuse_candidate)
    assert env["deployer"].run_once()["status"] == "interrupted"
    assert head_of(env["checkout"]) == env["base"]
    assert local_note.read_text() == "keep this local note\n"
    assert not any(c[0] in {"core", "web"} for c in env["world"].calls)
    assert state(env)["inflight"] is None


@pytest.mark.parametrize("escape", ["exception", "kill", "legacy_kill", "restore_failure"])
def test_an_old_image_recreated_under_candidate_compose_is_restored(env, monkeypatch, escape):
    sha = env["remote"].commit({"docker-compose.yml": "services: {core: {environment: {NEW: yes}}}\n"})
    certify(env, sha)
    deployer, world = env["deployer"], env["world"]
    runtime_checkout = [env["base"]]
    original_restore = dom.release.restore

    def nested_rollback_then_failure(*args):
        runtime_checkout[0] = head_of(env["checkout"])
        world.container_id = "b" * 64
        world.image = OLD_IMAGE
        raise subprocess.TimeoutExpired(["fake-inner-rollback-receipt"], 600)

    def restore_under_checkout(*args, **kwargs):
        if escape == "restore_failure":
            return {"status": "restore_failed"}
        runtime_checkout[0] = head_of(env["checkout"])
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(deployer, "deploy_core", nested_rollback_then_failure)
    monkeypatch.setattr(dom.release, "restore", restore_under_checkout)
    monkeypatch.setattr(dom.release, "run", lambda *a, **kw: "")
    if escape in {"kill", "legacy_kill"}:
        st = state(env)
        previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                    "core_pin": world.pin, "web_release": world.web}
        if escape == "kill":
            previous["core_container_id"] = world.container_id
        st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
        st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
        deployer.save(st)
        deployer.fetch_commit(sha)
        git(env["checkout"], "checkout", "-q", "--detach", sha)
        runtime_checkout[0] = sha
        world.container_id = "b" * 64
    result = deployer.run_once()
    assert head_of(env["checkout"]) == env["base"]
    if escape == "restore_failure":
        assert result["status"] == "restore_failed"
        assert state(env)["inflight"] is not None and deployer.paused()
    else:
        assert result["status"] == ("interrupted" if "kill" in escape else "core_refused")
        assert runtime_checkout[0] == env["base"]
        assert state(env)["inflight"] is None


@pytest.mark.parametrize("observation", ["", "bad-container " + OLD_IMAGE + " true",
                                         "a" * 64 + " bad-image true",
                                         "a" * 64 + " " + OLD_IMAGE + " extra",
                                         "a" * 64 + " " + OLD_IMAGE + " false"])
def test_invalid_core_identity_refuses_before_mutation(env, monkeypatch, observation):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    inspect = Target.inspect
    monkeypatch.setattr(Target, "inspect", lambda self, template:
                        observation if template == "{{.Id}} {{.Image}} {{.State.Running}}" else inspect(self, template))
    assert env["deployer"].run_once()["status"] == "refused"
    assert head_of(env["checkout"]) == env["base"]
    assert not any(c[0] in {"core", "web"} for c in env["world"].calls)
    assert state(env).get("inflight") is None


@pytest.mark.parametrize("fault", ["stopped", "unready"])
def test_the_same_old_container_is_not_restored_until_running_and_ready(env, monkeypatch, fault):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    world = env["world"]

    def disturbed_then_failed(*args):
        if fault == "stopped":
            world.running = False
        else:
            world.ready = False
        raise OSError("compose interrupted before replacing the old container")

    monkeypatch.setattr(env["deployer"], "deploy_core", disturbed_then_failed)
    monkeypatch.setattr(dom.release, "run", lambda *a, **kw: "")
    monkeypatch.setattr(dom.time, "sleep", lambda s: None)  # the readiness re-checks
    assert env["deployer"].run_once()["status"] == "core_refused"
    assert any(c[0] == "core-restore" for c in world.calls)
    assert world.running and world.ready
    assert state(env)["inflight"] is None


def test_one_missed_readiness_probe_does_not_recreate_an_untouched_core(env, monkeypatch):
    """A pre-change core refusal re-checks the untouched old core; a single
    readiness blip must not recreate production for nothing."""
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    world = env["world"]
    probes = iter([False, True])

    def refused(*args):
        raise OSError("backup failed before any production change")

    class Blip(Target):
        def ready(self):
            return next(probes, True)

    monkeypatch.setattr(env["deployer"], "deploy_core", refused)
    monkeypatch.setattr(env["deployer"], "core_target", lambda: Blip(world))
    monkeypatch.setattr(dom.time, "sleep", lambda s: None)
    assert env["deployer"].run_once()["status"] == "core_refused"
    assert not any(c[0] == "core-restore" for c in world.calls)


def test_a_rollout_record_without_its_rollback_keys_falls_back_to_the_snapshot(env, monkeypatch):
    deployer = env["deployer"]
    calls = []
    monkeypatch.setattr(deployer, "_core_back", lambda previous: calls.append(previous) or {"status": "unchanged"})
    out = deployer.restore({"checkout": None, "core_image_id": OLD_IMAGE}, {"core": {"status": "rolled_back"}})
    assert out["core"] == {"status": "unchanged"} and calls


def test_resume_rechecks_a_failed_web_restore_already_pointing_at_previous(env, monkeypatch):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    st = state(env)
    previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                "core_container_id": world.container_id, "core_pin": world.pin,
                "web_release": world.web}
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
    st["candidates"][sha] = {"attempts": 1, "allowed": 1, "outcome": "restore_failed"}
    deployer.save(st)
    switch_back = deployer.web_switch_back

    def still_broken(rid):
        world.calls.append(("web-back", rid))
        return {"status": "rollback_failed", "detail": "old pointer but broken static serving"}

    monkeypatch.setattr(deployer, "web_switch_back", still_broken)
    for _ in range(2):
        assert deployer.run_once()["status"] == "restore_failed"
        assert state(env)["inflight"]["previous"] == previous
        assert deployer.paused()
        deployer.pause_marker.unlink()
    monkeypatch.setattr(deployer, "web_switch_back", switch_back)
    assert deployer.run_once()["status"] == "interrupted"
    assert state(env)["inflight"] is None
    assert not any(c[0] in {"relay", "core", "web"} for c in world.calls)
    assert sum(c[0] == "web-back" for c in world.calls) == 3


def test_recovery_recreates_a_confirmed_absent_container_from_the_saved_image(env, monkeypatch):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    st = state(env)
    previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                "core_container_id": world.container_id, "core_pin": world.pin,
                "web_release": world.web}
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
    st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
    deployer.save(st)
    # Compose removed the old container and the process died before replacement.
    monkeypatch.setattr(Target, "inspect", lambda self, template: "")
    monkeypatch.setattr(Target, "container_absent", lambda self: True, raising=False)
    monkeypatch.setattr(dom.release, "run", lambda *args, **kwargs: "")
    assert deployer.run_once()["status"] == "interrupted"
    assert any(call[0] == "core-restore" for call in world.calls)
    assert state(env)["inflight"] is None


def test_a_lost_failed_web_rollback_receipt_keeps_recovery_intent(env, monkeypatch):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    previous_web = world.web
    healthy_switch_back = deployer.web_switch_back

    def nested_rollback_loses_receipt(revision):
        world.web = f"{revision[:12]}-111111111111"
        # Nested rollback restores the pointer but fails its serving check;
        # Ctrl-C escapes before its rollback_failed receipt reaches the caller.
        world.web = previous_web
        raise KeyboardInterrupt("after failed web rollback, before receipt")

    def still_broken(rid):
        world.calls.append(("web-back", rid))
        return {"status": "rollback_failed", "detail": "pointer restored, serving still broken"}

    monkeypatch.setattr(deployer, "deploy_web", nested_rollback_loses_receipt)
    monkeypatch.setattr(deployer, "web_switch_back", still_broken)
    with pytest.raises(KeyboardInterrupt):
        deployer.run_once()
    assert state(env)["inflight"] is not None
    assert deployer.paused()
    assert any(call[0] == "web-back" for call in world.calls)
    assert state(env)["candidates"][sha]["outcome"] == "restore_failed"

    previous = state(env)["inflight"]["previous"]
    deployer.pause_marker.unlink()
    assert deployer.run_once()["status"] == "restore_failed"
    assert state(env)["inflight"]["previous"] == previous and deployer.paused()
    monkeypatch.setattr(deployer, "web_switch_back", healthy_switch_back)
    deployer.pause_marker.unlink()
    assert deployer.run_once()["status"] == "interrupted"
    assert state(env)["inflight"] is None


@pytest.mark.parametrize("observation, absence", [
    ("", False), ("", None), ("", {}), ("", {"absent": True}), ("", "true"),
    ("", OSError("fake daemon/access failure")), ("", TimeoutError("fake listing timeout")),
    ("malformed", True), ("a" * 64 + " bad-image true", True),
    ("a" * 64 + " " + OLD_IMAGE + " unknown", True), (None, True),
])
def test_uncertain_container_observation_keeps_the_recovery_journal(env, monkeypatch, observation, absence):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    st = state(env)
    previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                "core_container_id": world.container_id, "core_pin": world.pin,
                "web_release": world.web}
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
    st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
    deployer.save(st)
    calls = []

    def confirm_absence(self):
        calls.append("absence")
        if isinstance(absence, Exception):
            raise absence
        return absence

    monkeypatch.setattr(Target, "inspect", lambda self, template: observation)
    monkeypatch.setattr(Target, "container_absent", confirm_absence, raising=False)
    assert deployer.run_once()["status"] == "restore_failed"
    if observation != "":
        assert calls == []  # malformed identity is not evidence of absence
    assert state(env)["inflight"]["previous"] == previous and deployer.paused()
    assert not any(call[0] == "core-restore" for call in world.calls)


def test_a_failed_absent_container_restore_keeps_recovery_until_a_verified_retry(env, monkeypatch):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    st = state(env)
    previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                "core_container_id": world.container_id, "core_pin": world.pin,
                "web_release": world.web}
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
    st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
    deployer.save(st)
    monkeypatch.setattr(Target, "inspect", lambda self, template: "")
    monkeypatch.setattr(Target, "container_absent", lambda self: True, raising=False)
    monkeypatch.setattr(dom.release, "run", lambda *args, **kwargs: "")
    original_restore = dom.release.restore
    monkeypatch.setattr(dom.release, "restore", lambda *args, **kwargs: {"status": "restore_failed"})
    for _ in range(2):
        assert deployer.run_once()["status"] == "restore_failed"
        assert state(env)["inflight"]["previous"] == previous and deployer.paused()
        deployer.pause_marker.unlink()
    monkeypatch.setattr(dom.release, "restore", original_restore)
    assert deployer.run_once()["status"] == "interrupted"
    assert state(env)["inflight"] is None


def test_an_equal_web_pointer_is_revalidated_after_an_interrupted_noop(env, monkeypatch):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    previous_web = world.web

    def interrupted_noop(revision):
        raise KeyboardInterrupt("no pointer change, but caller needs serving evidence")

    monkeypatch.setattr(deployer, "deploy_web", interrupted_noop)
    with pytest.raises(KeyboardInterrupt):
        deployer.run_once()
    assert ("web-back", previous_web) in world.calls
    assert state(env)["inflight"] is None
    assert not deployer.paused()


def test_absence_confirmation_is_not_used_to_invent_an_initial_baseline(env, monkeypatch):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    calls = []
    monkeypatch.setattr(Target, "inspect", lambda self, template: "")
    monkeypatch.setattr(Target, "container_absent", lambda self: calls.append("absence") or True, raising=False)
    assert env["deployer"].run_once()["status"] == "refused"
    assert calls == [] and state(env).get("inflight") is None


def test_a_missing_web_snapshot_cannot_select_the_rollback_default(env, monkeypatch):
    """Exercise actual rollback's ``to or saved.previous`` semantics, all I/O fake."""
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    certify(env, sha)
    world.web = None
    stale = "dddddddddddd-999999999999"
    rollback_calls = []
    monkeypatch.setattr(dom.web, "_load", lambda state: {"previous": stale})
    monkeypatch.setattr(dom.web, "current", lambda root: world.web)
    monkeypatch.setattr(dom.web, "tree", lambda root, rid: env["tmp"])
    monkeypatch.setattr(dom.web, "smoke", lambda *args, **kwargs: ["fake serving passed"])
    monkeypatch.setattr(dom.web, "_record", lambda *args, **kwargs: None)

    def switch(root, rid):
        world.calls.append(("pointer-switch", rid))
        world.web = rid

    def rollback(rid):
        rollback_calls.append(rid)
        return dom.web.rollback(rid, root=env["tmp"] / "web", state=env["tmp"] / "web-state",
                                base="https://unused.invalid", allow_live=True)

    def refused(revision):
        raise dom.web.WebError("no live release; run adopt first")

    monkeypatch.setattr(dom.web, "switch", switch)
    monkeypatch.setattr(deployer, "web_switch_back", rollback)
    monkeypatch.setattr(deployer, "deploy_web", refused)
    result = deployer.run_once()
    assert world.web is None and not rollback_calls
    assert result["status"] == "restore_failed"
    previous = state(env)["inflight"]["previous"]
    assert previous["web_release"] is None and deployer.paused()
    for _ in range(2):
        deployer.pause_marker.unlink()
        assert deployer.run_once()["status"] == "restore_failed"
        assert state(env)["inflight"]["previous"] == previous and deployer.paused()
        assert world.web is None and not rollback_calls
    assert not any(call[0] == "pointer-switch" for call in world.calls)


@pytest.mark.parametrize("rid", [None, "", False, 0, {}, [], "not-a-release", "../outside",
                                 "g" * 12 + "-" + "0" * 12, "missing-key"])
def test_recovery_refuses_missing_or_invalid_explicit_web_identity(env, monkeypatch, rid):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    st = state(env)
    previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                "core_container_id": world.container_id, "core_pin": world.pin,
                "web_release": rid}
    if rid == "missing-key":
        del previous["web_release"]
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
    st["candidates"][sha] = {"attempts": 1, "allowed": 1, "outcome": "restore_failed"}
    deployer.save(st)
    calls = []
    monkeypatch.setattr(deployer, "web_switch_back", lambda target: calls.append(target) or {"status": "rolled_back"})
    for _ in range(2):
        assert deployer.run_once()["status"] == "restore_failed"
        assert state(env)["inflight"]["previous"] == previous and deployer.paused()
        assert calls == []
        deployer.pause_marker.unlink()


@pytest.mark.parametrize("rid", ["aaaaaaaaaaaa-000000000000", "adopted-20261022t075000-000000000000"])
def test_recovery_accepts_explicit_normal_and_adopted_web_identities(env, rid):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"flutter_app/main.dart": "v2\n"})
    world.web = rid
    st = state(env)
    previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                "core_container_id": world.container_id, "core_pin": world.pin,
                "web_release": rid}
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": previous}
    st["candidates"][sha] = {"attempts": 1, "allowed": 1, "outcome": "deploying"}
    deployer.save(st)
    assert deployer.run_once()["status"] == "interrupted"
    assert ("web-back", rid) in world.calls
    assert state(env)["inflight"] is None and not deployer.paused()


def core_dependent_web(env, monkeypatch, *, fault=None):
    """Use real deploy/rollback/smoke; only external effects are inert fakes."""
    import hashlib

    deployer, world = env["deployer"], env["world"]
    previous_web = world.web
    root = env["tmp"] / "web-root"
    directory = root / ".releases" / previous_web
    directory.mkdir(parents=True)
    main = b"same signed PWA across this core-only deployment"
    name = "main.dart." + hashlib.md5(main, usedforsecurity=False).hexdigest()[:8] + ".js"
    payloads = {"index.html": b"<html>unchanged signed PWA</html>", name: main}
    for filename, body in payloads.items():
        (directory / filename).write_bytes(body)
    inventory = dom.web.inventory(directory)
    events = []
    monkeypatch.setattr(dom.web, "verify_provenance", lambda *args: {"release": inventory, "ci_run": "42-1"})
    monkeypatch.setattr(dom.web, "_load", lambda path: {
        "releases": {previous_web: {"tree_sha256": inventory["sha256"]}},
        "previous": "dddddddddddd-999999999999"})
    monkeypatch.setattr(dom.web, "current", lambda path: world.web)
    monkeypatch.setattr(dom.web, "_record", lambda *args: events.append(("record",)))
    monkeypatch.setattr(dom.web.time, "sleep", lambda delay: None)

    def fetch(url):
        events.append(("fetch", world.image, head_of(env["checkout"]), world.web, url))
        if (world.image != OLD_IMAGE and fault != "healthy-candidate") or fault == "persistent-web":
            return 404, b"core ready but its static mount is broken", {}
        filename = "index.html" if url.endswith("/app/") else url.rsplit("/", 1)[1]
        return 200, payloads[filename], {"Cache-Control": dom.web.IMMUTABLE}

    def switch(path, rid):
        events.append(("switch", rid))
        world.web = rid

    def rollback(rid):
        events.append(("rollback", rid))
        result = dom.web.rollback(rid, root=root, state=env["tmp"] / "web-state",
                                  base="https://unused.invalid", fetch=fetch, allow_live=True)
        if fault == "lost-receipt":
            raise OSError("web rollback receipt lost after failed serving check")
        if fault == "wrong-receipt":
            result["to"] = "ffffffffffff-999999999999"
        elif fault == "unknown-receipt":
            result.pop("to", None)
        return result

    def checkout_back(sha):
        events.append(("checkout", sha))
        if fault == "checkout":
            raise OSError("previous checkout could not be restored")
        dom.checkout_detached(env["checkout"], sha)
        return {"status": "restored"}

    def restore_core(target, tag, image, **kwargs):
        events.append(("core", image))
        if fault == "core":
            return {"status": "restore_failed"}
        world.image, world.running, world.ready = image, True, True
        if fault == "wrong-target":
            world.web = "ffffffffffff-999999999999"
        elif fault == "unknown-target":
            world.web = None
        return {"status": "unknown" if fault == "unknown-core-status" else "restored"}

    monkeypatch.setattr(dom.web, "switch", switch)
    monkeypatch.setattr(deployer, "deploy_web", lambda revision: dom.web.deploy(
        revision, root=root, state=env["tmp"] / "web-state", releases=env["cfg"].releases,
        base="https://unused.invalid", fetch=fetch))
    monkeypatch.setattr(deployer, "web_switch_back", rollback)
    monkeypatch.setattr(deployer, "_checkout_back", checkout_back)
    monkeypatch.setattr(dom.release, "restore", restore_core)
    monkeypatch.setattr(deployer, "image_id", lambda tag: OLD_IMAGE)
    return events, previous_web


def run_core_dependent_restore(env, *, recovering):
    deployer, world = env["deployer"], env["world"]
    sha = env["remote"].commit({"docker/core/app.py": "core with broken static mount\n"})
    certify(env, sha)
    if recovering:
        st = state(env)
        previous = {"checkout": env["base"], "core_image_id": OLD_IMAGE,
                    "core_container_id": world.container_id, "core_pin": world.pin,
                    "web_release": world.web}
        st["inflight"] = {"revision": sha, "previous": previous}
        st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
        deployer.save(st)
        deployer.fetch_commit(sha)
        dom.checkout_detached(env["checkout"], sha)
        world.image, world.container_id = "sha256:" + "b" * 64, "b" * 64
    return deployer.run_once()


@pytest.mark.parametrize("recovering", [False, True])
def test_web_failure_before_core_restore_is_rechecked_after_core_is_restored(env, monkeypatch, recovering):
    # Without the final same-target check, a healthy restored service pauses
    # forever on web smoke evidence gathered under the candidate core.
    events, previous_web = core_dependent_web(env, monkeypatch)
    result = run_core_dependent_restore(env, recovering=recovering)
    assert result["status"] == ("interrupted" if recovering else "web_refused"), result
    assert state(env)["inflight"] is None and not env["deployer"].paused()
    assert env["world"].web == previous_web and env["world"].image == OLD_IMAGE
    assert head_of(env["checkout"]) == env["base"]
    mutations = [event[0] for event in events if event[0] in {"switch", "record", "checkout", "core"}]
    assert mutations == ["checkout", "core"]
    assert [event for event in events if event[0] == "rollback"] == [("rollback", previous_web)] * 2
    restored = next(i for i, event in enumerate(events) if event[0] == "core")
    assert any(event[0] == "fetch" and event[1] == OLD_IMAGE and event[2] == env["base"]
               for event in events[restored + 1:])


@pytest.mark.parametrize("recovering", [False, True])
@pytest.mark.parametrize("fault", ["persistent-web", "core", "checkout", "wrong-target", "unknown-target",
                                  "lost-receipt", "wrong-receipt", "unknown-receipt", "unknown-core-status"])
def test_post_core_web_recheck_never_hides_incomplete_restoration(env, monkeypatch, recovering, fault):
    events, previous_web = core_dependent_web(env, monkeypatch, fault=fault)
    result = run_core_dependent_restore(env, recovering=recovering)
    assert result["status"] == "restore_failed"
    previous = state(env)["inflight"]["previous"]
    assert previous["web_release"] == previous_web and env["deployer"].paused()
    assert env["deployer"].run_once()["status"] == "paused"
    assert not any(event[0] in {"switch", "record"} for event in events)
    assert sum(event[0] == "core" for event in events) == 1
    assert sum(event[0] == "checkout" for event in events) == 1
    assert sum(event[0] == "rollback" for event in events) == (2 if fault == "persistent-web" else 1)


@pytest.mark.parametrize("recovering", [False, True])
def test_a_successful_web_check_does_not_repeat_after_restoring_core(env, monkeypatch, recovering):
    events, previous_web = core_dependent_web(env, monkeypatch, fault="healthy-candidate")
    env["world"].verify_ok = False
    result = run_core_dependent_restore(env, recovering=recovering)
    assert result["status"] == ("interrupted" if recovering else "verify_failed")
    assert state(env)["inflight"] is None and not env["deployer"].paused()
    assert [event for event in events if event[0] == "rollback"] == [("rollback", previous_web)]
    assert not any(event[0] in {"switch", "record"} for event in events)
