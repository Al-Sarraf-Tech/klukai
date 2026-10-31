#!/usr/bin/env python3
"""One Grok 4.7 turn through the official account-login CLI.

Chat and background text default to low effort. The caller passes the effort.
No xAI API key and no paid credits. The clean Grok home keeps
``disable_api_key_auth = true``. The system prompt is a file, not an argv
string.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import uuid

import agy_art_runtime as agy
from grok_image_render import CONFIG, MODEL, execute, iter_stdout

# Chat replies and background memory work. Override with the env names.
CHAT_EFFORT = "low"
BACKGROUND_EFFORT = "low"
_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
MAX_ARG_STRLEN = 131072


def _configured_effort(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip().lower()
    return value if value in _EFFORTS else default


def chat_effort() -> str:
    return _configured_effort("KLUKAI_GROK_CHAT_EFFORT", CHAT_EFFORT)


def background_effort() -> str:
    return _configured_effort("KLUKAI_GROK_BACKGROUND_EFFORT", BACKGROUND_EFFORT)


def effort_for(role: str) -> str:
    if role == "chat":
        return chat_effort()
    return background_effort()


class Turn:
    def __init__(self, text: str, model: str, effort: str, effort_source: str, tokens: int | None = None):
        self.text = text
        self.model = model
        self.effort = effort
        self.effort_source = effort_source
        self.tokens = tokens


def arguments(session: str, prompt: str, system_file: Path, effort: str) -> list[str]:
    result = [
        "--session-id", session, "--model", MODEL, "--effort", effort,
        "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
        "--max-turns", "1", "--output-format", "streaming-messages-json",
        "--include-partial-messages",
        "--verbatim", "--system-prompt-file", str(system_file), "--single", prompt,
    ]
    if any(len(arg) >= MAX_ARG_STRLEN for arg in result):
        raise OSError(7, "Argument list too long")
    return result


def _rows(raw: str) -> list[dict]:
    try:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except ValueError as exc:
        raise agy.RuntimeError("Invalid Grok chat stream") from exc
    if not rows:
        raise agy.RuntimeError("Grok chat stream was empty")
    return rows


def _summary_effort(profile: Path, session: str) -> str | None:
    sessions = profile / ".grok" / "sessions"
    if not sessions.is_dir():
        return None
    for path in sessions.rglob("summary.json"):
        if session not in path.parts:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        value = data.get("reasoning_effort") if isinstance(data, dict) else None
        return value if isinstance(value, str) else None
    return None


def text_deltas(row: dict) -> list[str]:
    """Partial text from one streaming-messages-json row."""
    if not isinstance(row, dict):
        return []
    if row.get("type") == "stream_event":
        event = row.get("event") if isinstance(row.get("event"), dict) else row
        delta = event.get("delta") if isinstance(event, dict) else None
        text = delta.get("text") if isinstance(delta, dict) else None
        return [text] if isinstance(text, str) and text else []
    if row.get("type") != "assistant":
        return []
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    found = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            if block["text"]:
                found.append(block["text"])
    return found


def _novel(piece: str, emitted: str, *, assistant: bool) -> str:
    if assistant and emitted and (piece == emitted or piece.startswith(emitted)):
        return piece[len(emitted):]
    return piece


def output_tokens(raw: str) -> int | None:
    for row in _rows(raw) if raw.strip() else []:
        usage = row.get("usage") if isinstance(row, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
            return usage["output_tokens"]
    return None


def parse_turn(raw: str, session: str, profile: Path, *, effort: str | None = None) -> Turn:
    """Require OAuth, grok-4.7, and the requested effort when the CLI reports it."""
    expected = chat_effort() if effort is None else effort
    rows = _rows(raw)
    init = [row for row in rows if row.get("type") == "system" and row.get("subtype") == "init"]
    ends = [row for row in rows if row.get("type") == "result"]
    if len(init) != 1 or len(ends) != 1:
        raise agy.RuntimeError("Grok chat event counts were invalid")
    if init[0].get("apiKeySource") != "oauth":
        raise agy.RuntimeError("Grok chat authentication was not OAuth")
    if init[0].get("model") != MODEL:
        raise agy.RuntimeError("Grok chat model was not grok-4.7")
    if init[0].get("mcp_servers") not in ([], None):
        raise agy.RuntimeError("Grok chat MCP inventory was not empty")
    echoed = init[0].get("reasoning_effort", init[0].get("effort"))
    source = "stream"
    if not isinstance(echoed, str):
        echoed = _summary_effort(profile, session)
        source = "summary"
    if echoed is None:
        echoed, source = expected, "flag"
    if echoed != expected:
        raise agy.RuntimeError("Grok chat effort was not " + expected)
    end = ends[0]
    if end.get("is_error") or end.get("session_id") not in (None, session):
        raise agy.RuntimeError("Grok chat did not finish in this session")
    text = end.get("result") if isinstance(end.get("result"), str) else ""
    if not text.strip():
        parts = []
        for row in rows:
            message = row.get("message")
            if row.get("type") != "assistant" or not isinstance(message, dict):
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        text = "".join(parts)
    if not text.strip():
        raise agy.RuntimeError("Grok chat returned no text")
    return Turn(text.strip(), MODEL, echoed, source, output_tokens(raw))


def _prepare(system: str, prompt: str, effort: str):
    if not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
        raise agy.RuntimeError("Invalid Grok chat request")
    if effort not in _EFFORTS:
        raise agy.RuntimeError("Invalid Grok chat effort")
    session = str(uuid.uuid4())
    root = Path(tempfile.mkdtemp(prefix="klukai-grok-chat-"))
    profile, work = root / "profile", root / "work"
    profile.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    config = profile / ".grok"
    config.mkdir()
    (config / "config.toml").write_text(CONFIG)
    system_file = work / "system.txt"
    system_file.write_text(system)
    system_file.chmod(0o600)
    return session, profile, work, arguments(session, prompt, system_file, effort)


def stream(system: str, prompt: str, *, timeout: float = 90, cancel=None, effort: str | None = None):
    """Yield delta events, then one done event, as the CLI prints them."""
    if cancel is not None and cancel.is_set():
        raise agy.RuntimeError("Grok chat was cancelled")
    chosen = chat_effort() if effort is None else effort
    session, profile, work, args = _prepare(system, prompt, chosen)
    raw_lines: list[str] = []
    emitted = ""
    try:
        for line in iter_stdout(profile, work, args, timeout=timeout, cancel=cancel):
            raw_lines.append(line)
            try:
                row = json.loads(line)
            except ValueError:
                continue
            assistant = row.get("type") == "assistant"
            for piece in text_deltas(row):
                extra = _novel(piece, emitted, assistant=assistant)
                if not extra:
                    continue
                emitted += extra
                yield {"type": "delta", "text": extra}
        turn = parse_turn("\n".join(raw_lines), session, profile, effort=chosen)
    finally:
        import shutil
        shutil.rmtree(profile.parent, ignore_errors=True)
    yield {
        "type": "done", "text": turn.text, "model": turn.model, "effort": turn.effort,
        "effort_source": turn.effort_source, "tokens": turn.tokens,
    }


def complete(system: str, prompt: str, *, timeout: float = 90, cancel=None, effort: str | None = None) -> Turn:
    if cancel is not None and cancel.is_set():
        raise agy.RuntimeError("Grok chat was cancelled")
    chosen = chat_effort() if effort is None else effort
    session, profile, work, args = _prepare(system, prompt, chosen)
    try:
        raw = execute(profile, work, args, timeout=timeout, cancel=cancel)
        return parse_turn(raw, session, profile, effort=chosen)
    finally:
        import shutil
        shutil.rmtree(profile.parent, ignore_errors=True)
