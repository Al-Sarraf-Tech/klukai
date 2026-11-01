#!/usr/bin/env python3
"""A/B documented xAI chat models on the same short prompts.

Production chat stays on grok-4.7. This script does not change that. It
reads KLUKAI_XAI_API_KEY from the environment, never from a file, and it
does not print the key. Grok 4.7 Fast is not on the public API, so the
faster slug here is the documented non-reasoning model.

    python3 ops/amarillo/scripts/klukai-xai-chat-benchmark.py --dry-run
    KLUKAI_XAI_API_KEY=... python3 ops/amarillo/scripts/klukai-xai-chat-benchmark.py \\
        --out /tmp/klukai-chat-benchmark

Each run records first-token time, total time, token counts, cost, and
the reply text for a character-quality read. Prices below match
https://docs.x.ai/developers/models for prompts under 200k tokens
(fetched 2026-10-31): $2 / $0.50 / $6 per million input, cached input,
and output. A billed tick count wins when the response includes one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://api.x.ai/v1/chat/completions"
# Text models on the docs page above. Image and voice slugs are not chat.
DOCUMENTED = frozenset(
    {
        "grok-4.7",
        "grok-4.6",
        "grok-4.5",
        "grok-4.3",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309-non-reasoning",
        "grok-build-0.1",
        "grok-4.20-multi-agent-0309",
    }
)
DEFAULT_MODELS = ("grok-4.7", "grok-4.20-0309-non-reasoning")
PROMPTS = (
    (
        "greeting",
        "You are Klukai. You address him only as Commander. "
        "Reply in one or two sentences, in character. No preamble.",
        "Commander just came in from the rain. Greet him.",
    ),
    (
        "memory",
        "You are Klukai. You address him only as Commander. "
        "Reply in one or two sentences, in character. No preamble.",
        "He asks if you remember the night the hangar lights went out.",
    ),
    (
        "stage",
        "You are Klukai. You address him only as Commander. "
        "A stage direction, if you use one, is in parentheses and starts with I. "
        "Reply in one or two sentences. No preamble.",
        "He says he is staying. Answer him.",
    ),
)


class PlanError(ValueError):
    """The requested model is not on the documented chat list."""


def models_from_arg(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_MODELS
    chosen = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not chosen or any(name not in DOCUMENTED for name in chosen):
        raise PlanError("undocumented chat model")
    return chosen


def wants_reasoning(model: str) -> bool:
    """Low effort on reasoning models. The non-reasoning slug omits it."""
    if "non-reasoning" in model or model.startswith("grok-build"):
        return False
    if "multi-agent" in model:
        return False
    return True


def request_body(model: str, system: str, user: str) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 256,
    }
    if wants_reasoning(model):
        body["reasoning_effort"] = "low"
    return body


def usd(prompt: int, cached: int, output: int, ticks: object) -> float:
    if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 0:
        return ticks / 10_000_000_000
    fresh_in = max(0, prompt)
    cached_n = min(max(0, cached), fresh_in)
    fresh = fresh_in - cached_n
    return (fresh * 2.0 + cached_n * 0.5 + max(0, output) * 6.0) / 1_000_000


def _delta(event: dict) -> str:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return ""
    text = delta.get("content")
    return text if isinstance(text, str) else ""


def consume(line: str, parts: list[str], started: float, state: dict) -> None:
    if not line.startswith("data:"):
        return
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return
    event = json.loads(data)
    if not isinstance(event, dict):
        return
    piece = _delta(event)
    if piece:
        if state.get("first") is None:
            state["first"] = time.perf_counter() - started
        parts.append(piece)
    usage = event.get("usage")
    if isinstance(usage, dict):
        state["usage"] = usage


def _counts(usage: dict) -> tuple[int, int, int, object]:
    def num(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = num(details.get("cached_tokens"))
    if cached == 0:
        cached = num(usage.get("cached_tokens"))
    return (
        num(usage.get("prompt_tokens")),
        cached,
        num(usage.get("completion_tokens")),
        usage.get("cost_in_usd_ticks"),
    )


def run_one(key: str, model: str, name: str, system: str, user: str) -> dict:
    body = request_body(model, system, user)
    request = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    parts: list[str] = []
    state: dict = {"first": None, "usage": {}}
    with urllib.request.urlopen(request, timeout=120) as response:
        for raw in response:
            consume(raw.decode("utf-8", "replace").strip(), parts, started, state)
    total = time.perf_counter() - started
    prompt, cached, output, ticks = _counts(state["usage"])
    return {
        "model": model,
        "prompt": name,
        "ttft_s": state["first"],
        "total_s": round(total, 3),
        "input_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": output,
        "usd": round(usd(prompt, cached, output, ticks), 6),
        "reply": "".join(parts).strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B documented xAI chat models")
    parser.add_argument("--models", default="", help="Comma-separated documented slugs")
    parser.add_argument("--out", default="", help="Directory for replies and summary.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        chosen = models_from_arg(args.models or None)
    except PlanError:
        print("undocumented chat model", file=sys.stderr)
        return 2
    if args.dry_run:
        for model in chosen:
            for name, system, user in PROMPTS:
                body = request_body(model, system, user)
                flag = "yes" if "reasoning_effort" in body else "no"
                print(f"dry-run model={model} prompt={name} reasoning={flag}")
        return 0
    if not args.out:
        print("pass --out DIR", file=sys.stderr)
        return 2
    key = os.environ.get("KLUKAI_XAI_API_KEY", "").strip()
    if not key:
        print("KLUKAI_XAI_API_KEY is not set", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []
    failed = False
    for model in chosen:
        for name, system, user in PROMPTS:
            try:
                row = run_one(key, model, name, system, user)
            except urllib.error.HTTPError as exc:
                failed = True
                print(f"model={model} prompt={name} http={exc.code}", file=sys.stderr)
                runs.append({"model": model, "prompt": name, "error": f"http {exc.code}"})
                continue
            except Exception as exc:
                failed = True
                print(
                    f"model={model} prompt={name} error={type(exc).__name__}",
                    file=sys.stderr,
                )
                runs.append(
                    {"model": model, "prompt": name, "error": type(exc).__name__}
                )
                continue
            reply = row.pop("reply")
            path = out / f"{model}--{name}.txt"
            path.write_text(reply + "\n", encoding="utf-8")
            row["reply_file"] = path.name
            runs.append(row)
            ttft = "-" if row["ttft_s"] is None else f"{row['ttft_s']:.3f}"
            print(
                f"model={model} prompt={name} ttft_s={ttft} "
                f"total_s={row['total_s']:.3f} usd={row['usd']:.6f}"
            )
    summary = {"models": list(chosen), "runs": runs}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
