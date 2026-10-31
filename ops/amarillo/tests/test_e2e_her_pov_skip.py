"""Her POV skips the image step when the e2e user is not allowlisted."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import e2e_live  # noqa: E402


def test_an_image_refusal_for_someone_off_the_list_is_a_skip():
    job = {"status": "failed", "error": "image_failed", "title": "The lamp"}
    assert e2e_live.her_pov_image_skipped("claude", job, {"jalsarraf"}) is True


def test_an_allowlisted_user_still_fails_a_broken_image():
    job = {"status": "failed", "error": "image_failed", "title": "The lamp"}
    assert e2e_live.her_pov_image_skipped("jalsarraf", job, {"jalsarraf"}) is False


def test_a_story_failure_is_not_an_image_skip():
    job = {"status": "failed", "error": "internal", "title": ""}
    assert e2e_live.her_pov_image_skipped("claude", job, {"jalsarraf"}) is False
    assert e2e_live.her_pov_image_skipped("claude", job, None) is False


def test_the_allowlist_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", " jalsarraf , alice ")
    assert e2e_live.agy_allowed_users() == {"jalsarraf", "alice"}


def test_the_allowlist_is_read_from_the_container_config(monkeypatch):
    monkeypatch.delenv("KLUKAI_AGY_ALLOWED_USERS", raising=False)
    seen = []

    def run(argv, **_kwargs):
        seen.append(argv)
        class Result:
            returncode = 0
            stdout = "PATH=/usr/bin\nKLUKAI_AGY_ALLOWED_USERS=jalsarraf,alice\n"
        return Result()

    monkeypatch.setattr(e2e_live.subprocess, "run", run)
    assert e2e_live.agy_allowed_users() == {"jalsarraf", "alice"}
    assert seen[0][1] == "inspect"
    assert "printenv" not in seen[0]


def test_a_skipped_check_is_not_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(e2e_live, "mint_token", lambda username: "token")
    monkeypatch.setattr(e2e_live, "revoke_token", lambda token: None)

    async def skipped(ctx):
        raise e2e_live.CheckSkipped("image skipped: claude is not on the list")

    monkeypatch.setitem(e2e_live.CHECKS, "her-pov", skipped)
    code = asyncio.run(e2e_live.run(["her-pov"], "claude", "ricky"))
    assert code == 0
    captured = capsys.readouterr().out
    assert "SKIP" in captured
    assert "FAIL" not in captured
