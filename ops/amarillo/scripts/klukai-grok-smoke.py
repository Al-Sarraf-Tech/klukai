#!/usr/bin/env python3
"""One real Grok picture, checker pass, ledger row, and chat turn.

Run on amarillo after deploy, as the user that owns the Grok login. This spends
one image attempt. It prints a pass/fail summary and never prints credentials,
prompts, or image bytes.

    GROK_AUTH_PATH=$HOME/.grok/auth.json \
    KLUKAI_GROK_DAILY_LIMIT=10 \
    KLUKAI_GROK_MONTHLY_LIMIT=150 \
    python3 ops/amarillo/scripts/klukai-grok-smoke.py

The reference manifest defaults to
~/.local/share/klukai/agy-bridge/references.json. Override with
KLUKAI_SMOKE_REFERENCES. The ledger defaults to the worker's
grok-attempts.sqlite3.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import grok_account_chat  # noqa: E402
import grok_image_check  # noqa: E402
import grok_image_render  # noqa: E402


def _line(name: str, ok: bool, detail: str) -> str:
    return f"{name}: {'pass' if ok else 'fail'} {detail}".rstrip()


def chat_turn() -> tuple[bool, str]:
    turn = grok_account_chat.complete(
        "You are Klukai. Reply with one word.",
        "Reply with the single word ready.",
        timeout=90,
    )
    ok = (
        turn.model == grok_account_chat.MODEL
        and turn.effort == grok_account_chat.chat_effort()
        and turn.effort_source in {"stream", "summary"}
        and bool(turn.text)
    )
    return ok, f"model={turn.model} effort={turn.effort} source={turn.effort_source}"


def ledger_count() -> int:
    path = Path(os.environ.get("KLUKAI_GROK_LEDGER", str(grok_image_render.DEFAULT_LEDGER)))
    if not path.is_file():
        return 0
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT COUNT(*) FROM grok_attempts").fetchone()[0]


def main() -> int:
    lines = []
    ok = True
    before = 0
    try:
        before = ledger_count()
    except Exception:
        lines.append(_line("ledger", False, "unreadable before the render"))
        ok = False
    references = Path(os.environ.get(
        "KLUKAI_SMOKE_REFERENCES",
        str(Path.home() / ".local/share/klukai/agy-bridge/references.json"),
    ))
    try:
        manifest = json.loads(references.read_text())
        request = {
            "request_id": str(uuid.uuid4()),
            "user_id": "jalsarraf",
            "prompt": "adult Klukai standing on a rooftop at dusk, coat, calm expression",
            "width": 832,
            "height": 1216,
            "include_commander": False,
        }
        image = grok_image_render.render(request, manifest)
        lines.append(_line("image", image.startswith(b"\x89PNG"), "png"))
        ok = ok and image.startswith(b"\x89PNG")
    except Exception as exc:
        lines.append(_line("image", False, type(exc).__name__))
        ok = False
        image = b""
    if image:
        try:
            verdict = grok_image_check.run(
                image, {"prompt": request["prompt"], "include_commander": False},
                manifest, timeout=90,
            )
            passed = verdict.get("verdict") == "pass"
            lines.append(_line("check", passed, f"verdict={verdict.get('verdict')}"))
            ok = ok and passed
        except Exception as exc:
            lines.append(_line("check", False, type(exc).__name__))
            ok = False
    try:
        after = ledger_count()
        recorded = after == before + 1
        lines.append(_line("ledger", recorded, f"attempts={after}"))
        ok = ok and recorded
    except Exception as exc:
        lines.append(_line("ledger", False, type(exc).__name__))
        ok = False
    try:
        chat_ok, detail = chat_turn()
        lines.append(_line("chat", chat_ok, detail))
        ok = ok and chat_ok
    except Exception as exc:
        lines.append(_line("chat", False, type(exc).__name__))
        ok = False
    lines.append("SMOKE PASS" if ok else "SMOKE FAIL")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
