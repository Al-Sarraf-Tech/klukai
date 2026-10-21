"""Owner decision 2026-10-19: tasteful nudity of Klukai alone, past the intimacy gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import account_images
from app import agy_image_provider as agy
from app.image_provider import nudity_allowed


@pytest.mark.parametrize(
    "text",
    [
        "Klukai nude in the hot spring",
        "naked after the shower",
        "she is bathing at the onsen",
        "a bath together? no, just her in the bathtub",
        "undressing for a skinny-dip",
        "Topless on the beach",
    ],
)
def test_cues_grant_only_past_the_gate_and_alone(text):
    assert nudity_allowed(text, intimate=True, include_commander=False) is True
    assert nudity_allowed(text, intimate=False, include_commander=False) is False
    assert nudity_allowed(text, intimate=True, include_commander=True) is False


@pytest.mark.parametrize(
    "text",
    [
        "Klukai in bed reading",  # "bed" alone is not a request
        "watching a meteor shower from the hangar roof",
        "a bloodbath in the contaminated zone",
        "Klukai in her bathing suit at the beach",  # swimwear, not nudity
        "two bathing suits drying on the Elmo's rail",
        "",
        None,
    ],
)
def test_ordinary_scenes_are_never_granted(text):
    assert nudity_allowed(text, intimate=True, include_commander=False) is False


@pytest.mark.parametrize(
    "text",
    [
        "Klukai and Groza bathing at the onsen",
        "Leva and Klukai naked in the hot spring",
        "1girl, klukai, 2girls, multiple girls, onsen, bathing",
    ],
)
def test_never_granted_when_she_is_not_alone(text):
    """Klukai alone: another girl in the picture is never part of a grant."""
    assert nudity_allowed(text, intimate=True, include_commander=False) is False


@pytest.mark.parametrize(
    "text",
    [
        "Klukai caught in a rain shower on patrol",
        "Klukai at a baby shower",
        "planning a bridal shower",
        "Klukai at the onsen, not naked",
        "a bath scene but no nudity please",
        "Klukai by the hot spring without undressing",
    ],
)
def test_his_words_must_actually_ask(text):
    """Only his words asking for it grant it: weather, parties and a refusal
    are not requests."""
    assert nudity_allowed(text, intimate=True, include_commander=False) is False


@pytest.fixture
def agy_on(monkeypatch):
    monkeypatch.setattr(agy, "uses_agy_images", lambda user_id: True)


@pytest.mark.asyncio
async def test_payload_carries_the_grant_only_when_given(agy_on):
    base = dict(user_id="jalsarraf", width=832, height=1216, include_commander=False)
    plain = await agy._validated_payload("Klukai at the onsen", request_id=None, **base)
    granted = await agy._validated_payload(
        "Klukai at the onsen", request_id=None, nudity=True, **base
    )
    assert "nudity" not in plain  # earlier requests keep their exact digest
    assert granted["nudity"] is True


@pytest.mark.asyncio
async def test_the_commander_is_never_part_of_a_granted_request(agy_on):
    with pytest.raises(ValueError):
        await agy._validated_payload(
            "both at the onsen",
            user_id="jalsarraf",
            width=832,
            height=1216,
            include_commander=True,
            request_id=None,
            nudity=True,
        )


@pytest.mark.asyncio
async def test_submit_sends_the_grant_to_the_worker(agy_on, monkeypatch):
    post = AsyncMock(return_value={"status": "completed", "image": b"png"})
    monkeypatch.setattr(agy, "_post_worker", post)
    await agy.submit_agy_image("Klukai at the onsen", user_id="jalsarraf", nudity=True)
    path, payload = post.await_args.args
    assert path == "/generate" and payload["nudity"] is True


@pytest.mark.asyncio
async def test_journal_records_the_grant_and_recovery_resends_it(monkeypatch):
    stored = {}

    async def fake_fetch(request_id):
        return stored.get("row")

    async def fake_insert(
        request_id,
        user_id,
        prompt,
        width,
        height,
        include_commander,
        digest,
        archive_context=None,
    ):
        stored["row"] = {
            "id": request_id,
            "user_id": user_id,
            "prompt": prompt,
            "width": width,
            "height": height,
            "include_commander": include_commander,
            "phase": "accepted",
            "memory_id": None,
            "archive_context": archive_context,
        }

    submit = AsyncMock(return_value={"status": "unknown", "image": None})
    recover = AsyncMock(return_value={"status": "unknown", "image": None})
    monkeypatch.setattr(account_images, "fetch_request", fake_fetch)
    monkeypatch.setattr(account_images, "insert_request", fake_insert)
    monkeypatch.setattr(account_images, "update_request", AsyncMock())
    monkeypatch.setattr(agy, "submit_agy_image", submit)
    monkeypatch.setattr(agy, "recover_agy_image", recover)

    await account_images.fulfill_account_image(
        "jalsarraf",
        "Klukai in the onsen",
        width=832,
        height=1216,
        include_commander=False,
        archive_context={"mood": "tender"},
        nudity=True,
    )
    assert stored["row"]["archive_context"] == {"mood": "tender", "nudity": True}
    assert submit.await_args.kwargs["nudity"] is True

    stored["row"]["phase"] = "accepted"
    await account_images._recover_saved(stored["row"])
    assert recover.await_args.kwargs["nudity"] is True


def test_a_journalled_grant_never_applies_with_the_commander():
    row = {"include_commander": True, "archive_context": {"nudity": True}}
    assert account_images._granted_nudity(row) is False
    assert (
        account_images._granted_nudity(
            {"include_commander": False, "archive_context": {}}
        )
        is False
    )


@pytest.mark.asyncio
async def test_her_own_pictures_are_never_granted(monkeypatch):
    """Her POV, album and mission renders come through generate_image with a
    prompt she composed: "onsen" there is not his words asking, so the worker
    is never told nudity is allowed, even past the intimacy gate."""
    from app import image_gen

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setattr(agy, "uses_agy_images", lambda user_id: True)
    submit = AsyncMock(return_value={"status": "completed", "image": None})
    monkeypatch.setattr(agy, "submit_agy_image", submit)
    await image_gen.generate_image(
        "Klukai alone, naked, relaxing at the onsen", sfw=False, user_id="jalsarraf"
    )
    submit.assert_awaited_once()
    assert submit.await_args.kwargs["nudity"] is False
