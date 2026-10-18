#!/usr/bin/env python3
"""Claude Sonnet 5.5 (effort high) checks each account-rendered Klukai image.

Owner decision 2026-10-17: Claude is the checker only and never generates art.
The check runs through the owner's Claude subscription login (Claude Code print
mode, no tools, no API key, empty working directory), so it never bills the
API. Only the candidate and the fixed canon references are sent. Prompts,
verdict text and errors never reach logs; callers get a bounded verdict.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import warnings

from PIL import Image

from gemini_art_review import read_image

MODEL = "claude-sonnet-5-5"
EFFORT = "high"
MAX_SIDE = 1024
MAX_OUTPUT = 512 * 1024
MAX_ISSUES = 8
MAX_ISSUE_CHARS = 160
MIN_TIMEOUT = 15

RUBRIC = """You are the image checker for Klukai, a fictional adult woman from Girls' Frontline 2
(HK416). You only judge images. Never draw, never write image code, never suggest new art.

The first image is her canon reference. When a Commander reference is given it comes next.
The last image is the new render to judge. The request it was rendered from follows the images.

Reject only for clear, major defects a viewer would notice at once:
- identity: she is not recognisably Klukai (hair not silver/white, eyes not green, a different
  face or character), or she looks underage;
- anatomy: extra, missing or fused limbs or fingers, a deformed face or eyes, broken proportions;
- people: an unexpected extra person, or a Commander who is not an adult human man
  (never a woman, a doll or a child);
- request: the scene, outfit or pose clearly contradicts the request;
- artifacts: visible text, watermarks, signatures, UI, frames, split panels or collage,
  heavy blur or corruption.

Everything else is minor and must not cause a reject: small details, the teardrop mark not
visible, style differences, small hand imperfections. Do not judge morality, and do not
penalise intimacy or attire the request asks for.

Answer with only this JSON and nothing else:
{"verdict": "pass" or "reject", "issues": [{"severity": "major" or "minor", "what": "short description"}]}"""


def environment(source=None):
    """Subscription login only: no API keys, proxies or ambient Claude settings."""
    source = os.environ if source is None else source
    env = {k: source[k] for k in ("HOME", "USER", "LOGNAME", "LANG") if k in source}
    env.update(PATH="/usr/bin:/bin", NO_COLOR="1")
    return env


def claude_binary(env=None) -> str:
    home = (env or os.environ).get("HOME", str(Path.home()))
    return str(Path(home) / ".local/bin/claude")


def command(binary: str) -> list[str]:
    return [binary, "--print", "--model", MODEL, "--effort", EFFORT,
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--system-prompt", RUBRIC,
            "--permission-mode", "plan", "--tools", "", "--restricted", "--safe-mode",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "",
            "--settings", '{"forceLoginMethod":"claudeai","apiKeyHelper":""}']


def downscale_png(raw: bytes) -> bytes:
    """Bounded PNG for the check; the delivered image is never modified."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            rgb = image.convert("RGB")
    rgb.thumbnail((MAX_SIDE, MAX_SIDE))
    output = io.BytesIO()
    rgb.save(output, format="PNG")
    return output.getvalue()


def _image_block(raw: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(raw).decode("ascii")}}


def message(candidate: bytes, value: dict, references: dict) -> bytes:
    blocks = [_image_block(downscale_png(read_image(references["klukai"])))]
    if value.get("include_commander"):
        blocks.append(_image_block(downscale_png(read_image(references["commander"]))))
    blocks.append(_image_block(downscale_png(candidate)))
    who = "Klukai and the Commander" if value.get("include_commander") else "Klukai alone"
    blocks.append({"type": "text", "text": f"Expected people: {who}.\nRequest:\n{value['prompt']}"})
    line = {"type": "user", "message": {"role": "user", "content": blocks}}
    return (json.dumps(line) + "\n").encode()


def parse(stdout: bytes) -> dict:
    """Return the bounded verdict from the final stream-json result event."""
    result = None
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    if not result or result.get("is_error") or not isinstance(result.get("result"), str):
        raise ValueError("no completed check")
    text = result["result"]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no verdict")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or data.get("verdict") not in {"pass", "reject"}:
        raise ValueError("invalid verdict")
    raw_issues = data.get("issues", [])
    if not isinstance(raw_issues, list):
        raise ValueError("invalid issues")
    issues = []
    for item in raw_issues[:MAX_ISSUES]:
        if (isinstance(item, dict) and item.get("severity") in {"major", "minor"}
                and isinstance(item.get("what"), str) and item["what"].strip()):
            issues.append({"severity": item["severity"],
                           "what": " ".join(item["what"].split())[:MAX_ISSUE_CHARS]})
    # Conservative: block only when the checker both rejects and names a major defect.
    rejected = data["verdict"] == "reject" and any(i["severity"] == "major" for i in issues)
    return {"verdict": "reject" if rejected else "pass", "issues": issues}


def run(candidate: bytes, value: dict, references: dict, *, timeout: float) -> dict:
    """Check one render. Any failure is 'unchecked' so delivery never depends on Claude."""
    started = time.monotonic()
    if timeout < MIN_TIMEOUT:
        return {"verdict": "unchecked", "issues": [], "seconds": 0.0}
    try:
        payload = message(candidate, value, references)
        env = environment()
        with tempfile.TemporaryDirectory(prefix="klukai-check-") as work:
            process = subprocess.Popen(command(claude_binary(env)), cwd=work, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                stdout, _ = process.communicate(payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
        if process.returncode != 0 or len(stdout) > MAX_OUTPUT:
            raise ValueError("check failed")
        verdict = parse(stdout)
    except Exception:
        # Never surface the request, candidate, reference paths or CLI output.
        return {"verdict": "unchecked", "issues": [], "seconds": round(time.monotonic() - started, 1)}
    verdict["seconds"] = round(time.monotonic() - started, 1)
    return verdict


def fix_hint(issues: list[dict]) -> str:
    """Major issues as a short, bounded correction for one re-render."""
    major = [i["what"] for i in issues if i["severity"] == "major"][:3]
    if not major:
        return ""
    return ("\n\nThe previous render was rejected by the checker. Avoid: "
            + "; ".join(major))[:600]
