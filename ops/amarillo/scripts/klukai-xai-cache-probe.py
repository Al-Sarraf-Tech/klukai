#!/usr/bin/env python3
"""Show whether a real multi-turn chat caches a growing message prefix.

Uses KLUKAI_XAI_API_KEY from the environment. It does not read the key file
and it does not print the key. Three short turns, then one cached_tokens
number per turn. Chat completions stick to x-grok-conv-id; prompt_cache_key
is only on the Responses API.

    KLUKAI_XAI_API_KEY=... python3 ops/amarillo/scripts/klukai-xai-cache-probe.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

URL = "https://api.x.ai/v1/chat/completions"
MODEL = "grok-4.7"
PERSONA = "You are a terse assistant. Answer in one short sentence."
CONV = "klukai-cache-probe"


def messages(turns: list[tuple[str, str]], volatile: str) -> list[dict[str, str]]:
    built: list[dict[str, str]] = [{"role": "system", "content": PERSONA}]
    for role, text in turns:
        built.append({"role": role, "content": text})
    built.append({"role": "system", "content": volatile})
    return built


def _cached(usage: dict) -> int:
    details = usage.get("prompt_tokens_details") or {}
    raw = details.get("cached_tokens", usage.get("cached_tokens", 0))
    return int(raw or 0)


def main() -> int:
    key = os.environ.get("KLUKAI_XAI_API_KEY", "").strip()
    if not key:
        print("skip: KLUKAI_XAI_API_KEY is not set")
        return 0
    history: list[tuple[str, str]] = []
    user_lines = ["Name one color.", "Name one city.", "Name one river."]
    for index, line in enumerate(user_lines, start=1):
        history.append(("user", line))
        body = {
            "model": MODEL,
            "messages": messages(history, f"turn {index} note"),
            "stream": False,
            "reasoning_effort": "low",
            "max_tokens": 32,
        }
        request = urllib.request.Request(
            URL,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "x-grok-conv-id": CONV,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
        usage = payload.get("usage") or {}
        reply = payload["choices"][0]["message"]["content"].strip()
        print(
            f"turn={index} prompt_tokens={usage.get('prompt_tokens')} "
            f"cached_tokens={_cached(usage)}"
        )
        history.append(("assistant", reply))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"probe failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
