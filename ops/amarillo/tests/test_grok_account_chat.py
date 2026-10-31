"""Grok chat turns report grok-4.7 and low effort without printing secrets."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import grok_account_chat as chat  # noqa: E402


def _stream(session, text="ready", effort="low"):
    return "\n".join(map(json.dumps, [
        {"type": "system", "subtype": "init", "session_id": session, "apiKeySource": "oauth",
         "mcp_servers": [], "skills": [], "model": "grok-4.7", "reasoning_effort": effort},
        {"type": "assistant", "session_id": session,
         "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": session,
         "result": text},
    ]))


def test_parse_turn_requires_oauth_grok_4_7_and_low(tmp_path):
    session = str(uuid.uuid4())
    turn = chat.parse_turn(_stream(session), session, tmp_path, effort="low")
    assert (turn.model, turn.effort, turn.effort_source, turn.text) == (
        "grok-4.7", "low", "stream", "ready",
    )
    import agy_art_runtime as agy
    bad = _stream(session, effort="high")
    try:
        chat.parse_turn(bad, session, tmp_path, effort="low")
    except agy.RuntimeError as exc:
        assert "low" in str(exc)
    else:
        raise AssertionError("high effort was accepted")


def test_arguments_put_the_system_text_in_prompt_json():
    system = "You are Klukai."
    args = chat.arguments("s", system, "hello", "low")
    assert args[args.index("--model") + 1] == "grok-4.7"
    assert args[args.index("--effort") + 1] == "low"
    assert args[args.index("--output-format") + 1] == "streaming-messages-json"
    assert "--include-partial-messages" in args
    assert "--system-prompt-file" not in args
    assert "--single" not in args
    assert args[args.index("--system-prompt-override") + 1] == system
    assert system not in args[args.index("--prompt-json") + 1]
    assert args[args.index("--tools") + 1] == "read_file"
    assert "run_terminal_cmd" in args[args.index("--disallowed-tools") + 1]
    document = json.loads(args[args.index("--prompt-json") + 1])
    assert document == {
        "type": "acp",
        "content": [{"type": "text", "text": "hello"}],
    }
    assert max(len(arg.encode()) for arg in args) < chat.PROMPT_JSON_LIMIT
    assert "disable_api_key_auth = true" in chat.CONFIG
    assert "api.x.ai" not in Path(chat.__file__).read_text()
    try:
        chat.arguments("s", "S" * chat.PROMPT_JSON_LIMIT, "hello", "low")
    except OSError as exc:
        assert exc.errno == 7 and "120KB" in exc.strerror
    else:
        raise AssertionError("an oversized prompt JSON was accepted")


def _fake_stdout(profile, work, args, timeout=170, *, cancel=None):
    session = args[args.index("--session-id") + 1]
    yield from _stream(session).splitlines()


def test_home_for_and_stream_can_run_twice(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_GROK_HOME_ROOT", str(tmp_path))
    monkeypatch.setattr(chat, "iter_stdout", _fake_stdout)
    first = chat.home_for("chat")
    second = chat.home_for("chat")
    assert first == second
    assert first[0].is_dir() and first[1].is_dir()
    assert (first[0] / ".grok" / "config.toml").is_file()
    for _ in range(2):
        events = list(chat.stream("persona", "hello", role="chat"))
        assert events[-1]["type"] == "done"
        assert events[-1]["text"] == "ready"
    assert chat.home_for("chat")[0] != chat.home_for("recall")[0]
    chat_cfg = (chat.home_for("chat")[0] / ".grok" / "config.toml").read_text()
    story_cfg = (chat.home_for("background")[0] / ".grok" / "config.toml").read_text()
    assert "max_completion_tokens = 2048" in chat_cfg
    assert "max_completion_tokens = 8192" in story_cfg


def test_chat_and_recall_homes_run_together_and_one_role_does_not(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_GROK_HOME_ROOT", str(tmp_path))
    chat_inside = threading.Event()
    release_chat = threading.Event()
    recall_inside = threading.Event()

    def fake_stdout(profile, work, args, timeout=170, *, cancel=None):
        kind = profile.parent.name
        if kind == "chat":
            chat_inside.set()
            assert release_chat.wait(2)
        else:
            recall_inside.set()
        yield from _fake_stdout(profile, work, args, timeout=timeout, cancel=cancel)

    monkeypatch.setattr(chat, "iter_stdout", fake_stdout)

    def consume(role, out):
        out.extend(chat.stream("persona", "hello", role=role))

    chat_out, recall_out = [], []
    chat_thread = threading.Thread(target=consume, args=("chat", chat_out))
    recall_thread = threading.Thread(target=consume, args=("recall", recall_out))
    chat_thread.start()
    assert chat_inside.wait(2)
    recall_thread.start()
    assert recall_inside.wait(2)
    release_chat.set()
    chat_thread.join(2)
    recall_thread.join(2)
    assert chat_out[-1]["type"] == "done"
    assert recall_out[-1]["type"] == "done"

    active = {"n": 0, "max": 0}
    guard = threading.Lock()

    def counting_stdout(profile, work, args, timeout=170, *, cancel=None):
        with guard:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        try:
            time.sleep(0.05)
            yield from _fake_stdout(profile, work, args, timeout=timeout, cancel=cancel)
        finally:
            with guard:
                active["n"] -= 1

    monkeypatch.setattr(chat, "iter_stdout", counting_stdout)
    threads = [
        threading.Thread(target=consume, args=("chat", [])) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert active["max"] == 1
    assert active["n"] == 0


def test_the_role_home_lock_blocks_another_process(monkeypatch, tmp_path):
    root = tmp_path / "homes"
    monkeypatch.setenv("KLUKAI_GROK_HOME_ROOT", str(root))
    scripts = str(Path(chat.__file__).resolve().parent)
    held = tmp_path / "held"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['KLUKAI_GROK_HOME_ROOT'] = sys.argv[2]\n"
            "import grok_account_chat as chat\n"
            "with chat._home_lock('chat'):\n"
            "    open(sys.argv[3], 'w').write('held')\n"
            "    time.sleep(30)\n",
            scripts,
            str(root),
            str(held),
        ]
    )
    try:
        deadline = time.monotonic() + 3
        while not held.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert held.exists()
        lock_path = root / "chat" / ".lock"
        blocked = os.open(lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(blocked, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        finally:
            os.close(blocked)
        assert acquired is False
    finally:
        child.kill()
        child.wait(timeout=3)
    free = os.open(root / "chat" / ".lock", os.O_RDWR)
    try:
        fcntl.flock(free, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(free, fcntl.LOCK_UN)
    finally:
        os.close(free)


def test_chat_background_and_image_efforts_are_configurable(monkeypatch):
    from grok_image_render import image_effort
    assert chat.chat_effort() == "low"
    assert chat.background_effort() == "low"
    assert image_effort() == "xhigh"
    monkeypatch.setenv("KLUKAI_GROK_CHAT_EFFORT", "minimal")
    monkeypatch.setenv("KLUKAI_GROK_BACKGROUND_EFFORT", "medium")
    monkeypatch.setenv("KLUKAI_GROK_IMAGE_EFFORT", "high")
    assert chat.chat_effort() == "minimal"
    assert chat.background_effort() == "medium"
    assert image_effort() == "high"
