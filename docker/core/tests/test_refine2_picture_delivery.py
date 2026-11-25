"""How a picture is handed over, or not. Refused, failed and empty stay distinct."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import account_images
from app import background as bg
from app import picture_intent as pi
from app.image_provider import asks_for_sex_act

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from grok_image_check import background_only_passes  # noqa: E402
import xai_image  # noqa: E402

_ACT = re.compile(
    r"\b(?:sex|fucks?|fucking|orgasms?|intercourse|nsfw|moaning|explicit|spread legs)\b",
    re.I,
)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "u1,jalsarraf")
    pi._LAST.clear()
    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=SimpleNamespace(level=9))
    memory = MagicMock()
    memory.get_session = AsyncMock(return_value=None)
    memory.record_milestone = AsyncMock()
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_proactive = AsyncMock()
    ws.send_thinking = AsyncMock()
    proactive = MagicMock()
    proactive.record_first = AsyncMock()
    context_mod = MagicMock()
    memory_archive = MagicMock()
    memory_archive.save_image = AsyncMock(return_value="mem-1")
    memory_archive.update_kept = AsyncMock(return_value=True)
    monkeypatch.setattr(bg, "affection", affection)
    monkeypatch.setattr(bg, "memory", memory)
    monkeypatch.setattr(bg, "ws", ws)
    monkeypatch.setattr(bg, "proactive", proactive)
    monkeypatch.setattr(bg, "context", context_mod)
    monkeypatch.setattr(bg, "memory_archive", memory_archive)
    monkeypatch.setattr(bg, "session_id", lambda uid: f"session:{uid}")
    monkeypatch.setattr("app.her_day.render_costume", AsyncMock(return_value=None))
    monkeypatch.setattr("app.her_day.render_place", AsyncMock(return_value=None))
    yield SimpleNamespace(ws=ws, affection=affection, memory_archive=memory_archive)
    pi._LAST.clear()


def _said(ws) -> list[str]:
    return [call.args[1] for call in ws.send_proactive.await_args_list if call.args]


async def _draw(request: str, outcome: dict | None, *, user_id: str = "u1"):
    fulfill = AsyncMock(return_value=outcome)
    with patch("app.account_images.fulfill_account_image", new=fulfill):
        await bg.background_image_gen(request, user_id=user_id, recent_turns=[])
    return fulfill


def test_the_three_lines_are_not_the_same_sentence():
    refused = bg.delivery_line({"phase": "refused"}, covered=True)
    blip = bg.delivery_line({"phase": "failed"}, covered=True)
    covered = bg.delivery_line({"phase": "failed", "empty": True}, covered=True)
    assert len({refused, blip, covered}) == 3
    assert "rephrase" in refused.lower()
    assert "ask me again" in blip.lower()
    assert "visualization failed" in blip.lower()
    assert "drew it covered" in covered.lower()
    assert "pipeline" not in covered.lower()
    assert "failed" not in covered.lower()
    # A refusal is a rephrase. Nothing in the line turns the checker off.
    assert "disable" not in refused.lower()
    assert "skip" not in refused.lower()


async def test_a_safety_refusal_asks_him_to_rephrase_and_keeps_the_check(wired):
    fulfill = await _draw(
        "draw yourself in the bath",
        {"phase": "refused", "image": None, "request_id": "r-refused"},
    )
    # The grant still went out. The refusal is not a reason to drop the check
    # or to send the picture again without it.
    fulfill.assert_awaited_once()
    assert fulfill.await_args.kwargs["nudity"] is True
    said = _said(wired.ws)
    assert said == [bg._REFUSED_LINE]
    assert pi.last_picture("u1")["outcome"] == "failed"


async def test_a_blip_asks_him_again(wired):
    await _draw(
        "draw the hangar",
        {"phase": "failed", "image": None, "request_id": "r-failed"},
    )
    assert _said(wired.ws) == [bg._BLIP_LINE]
    assert pi.last_picture("u1")["outcome"] == "failed"


async def test_an_empty_implied_cover_is_not_a_pipeline_failure(wired):
    await _draw(
        "draw yourself in the bath",
        {
            "phase": "failed",
            "empty": True,
            "image": None,
            "request_id": "r-empty",
        },
    )
    assert _said(wired.ws) == [bg._COVERED_LINE]
    frames = [call.args[1] for call in wired.ws.send.await_args_list]
    assert not any(isinstance(frame, dict) and frame.get("type") == "image" for frame in frames)
    assert pi.last_picture("u1")["outcome"] == "covered"


async def test_a_covered_picture_is_delivered_as_covered(wired):
    png = b"\x89PNG\r\n\x1a\ncovered"
    await _draw(
        "draw yourself in the bath",
        {"phase": "completed", "image": png, "request_id": "r-drawn"},
    )
    said = _said(wired.ws)
    assert said == [bg._COVERED_LINE]
    frames = [call.args[1] for call in wired.ws.send.await_args_list]
    assert any(isinstance(frame, dict) and frame.get("type") == "image" for frame in frames)
    assert pi.last_picture("u1")["outcome"] == "delivered"


async def test_a_sex_act_is_not_what_the_model_is_asked_to_draw(wired):
    fulfill = await _draw("draw yourself having sex", {"phase": "completed", "image": b"png", "request_id": "r-act"})
    fulfill.assert_awaited_once()
    prompt = fulfill.await_args.args[1]
    assert _ACT.search(prompt) is None
    assert "bedsheet" in prompt or "towel" in prompt
    assert fulfill.await_args.kwargs["include_commander"] is False
    assert fulfill.await_args.kwargs["nudity"] is True


async def test_a_sex_act_with_him_is_not_drawn(wired):
    fulfill = await _draw(
        "draw us having sex",
        {"phase": "completed", "image": b"png", "request_id": "r-us"},
        user_id="jalsarraf",
    )
    fulfill.assert_not_awaited()
    assert _said(wired.ws) == [bg._REFUSED_LINE]


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("draw yourself having sex", True),
        ("that dress is sexy", False),
        ("no sex in this one", False),
        ("draw the hangar", False),
    ],
)
def test_only_a_sex_act_counts_as_one(text, asked):
    assert asks_for_sex_act(text) is asked


async def test_a_keep_is_scoped_to_the_user(wired):
    await bg.do_memory_keep("mem-7", kept=True, user_id="alice")
    wired.memory_archive.update_kept.assert_awaited_once_with(
        "mem-7", kept=True, kept_by="commander", user_id="alice"
    )
    wired.memory_archive.update_kept.reset_mock()
    await bg.do_memory_keep("mem-7", kept=False, user_id="alice")
    assert wired.memory_archive.update_kept.await_args.kwargs["user_id"] == "alice"
    assert wired.memory_archive.update_kept.await_args.kwargs["kept_by"] == "discarded"


async def test_completed_without_bytes_stays_ungenerated_and_is_marked_empty(monkeypatch):
    monkeypatch.setattr(account_images, "update_request", AsyncMock())
    monkeypatch.setattr(
        "app.agy_image_provider.submit_agy_image",
        AsyncMock(return_value={"status": "completed", "image": None}),
    )
    result = await account_images._submit_saved(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "user_id": "u1",
            "prompt": "a hangar",
            "width": 832,
            "height": 1216,
            "include_commander": False,
            "archive_context": {},
        }
    )
    assert result["phase"] == "failed" and result["generated"] is False
    assert result["empty"] is True


def test_a_differing_background_is_minor_and_a_3d_drift_is_not():
    softened = background_only_passes(
        {
            "verdict": "reject",
            "issues": [
                {"severity": "major", "what": "the background is a garden, not the room"}
            ],
        }
    )
    assert softened["verdict"] == "pass"
    assert softened["issues"][0]["severity"] == "minor"
    for what in ("photorealistic background", "3D-rendered background", "a photographic sky"):
        verdict = background_only_passes(
            {"verdict": "reject", "issues": [{"severity": "major", "what": what}]}
        )
        assert verdict["verdict"] == "reject", what
        assert verdict["issues"][0]["severity"] == "major", what
    assert xai_image.background_only_passes is background_only_passes
