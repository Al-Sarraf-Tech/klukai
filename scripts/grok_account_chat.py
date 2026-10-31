#!/usr/bin/env python3
"""One Grok 4.7 turn at medium effort through the official account-login CLI.

No xAI API key and no paid credits. The process environment drops inherited
secrets, and the clean Grok home keeps ``disable_api_key_auth = true``.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import uuid

import agy_art_runtime as agy
from grok_image_render import CONFIG, EFFORT, MODEL, execute


class Turn:
    def __init__(self, text: str, model: str, effort: str, effort_source: str):
        self.text = text
        self.model = model
        self.effort = effort
        self.effort_source = effort_source


def arguments(session: str, system: str, prompt: str) -> list[str]:
    return [
        "--session-id", session, "--model", MODEL, "--effort", EFFORT,
        "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
        "--max-turns", "1", "--output-format", "streaming-messages-json",
        "--verbatim", "--system-prompt-override", system, "--single", prompt,
    ]


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


def parse_turn(raw: str, session: str, profile: Path) -> Turn:
    """Require OAuth, grok-4.7, and medium effort when the CLI reports it."""
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
        echoed, source = EFFORT, "flag"
    if echoed != EFFORT:
        raise agy.RuntimeError("Grok chat effort was not medium")
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
    return Turn(text.strip(), MODEL, echoed, source)


def complete(system: str, prompt: str, *, timeout: float = 90, cancel=None) -> Turn:
    if cancel is not None and cancel.is_set():
        raise agy.RuntimeError("Grok chat was cancelled")
    if not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
        raise agy.RuntimeError("Invalid Grok chat request")
    session = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="klukai-grok-chat-") as temp:
        root = Path(temp)
        profile, work = root / "profile", root / "work"
        profile.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        config = profile / ".grok"
        config.mkdir()
        (config / "config.toml").write_text(CONFIG)
        raw = execute(profile, work, arguments(session, system, prompt), timeout=timeout, cancel=cancel)
        return parse_turn(raw, session, profile)
