"""Deploy-on-merge decisions against real git and disposable production stand-ins."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys

import pytest

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
        return self.world.image if template == "{{.Image}}" else ""

    def read_pin(self) -> str:
        return self.world.pin


class World:
    """Production stand-ins and a record of everything the deployer did."""

    def __init__(self) -> None:
        self.image, self.pin, self.web = "sha256:old", "klukai-core:old", "aaaaaaaaaaaa-000000000000"
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
            self.w.image = f"sha256:{revision[:8]}"
        return {"rollout": {"status": self.w.core_result, "rollback_tag": "klukai-core:rollback-x",
                            "previous_image_id": "sha256:old"}}

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
        return {"status": "restored"}

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
    assert ("core-restore", "klukai-core:rollback-x", "sha256:old") in env["world"].calls
    assert env["world"].image == "sha256:old"
    assert head_of(env["checkout"]) == env["base"]


def test_failed_post_deploy_checks_restore_web_core_and_checkout(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].verify_ok = False
    before_web = env["world"].web
    assert env["deployer"].run_once()["status"] == "verify_failed"
    assert env["world"].web == before_web and env["world"].image == "sha256:old"
    assert head_of(env["checkout"]) == env["base"]
    assert "production is back on" in env["world"].notes[-1]


def test_an_interrupt_mid_release_restores_before_exiting(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    env["world"].raise_in_web = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        env["deployer"].run_once()
    assert env["world"].image == "sha256:old"
    assert head_of(env["checkout"]) == env["base"]
    assert state(env)["inflight"] is None
    assert state(env)["candidates"][sha]["outcome"] == "interrupted"


def test_a_killed_run_is_restored_at_the_start_of_the_next_cycle(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    certify(env, sha)
    st = state(env)
    # What a SIGKILL right after the core switch leaves behind.
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": {
        "checkout": env["base"], "core_image_id": "sha256:old", "core_pin": "klukai-core:old",
        "web_release": env["world"].web}}
    st["candidates"][sha] = {"attempts": 1, "allowed": 2, "outcome": "deploying"}
    env["deployer"].save(st)
    git(env["checkout"], "fetch", "-q", str(env["remote"].bare), "main")
    git(env["checkout"], "checkout", "-q", "--detach", sha)
    env["world"].image = "sha256:new"
    tagged = []
    original = dom.release.run
    dom.release.run = lambda argv, **kw: tagged.append(argv) or ""
    try:
        assert env["deployer"].run_once()["status"] == "interrupted"
    finally:
        dom.release.run = original
    assert env["world"].image == "sha256:old" and head_of(env["checkout"]) == env["base"]
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
    assert head_of(env["checkout"]) == sha and env["world"].image == f"sha256:{sha[:8]}"
    assert not any(c[0] == "core-restore" for c in env["world"].calls)


def test_an_incomplete_recovery_stays_paused_and_keeps_its_record(env):
    sha = env["remote"].commit({"docker/core/app.py": "v2\n"})
    st = state(env)
    st["inflight"] = {"revision": sha, "started_at": "x", "previous": {
        "checkout": env["base"], "core_image_id": "sha256:old", "core_pin": "klukai-core:old",
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
        "checkout": env["base"], "core_image_id": "sha256:old", "core_pin": "klukai-core:old",
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
