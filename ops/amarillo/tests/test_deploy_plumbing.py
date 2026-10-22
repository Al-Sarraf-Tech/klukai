"""Shared plumbing: the deploy lock, the web-root override, recovery plans, restore."""

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
import klukai_compose  # noqa: E402
import klukai_deploy_lock  # noqa: E402

SPEC = importlib.util.spec_from_file_location("klukai_release_plumbing", SCRIPTS / "klukai-release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def _hold(path: str, ready, done) -> None:
    with klukai_deploy_lock.held("holder", Path(path)):
        ready.set()
        done.wait(10)


def test_a_second_deployment_is_refused_while_one_holds_the_lock(tmp_path):
    lock = tmp_path / "deploy.lock"
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold, args=(str(lock), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(klukai_deploy_lock.Busy, match="holder"):
            with klukai_deploy_lock.held("second", lock):
                pass
    finally:
        done.set()
        holder.join(10)
    with klukai_deploy_lock.held("after", lock):  # released when the holder exits
        pass


def test_a_killed_holder_releases_the_lock(tmp_path):
    lock = tmp_path / "deploy.lock"
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold, args=(str(lock), ready, done))
    holder.start()
    assert ready.wait(10)
    holder.kill()
    holder.join(10)
    with klukai_deploy_lock.held("after kill", lock):
        pass


def test_compose_applies_the_web_root_before_the_image_pin(tmp_path):
    paths = {"compose-env": tmp_path / "env", "core-override": tmp_path / "override.json"}
    pin, web_root = tmp_path / "pin.json", tmp_path / "web-root.json"
    assert "-f" in klukai_compose.argv(paths, ["ps"], pin, web_root)
    assert str(web_root) not in klukai_compose.argv(paths, ["ps"], pin, web_root)
    pin.write_text("{}")
    web_root.write_text("{}")
    args = klukai_compose.argv(paths, ["up"], pin, web_root)
    assert args.index(str(web_root)) < args.index(str(pin))
    assert args[args.index("-f") + 1].endswith("docker-compose.yml")


def test_the_production_checkout_comes_from_the_environment(tmp_path):
    code = (
        "import sys; sys.path.insert(0, %r); import klukai_compose; print(klukai_compose.PRODUCTION)"
        % str(SCRIPTS)
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={"KLUKAI_PRODUCTION_CHECKOUT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == str(tmp_path)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


PLAN = "# Recovery\n## Data preserved\nall rows\n## Rollback\nrestore dump\n## Verification\ncounts\n"


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def commit(repo: Path, files: dict[str, str]) -> str:
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "change")
    return git(repo, "rev-parse", "HEAD")


def test_a_migration_with_a_reviewed_plan_is_covered(repo):
    sha = commit(repo, {"docker/core/migrations/240_x.sql": "select 1;\n",
                        "docs/releases/recovery/240_x.md": PLAN})
    found = release.recovery_plans(["docker/core/migrations/240_x.sql"], sha, repo)
    assert found == {"plans": {"docker/core/migrations/240_x.sql": "docs/releases/recovery/240_x.md"},
                     "missing": []}


@pytest.mark.parametrize("plan", [None, "# Recovery\nlooks fine\n"])
def test_a_migration_without_a_complete_plan_is_not_covered(repo, plan):
    files = {"docker/core/migrations/240_x.sql": "select 1;\n"}
    if plan is not None:
        files["docs/releases/recovery/240_x.md"] = plan
    sha = commit(repo, files)
    assert release.recovery_plans(["docker/core/migrations/240_x.sql"], sha, repo)["missing"] == [
        "docker/core/migrations/240_x.sql"]


def test_a_compose_change_needs_a_plan_named_after_its_blob(repo):
    blob = subprocess.run(["git", "hash-object", "--stdin"], input="services: {a: {}}\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    stale = commit(repo, {"docker-compose.yml": "services: {a: {}}\n",
                          "docs/releases/recovery/compose-000000000000.md": PLAN})
    assert release.recovery_plans(["docker-compose.yml"], stale, repo)["missing"] == ["docker-compose.yml"]
    covered = commit(repo, {f"docs/releases/recovery/compose-{blob[:12]}.md": PLAN})
    assert release.recovery_plans(["docker-compose.yml"], covered, repo)["plans"] == {
        "docker-compose.yml": f"docs/releases/recovery/compose-{blob[:12]}.md"}


class Target(release.Target):
    def __init__(self, pin: Path, healthy: bool) -> None:
        super().__init__(compose=lambda args: None, container="core", pin=pin, ready=lambda: healthy)
        self.healthy = healthy

    def inspect(self, template: str) -> str:
        if template == "{{.Image}}":
            return "sha256:old"
        if template == "{{.State.Health.Status}}":
            return "healthy" if self.healthy else "unhealthy"
        return "0"


@pytest.mark.parametrize("healthy,status", [(True, "restored"), (False, "restore_failed")])
def test_restore_puts_the_previous_image_back_and_says_whether_it_held(tmp_path, monkeypatch, healthy, status):
    monkeypatch.setattr(release, "watch", lambda t, i, **k: (t.healthy, "x"))
    target = Target(tmp_path / "pin.json", healthy)
    doc = release.restore(target, "klukai-core:rollback-1", "sha256:old")
    assert doc["status"] == status
    assert json.loads((tmp_path / "pin.json").read_text())["services"]["companion-core"]["image"] == \
        "klukai-core:rollback-1"


def test_the_deployer_token_is_a_known_but_optional_credential():
    import klukai_secrets

    assert "deployer-github-token" in klukai_secrets.NAMES
    assert "deployer-github-token" in klukai_secrets.OPTIONAL_NAMES
    assert "deployer-github-token" not in klukai_secrets.COMPOSE_NAMES


def test_production_target_refuses_a_checkout_without_compose(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "PRODUCTION", tmp_path)
    with pytest.raises(release.ReleaseError, match="KLUKAI_PRODUCTION_CHECKOUT"):
        release.production_target()


def test_the_web_release_cli_defaults_to_the_production_checkout(tmp_path):
    code = ("import runpy, sys, os; sys.argv = ['x', 'status'];\n"
            "import builtins\n"
            "try:\n    runpy.run_path(%r, run_name='__main__')\nexcept SystemExit:\n    pass\n"
            "print('CHECKOUT=' + os.environ.get('KLUKAI_PRODUCTION_CHECKOUT', ''))") % str(SCRIPTS / "klukai_web_release.py")
    out = subprocess.run([sys.executable, "-c", code], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True)
    assert f"CHECKOUT={tmp_path}/git/klukai" in out.stdout, out.stdout + out.stderr
