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
    document = json.loads(args[args.index("--prompt-json") + 1])
    assert document == {
        "type": "acp",
        "content": [
            {"type": "text", "text": system},
            {"type": "text", "text": "hello"},
        ],
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
