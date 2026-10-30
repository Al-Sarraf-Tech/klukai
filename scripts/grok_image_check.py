#!/usr/bin/env python3
"""Grok 4.7 (effort medium) checks each account-rendered Klukai image.

Owner decision 2026-10-30: all of Klukai's model use runs through the official
Grok CLI on the account login. This checker uses the same rubric as the Claude
checker (identity, anatomy, people, request, artifacts, and 2D-anime drift).
It never draws. No API key, no paid credits. Failure is "unchecked", never
approval. Prompts and CLI output are not logged.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import tempfile
import time
import uuid

from PIL import Image

from claude_image_check import (
    MAX_ISSUES,
    MIN_TIMEOUT,
    RUBRIC,
    downscale_png,
    fix_hint,
    parse,
    validate_verdict,
)
from commander_description import COMMANDER_TEXT_DESCRIPTION
from gemini_art_review import read_image
from grok_image_render import CONFIG, EFFORT, MODEL, execute

# Re-exported so the worker can report the checker the same way as before.
__all__ = ["MODEL", "EFFORT", "RUBRIC", "run", "fix_hint", "validate_verdict"]


def _jpeg(raw: bytes) -> bytes:
    """A small JPEG so the ACP image block fits on the CLI argument list."""
    with Image.open(io.BytesIO(downscale_png(raw))) as image:
        rgb = image.convert("RGB")
    rgb.thumbnail((768, 768))
    output = io.BytesIO()
    rgb.save(output, format="JPEG", quality=70)
    return output.getvalue()


def _block(raw: bytes) -> dict:
    return {
        "type": "image",
        "mimeType": "image/jpeg",
        "data": base64.b64encode(_jpeg(raw)).decode("ascii"),
    }


def prompt_document(candidate: bytes, value: dict, references: dict) -> str:
    """ACP content the pinned CLI accepts as ``--prompt-json``."""
    who = "Klukai and the Commander" if value.get("include_commander") else "Klukai alone"
    description = (
        f"\nCommander description (text only): {COMMANDER_TEXT_DESCRIPTION}"
        if value.get("include_commander")
        else ""
    )
    blocks = [
        _block(read_image(references["klukai"])),
        _block(candidate),
        {"type": "text", "text": f"Expected people: {who}.{description}\nRequest:\n{value['prompt']}"},
    ]
    return json.dumps({"type": "acp", "content": blocks}, separators=(",", ":"))


def arguments(session: str, payload: str) -> list[str]:
    return [
        "--session-id", session, "--model", MODEL, "--effort", EFFORT,
        "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
        "--max-turns", "1", "--output-format", "streaming-messages-json",
        "--verbatim", "--system-prompt-override", RUBRIC,
        "--prompt-json", payload,
    ]


def _require_model(raw: str) -> None:
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    init = [row for row in rows if row.get("type") == "system" and row.get("subtype") == "init"]
    if len(init) != 1 or init[0].get("model") != MODEL:
        raise ValueError("checker model was not grok-4.7")
    if init[0].get("apiKeySource") != "oauth":
        raise ValueError("checker authentication was not OAuth")
    echoed = init[0].get("reasoning_effort", init[0].get("effort"))
    if echoed is not None and echoed != EFFORT:
        raise ValueError("checker effort was not medium")


def run(candidate: bytes, value: dict, references: dict, *, timeout: float) -> dict:
    """Check one render. Failure is 'unchecked', never image approval."""
    started = time.monotonic()
    if timeout < MIN_TIMEOUT:
        return {"verdict": "unchecked", "issues": [], "seconds": 0.0}
    try:
        payload = prompt_document(candidate, value, references)
        session = str(uuid.uuid4())
        with tempfile.TemporaryDirectory(prefix="klukai-grok-check-") as temp:
            root = Path(temp)
            profile, work = root / "profile", root / "work"
            profile.mkdir(mode=0o700)
            work.mkdir(mode=0o700)
            config = profile / ".grok"
            config.mkdir()
            (config / "config.toml").write_text(CONFIG)
            raw = execute(profile, work, arguments(session, payload), timeout=timeout)
        _require_model(raw)
        verdict = parse(raw.encode("utf-8"))
    except Exception:
        return {"verdict": "unchecked", "issues": [], "seconds": round(time.monotonic() - started, 1)}
    verdict["issues"] = verdict["issues"][:MAX_ISSUES]
    verdict["seconds"] = round(time.monotonic() - started, 1)
    return verdict
