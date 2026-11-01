#!/usr/bin/env python3
"""Claude Sonnet 5.5 (effort high) checks each account-rendered Klukai image.

Owner decision 2026-10-17: Claude is the checker only and never generates art.
The check runs through the owner's Claude subscription login (Claude Code print
mode, no tools, no API key, empty working directory), so it never bills the
API. Only the candidate and the fixed canon references are sent. Prompts,
verdict text and errors never reach logs; callers get a bounded verdict.
"""
from __future__ import annotations

import re
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

from commander_description import COMMANDER_TEXT_DESCRIPTION
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

The first image is Klukai's canon reference. The last image is the new render to judge.
The request it was rendered from follows the images. The Commander has no image reference;
when he is requested, use only the detailed text description in the request.

Reject only for clear, major defects a viewer would notice at once:
- identity: she is not recognisably Klukai (hair not silver/white, eyes not green, a different
  face or character), or she looks underage;
- anatomy: extra, missing or fused limbs or fingers, a deformed face or eyes, broken proportions;
- people: an unexpected extra person, anyone who looks underage, or a Commander who is not an adult human man
  (never a woman, a doll or a child);
- request: the subject, outfit or pose clearly contradicts the request, or a thing its first line asks to see (her bike, say) is not in the picture at all. A first line starting "Setting:" asks to see nothing;
- artifacts: visible text, watermarks, signatures, UI, frames, split panels or collage,
  heavy blur or corruption;
- style: the render is photorealistic, photographic, 3D-rendered or semi-realistic
  instead of a 2D anime illustration like the reference, in the figures or the background.

Everything else is minor and must not cause a reject: small details, the teardrop mark not
visible, small hand imperfections, a setting or background that differs from the one asked for
(some sky in an indoor scene, say). Do not judge morality, and do not
penalise intimacy or attire the request asks for.

Answer with only this JSON and nothing else:
{"verdict": "pass" or "reject", "issues": [{"severity": "major" or "minor", "what": "short description"}]}"""


def expected_people(value: dict) -> str:
    """Who the render was asked to show, in the terms the renderer's brief uses."""
    if value.get("include_commander"):
        return "Klukai and the Commander"
    if re.search(r"\b(?:[2-9] women|multiple girls)\b", str(value.get("prompt", ""))):
        return "Klukai and the other adult women the request describes"
    return "Klukai alone"


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
            "--permission-mode", "bypassPermissions", "--tools", "", "--safe-mode",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "",
            "--settings", '{"forceLoginMethod":"claudeai","apiKeyHelper":"",'
            '"sandbox":{"enabled":false},"skipDangerousModePermissionPrompt":true}']


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
    blocks.append(_image_block(downscale_png(candidate)))
    who = expected_people(value)
    description = (
        f"\nCommander description (text only): {COMMANDER_TEXT_DESCRIPTION}"
        if value.get("include_commander")
        else ""
    )
    blocks.append({
        "type": "text",
        "text": f"Expected people: {who}.{description}\nRequest:\n{value['prompt']}",
    })
    line = {"type": "user", "message": {"role": "user", "content": blocks}}
    return (json.dumps(line) + "\n").encode()


def validate_verdict(data: object) -> dict:
    """Require an explicit, consistent verdict before bounding its evidence."""
    if not isinstance(data, dict) or data.get("verdict") not in ("pass", "reject"):
        raise ValueError("invalid verdict")
    raw_issues = data.get("issues")
    if not isinstance(raw_issues, list):
        raise ValueError("invalid issues")
    issues = []
    for item in raw_issues:
        if (not isinstance(item, dict) or item.get("severity") not in ("major", "minor")
                or not isinstance(item.get("what"), str) or not item["what"].strip()):
            raise ValueError("invalid issue")
        issues.append({"severity": item["severity"],
                       "what": " ".join(item["what"].split())[:MAX_ISSUE_CHARS]})
    has_major = any(i["severity"] == "major" for i in issues)
    if (data["verdict"] == "reject") != has_major:
        # Minor details still cannot trigger a redraw; an inconsistent review
        # is unavailable, never silently promoted into approval.
        raise ValueError("inconsistent verdict")
    # A late major issue must survive the display/storage bound and reach the
    # correction hint. Sorting is stable within each severity.
    issues.sort(key=lambda item: item["severity"] != "major")
    return {"verdict": data["verdict"], "issues": issues[:MAX_ISSUES]}


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
    return validate_verdict(json.loads(text[start:end + 1]))


def run(candidate: bytes, value: dict, references: dict, *, timeout: float) -> dict:
    """Check one render. Failure is 'unchecked', never image approval."""
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


# Quoted words in an issue are text the checker read in the image. A word shown to
# an image model tends to get painted, so the retry never repeats them.
_QUOTED = re.compile(r"""(?<!\w)['"\u2018\u201c].{1,60}?['"\u2019\u201d](?!\w)""")


# Shown "avoid the open sky", the model painted it again in all 3 pictures rejected twice.
# So a defect we can name becomes what should be there instead (xAI's guide: "Say the
# thing you want to see"), and anything else stays a short description of the defect.
_FIXES = (
    (re.compile(r"\b(?:text|letter\w*|logo|signs?|signage|writing|caption|watermark|label|name.?tape)\b", re.I),
     "Every surface is plain: signs, screens, labels and crates show only flat colour and simple shapes."),
    (re.compile(r"\b(?:extra|additional|second|another|unexpected) (?:person|people|man|woman|figure|girl)\b", re.I),
     "The people in the picture are only those named above."),
    (re.compile(r"\b(?:photo\w*|realistic|semi-realistic|3d)\b", re.I),
     "Flat cel-shaded 2D anime colour with clean ink lines on the figure and the background."),
    (re.compile(r"\b(?:limbs?|fingers?|hands?|arms?|legs?|anatomy|proportions?)\b", re.I),
     "Natural anatomy: two arms, two legs, five fingers on each hand, in a simple clear pose."),
)


def fix_hint(issues: list[dict]) -> str:
    """Major issues as a short, bounded correction for one re-render."""
    major = [i["what"] for i in issues
             if isinstance(i, dict) and i.get("severity") == "major" and isinstance(i.get("what"), str)][:3]
    if not major:
        return ""
    fixes: list[str] = []
    for what in major:
        fix = next((fix for pattern, fix in _FIXES if pattern.search(what)), None)
        line = fix or "Correct this: " + _QUOTED.sub("lettering", what)
        if line not in fixes:
            fixes.append(line)
    return ("\n\nFor this picture: " + " ".join(fixes))[:600]
