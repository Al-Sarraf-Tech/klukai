"""The Claude image checker: verdict parsing, hardening and fail-open behaviour, no cloud calls."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import time

from PIL import Image
import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/claude_image_check.py"


@pytest.fixture
def check(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("claude_image_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(width=64, height=64, color=(200, 210, 220)):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def references(tmp_path):
    pack = {}
    for name, color in (("klukai", (230, 235, 240)), ("commander", (40, 40, 50))):
        path = tmp_path / f"{name}.png"
        path.write_bytes(png(color=color))
        pack[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return pack


def value(**changes):
    data = {"request_id": "r", "user_id": "jalsarraf", "prompt": "Klukai in a garden",
            "width": 832, "height": 1216, "include_commander": False}
    data.update(changes)
    return data


def stream(result, *, is_error=False):
    events = [{"type": "system", "subtype": "init"},
              {"type": "result", "is_error": is_error, "result": result}]
    return "\n".join(json.dumps(e) for e in events).encode()


def test_pass_and_conservative_reject_rules(check):
    assert check.parse(stream('{"verdict": "pass", "issues": []}')) == {"verdict": "pass", "issues": []}
    major = '{"verdict": "reject", "issues": [{"severity": "major", "what": "extra arm"}]}'
    assert check.parse(stream(major))["verdict"] == "reject"
    # A reject without a named major defect, or a pass that lists one, never blocks.
    minor_only = '{"verdict": "reject", "issues": [{"severity": "minor", "what": "teardrop hidden"}]}'
    assert check.parse(stream(minor_only))["verdict"] == "pass"
    contradictory = '{"verdict": "pass", "issues": [{"severity": "major", "what": "extra arm"}]}'
    assert check.parse(stream(contradictory))["verdict"] == "pass"


def test_verdict_is_found_in_prose_and_issues_are_bounded(check):
    issues = [{"severity": "major", "what": "x " * 200}] * 20 + [{"severity": "odd", "what": "y"}]
    text = "Here is my verdict:\n" + json.dumps({"verdict": "reject", "issues": issues}) + "\nDone."
    parsed = check.parse(stream(text))
    assert parsed["verdict"] == "reject"
    assert len(parsed["issues"]) == check.MAX_ISSUES
    assert all(len(i["what"]) <= check.MAX_ISSUE_CHARS for i in parsed["issues"])


@pytest.mark.parametrize("raw", [
    stream("I can't help with that."),
    stream('{"verdict": "maybe"}'),
    stream('{"verdict": "pass", "issues": "none"}'),
    stream('{"verdict": "pass", "issues": []}', is_error=True),
    b"not json at all",
    b"",
])
def test_unusable_output_is_an_error(check, raw):
    with pytest.raises(ValueError):
        check.parse(raw)


def test_command_is_sonnet_high_on_the_subscription_with_no_tools(check):
    cmd = check.command("/x/claude")
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"forceLoginMethod": "claudeai", "apiKeyHelper": ""}
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert not any("dangerously" in part or "bypass" in part for part in cmd)
    assert "Never draw" in cmd[cmd.index("--system-prompt") + 1]


def test_environment_carries_no_keys(check):
    env = check.environment({"HOME": "/h", "USER": "u", "ANTHROPIC_API_KEY": "k", "GH_TOKEN": "t",
                             "HTTPS_PROXY": "p", "CLAUDE_CODE_OAUTH_TOKEN": "o"})
    assert env == {"HOME": "/h", "USER": "u", "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}


def test_message_sends_references_then_a_downscaled_candidate(check, references):
    line = json.loads(check.message(png(3000, 2000), value(), references))
    blocks = line["message"]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "text"]
    with Image.open(io.BytesIO(base64.b64decode(blocks[1]["source"]["data"]))) as candidate:
        assert max(candidate.size) == check.MAX_SIDE
    assert "Klukai in a garden" in blocks[-1]["text"] and "Klukai alone" in blocks[-1]["text"]
    with_commander = json.loads(check.message(png(), value(include_commander=True), references))
    assert [b["type"] for b in with_commander["message"]["content"]] == ["image", "image", "image", "text"]


def fake_claude(tmp_path, body):
    path = tmp_path / "fake-claude"
    path.write_text("#!/bin/sh\ncat > /dev/null\n" + body)
    path.chmod(0o700)
    return str(path)


def test_run_returns_the_parsed_verdict(check, references, tmp_path, monkeypatch):
    result = json.dumps({"type": "result", "is_error": False, "result": '{"verdict": "pass", "issues": []}'})
    monkeypatch.setattr(check, "claude_binary", lambda env=None: fake_claude(tmp_path, f"echo '{result}'\n"))
    verdict = check.run(png(), value(), references, timeout=20)
    assert verdict["verdict"] == "pass" and verdict["seconds"] >= 0


@pytest.mark.parametrize("body", ["exit 3\n", "echo nonsense\n"])
def test_run_failures_are_unchecked(check, references, tmp_path, monkeypatch, body):
    monkeypatch.setattr(check, "claude_binary", lambda env=None: fake_claude(tmp_path, body))
    assert check.run(png(), value(), references, timeout=20)["verdict"] == "unchecked"


def test_run_timeout_kills_the_check_and_is_unchecked(check, references, tmp_path, monkeypatch):
    monkeypatch.setattr(check, "claude_binary", lambda env=None: fake_claude(tmp_path, "sleep 30\n"))
    monkeypatch.setattr(check, "MIN_TIMEOUT", 0)
    started = time.monotonic()
    assert check.run(png(), value(), references, timeout=1)["verdict"] == "unchecked"
    assert time.monotonic() - started < 10


def test_too_little_time_skips_the_check(check, references, monkeypatch):
    monkeypatch.setattr(check.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    assert check.run(png(), value(), references, timeout=5)["verdict"] == "unchecked"


def test_fix_hint_names_only_major_issues(check):
    issues = [{"severity": "minor", "what": "teardrop hidden"},
              {"severity": "major", "what": "extra arm"}, {"severity": "major", "what": "text in corner"}]
    hint = check.fix_hint(issues)
    assert "extra arm" in hint and "text in corner" in hint and "teardrop" not in hint
    assert check.fix_hint([{"severity": "minor", "what": "small"}]) == ""
    assert len(check.fix_hint([{"severity": "major", "what": "z" * 500}] * 3)) <= 600
