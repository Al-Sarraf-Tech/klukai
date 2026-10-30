"""Grok chat turns report grok-4.7 and medium effort without printing secrets."""
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import grok_account_chat as chat  # noqa: E402


def _stream(session, text="ready", effort="medium"):
    return "\n".join(map(json.dumps, [
        {"type": "system", "subtype": "init", "session_id": session, "apiKeySource": "oauth",
         "mcp_servers": [], "skills": [], "model": "grok-4.7", "reasoning_effort": effort},
        {"type": "assistant", "session_id": session,
         "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": session,
         "result": text},
    ]))


def test_parse_turn_requires_oauth_grok_4_7_and_medium(tmp_path):
    session = str(uuid.uuid4())
    turn = chat.parse_turn(_stream(session), session, tmp_path)
    assert (turn.model, turn.effort, turn.effort_source, turn.text) == (
        "grok-4.7", "medium", "stream", "ready",
    )
    import agy_art_runtime as agy
    bad = _stream(session, effort="high")
    try:
        chat.parse_turn(bad, session, tmp_path)
    except agy.RuntimeError as exc:
        assert "medium" in str(exc)
    else:
        raise AssertionError("high effort was accepted")


def test_arguments_pin_the_model_and_disable_api_key_auth():
    args = chat.arguments("s", "system", "hello")
    assert args[args.index("--model") + 1] == "grok-4.7"
    assert args[args.index("--effort") + 1] == "medium"
    assert "disable_api_key_auth = true" in chat.CONFIG
    assert "api.x.ai" not in Path(chat.__file__).read_text()
