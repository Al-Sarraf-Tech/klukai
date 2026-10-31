"""Grok chat turns report grok-4.7 and low effort without printing secrets."""
import json
from pathlib import Path
import sys
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


def test_arguments_put_the_system_prompt_in_a_file(tmp_path):
    system = "S" * 200_000
    path = tmp_path / "system.txt"
    path.write_text(system)
    args = chat.arguments("s", "hello", path, "low")
    assert args[args.index("--model") + 1] == "grok-4.7"
    assert args[args.index("--effort") + 1] == "low"
    assert args[args.index("--system-prompt-file") + 1] == str(path)
    assert system not in args
    assert max(len(arg) for arg in args) < chat.MAX_ARG_STRLEN
    assert "disable_api_key_auth = true" in chat.CONFIG
    assert "api.x.ai" not in Path(chat.__file__).read_text()


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
