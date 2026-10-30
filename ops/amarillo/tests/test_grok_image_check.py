"""Grok checks with the same rubric as Claude, on grok-4.7 at medium effort."""
import io
import json
from pathlib import Path
import sys
import uuid

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import claude_image_check as claude  # noqa: E402
import grok_image_check as grok  # noqa: E402


def png():
    out = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(out, format="PNG")
    return out.getvalue()


def test_the_rubric_and_drift_rule_match_the_claude_checker():
    assert grok.RUBRIC is claude.RUBRIC
    assert "photorealistic" in grok.RUBRIC and "3D-rendered" in grok.RUBRIC
    assert grok.MODEL == "grok-4.7" and grok.EFFORT == "medium"


def test_prompt_json_is_an_acp_image_block_and_the_command_pins_the_model(monkeypatch):
    monkeypatch.setattr(grok, "read_image", lambda entry: png())
    document = json.loads(grok.prompt_document(png(), {"prompt": "rooftop", "include_commander": False},
                                               {"klukai": "ref"}))
    assert document["type"] == "acp"
    assert [block["type"] for block in document["content"]] == ["image", "image", "text"]
    assert document["content"][0]["mimeType"] == "image/jpeg"
    assert "rooftop" in document["content"][-1]["text"]
    args = grok.arguments(str(uuid.uuid4()), "{}")
    assert args[args.index("--model") + 1] == "grok-4.7"
    assert args[args.index("--effort") + 1] == "medium"
    assert "--system-prompt-override" in args
