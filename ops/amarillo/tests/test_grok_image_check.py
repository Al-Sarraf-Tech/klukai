"""Grok checks with the same rubric as Claude, on grok-4.7 at xhigh effort."""
import io
import json
import os
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
    assert grok.MODEL == "grok-4.7" and grok.image_effort() == "xhigh"


def test_prompt_json_is_an_acp_image_block_and_the_command_pins_the_model(monkeypatch):
    monkeypatch.setattr(grok, "read_image", lambda entry: png())
    document = json.loads(grok.prompt_document(png(), {"prompt": "rooftop", "include_commander": False},
                                               {"klukai": "ref"}))
    assert document["type"] == "acp"
    assert [block["type"] for block in document["content"]] == ["image", "image", "text"]
    assert document["content"][0]["mimeType"] == "image/jpeg"
    assert "rooftop" in document["content"][-1]["text"]
    args = grok.arguments(str(uuid.uuid4()), Path("prompt.json"))
    assert args[args.index("--model") + 1] == "grok-4.7"
    assert args[args.index("--effort") + 1] == "xhigh"
    assert args[args.index("--prompt-file") + 1] == "prompt.json"
    assert "--prompt-json" not in args
    assert "--system-prompt-override" in args


def test_a_real_size_image_stays_off_the_argv_and_a_failure_logs_its_type(monkeypatch, caplog):
    noisy = Image.frombytes("RGB", (640, 960), os.urandom(640 * 960 * 3))
    raw = io.BytesIO()
    noisy.save(raw, format="PNG")
    picture = raw.getvalue()
    monkeypatch.setattr(grok, "read_image", lambda entry: picture)
    seen = {}

    def execute(profile, work, args, timeout=170, cancel=None):
        seen["args"] = list(args)
        seen["payload"] = Path(args[args.index("--prompt-file") + 1]).read_text()
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(grok, "execute", execute)
    with caplog.at_level("WARNING"):
        result = grok.run(
            picture, {"prompt": "rooftop", "include_commander": False},
            {"klukai": "ref"}, timeout=30,
        )
    assert result["verdict"] == "unchecked"
    assert "OSError:7" in caplog.text
    args = seen["args"]
    assert max(len(arg) for arg in args) < grok.MAX_ARG_STRLEN
    payload = seen["payload"]
    assert len(payload.encode()) > grok.MAX_ARG_STRLEN
    document = json.loads(payload)
    assert document["type"] == "acp"
    assert payload not in args
