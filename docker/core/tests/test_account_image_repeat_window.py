"""Elapsed repeat protection against disposable PostgreSQL, no provider calls.

These tests require the existing explicit migration-test database opt-in.
Only synthetic image journal rows are created; each test owns a random schema.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from unittest.mock import AsyncMock

import pytest

from app import account_images as journal
from app import agy_image_provider as provider
from app import background
from tests.test_account_image_recovery import account_db  # noqa: F401
from tests.test_background_coverage import bg_mocks  # noqa: F401
from tests.test_migrations_postgres import migration_pool  # noqa: F401


def identity(**changes):
    scope = dict(
        user_id="jalsarraf",
        prompt="Take a picture",
        width=832,
        height=1216,
        include_commander=False,
        scene="hangar",
    )
    scope.update(changes)
    return journal.explicit_request_id(**scope)


async def picture(
    *,
    repeat_key=None,
    user_id="jalsarraf",
    prompt="original rendered prompt",
    width=832,
    height=1216,
    include_commander=False,
    **kwargs,
):
    return await journal.fulfill_account_image(
        user_id,
        prompt,
        width=width,
        height=height,
        include_commander=include_commander,
        repeat_key=repeat_key or identity(),
        **kwargs,
    )


@pytest.fixture
def provider_calls(monkeypatch):
    submit = AsyncMock(return_value={"status": "pending", "image": None})
    recover = AsyncMock(return_value={"status": "unknown", "image": None})
    monkeypatch.setattr(provider, "submit_agy_image", submit)
    monkeypatch.setattr(provider, "recover_agy_image", recover)
    return submit, recover


async def stored(pool, request_id):
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT created_at, archive_context, prompt, phase FROM companion_account_image_requests WHERE id = %s",
                (request_id,),
            )
        ).fetchone()


async def test_chat_599_601_boundary_reuses_original_id(
    account_db, bg_mocks, provider_calls, monkeypatch
):
    """A fixed bucket edge must not turn a two-second repeat into a paid job."""
    monkeypatch.setattr("app.image_provider.selected_provider", lambda: "agy")
    monkeypatch.setattr(
        "app.her_day.render_costume", AsyncMock(return_value="range_kit")
    )
    monkeypatch.setattr("app.her_day.render_place", AsyncMock(return_value=None))
    now = [1_800_000_599.0]
    monkeypatch.setattr(journal.time, "time", lambda: now[0])
    prompt = "Draw Klukai standing in a quiet garden"
    await background.background_image_gen(prompt, user_id="alice", recent_turns=[])
    now[0] += 2
    await background.background_image_gen(prompt, user_id="alice", recent_turns=[])
    submit, recover = provider_calls
    assert submit.await_count == 1
    assert recover.await_count == 1
    assert (
        submit.await_args.kwargs["request_id"]
        == recover.await_args.kwargs["request_id"]
    )


async def test_repeat_keeps_original_payload_archive_context_and_age(
    account_db, provider_calls
):
    original = {
        "conversation_id": "first-conversation",
        "mood": "composed",
        "affection_level": 2,
    }
    untouched = copy.deepcopy(original)
    first = await picture(archive_context=original)
    before = await stored(account_db, first["request_id"])
    again = await picture(
        prompt="later outfit and mood",
        archive_context={
            "conversation_id": "later-conversation",
            "mood": "devoted",
            "affection_level": 8,
        },
    )
    assert again["request_id"] == first["request_id"] and again["phase"] == "unknown"
    assert await stored(account_db, first["request_id"]) == before
    assert original == untouched
    assert {key: before[1][key] for key in original} == original
    assert provider_calls[0].await_count == 1
    assert provider_calls[1].await_args.args[0] == "original rendered prompt"


async def test_elapsed_window_expires_without_refreshing_on_reuse(
    account_db, provider_calls
):
    first = await picture()
    async with account_db.connection() as conn:
        await conn.execute(
            "UPDATE companion_account_image_requests SET created_at = NOW() - INTERVAL '590 seconds' WHERE id = %s",
            (first["request_id"],),
        )
    original_age = (await stored(account_db, first["request_id"]))[0]
    repeat = await picture()
    assert repeat["request_id"] == first["request_id"]
    assert (await stored(account_db, first["request_id"]))[0] == original_age
    # Even a pending previous job does not consume the next explicit request forever.
    async with account_db.connection() as conn:
        await conn.execute(
            "UPDATE companion_account_image_requests SET created_at = NOW() - INTERVAL '600 seconds' WHERE id = %s",
            (first["request_id"],),
        )
    later = await picture()
    assert later["request_id"] != first["request_id"]
    assert provider_calls[0].await_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "alice"},
        {"prompt": "Take a different picture"},
        {"width": 1216, "height": 832},
        {"include_commander": True},
        {"scene": "beach"},
    ],
)
async def test_distinct_original_scope_is_not_reused(
    account_db, provider_calls, change
):
    first = await picture()
    options = {
        key: value for key, value in change.items() if key not in {"scene", "prompt"}
    }
    other = await picture(repeat_key=identity(**change), **options)
    assert first["request_id"] != other["request_id"]
    assert provider_calls[0].await_count == 2
    provider_calls[1].assert_not_awaited()


async def test_granted_nudity_is_part_of_the_repeat_identity(
    account_db, provider_calls
):
    first = await picture(nudity=False)
    granted = await picture(nudity=True)
    again = await picture(nudity=True)
    assert first["request_id"] != granted["request_id"] == again["request_id"]
    assert provider_calls[0].await_count == 2
    assert provider_calls[0].await_args_list[0].kwargs["nudity"] is False
    assert provider_calls[0].await_args_list[1].kwargs["nudity"] is True
    assert provider_calls[1].await_args.kwargs["nudity"] is True


async def test_concurrent_repeat_claims_insert_and_submit_once(
    account_db, provider_calls
):
    results = await asyncio.gather(*(picture() for _ in range(12)))
    assert len({result["request_id"] for result in results}) == 1
    assert provider_calls[0].await_count == 1
    assert provider_calls[1].await_count == 11
    async with account_db.connection() as conn:
        assert await (
            await conn.execute("SELECT count(*) FROM companion_account_image_requests")
        ).fetchone() == (1,)


async def test_legacy_pending_row_is_not_guessed_from_similar_prompt(
    account_db, provider_calls
):
    legacy_id = str(uuid.uuid4())
    await journal.insert_request(
        legacy_id,
        "jalsarraf",
        "original rendered prompt",
        832,
        1216,
        False,
        "legacy-digest",
        {"conversation_id": "legacy"},
    )
    result = await picture()
    assert result["request_id"] != legacy_id
    assert (await stored(account_db, legacy_id))[1] == {"conversation_id": "legacy"}
    assert provider_calls[0].await_count == 1
    provider_calls[1].assert_not_awaited()


async def test_uncommitted_repeat_claim_failure_cannot_submit(
    account_db, provider_calls
):
    async with account_db.connection() as conn:
        await conn.execute("""
            CREATE FUNCTION reject_picture() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'fixture refuses insert'; END $$;
            CREATE TRIGGER reject_picture BEFORE INSERT ON companion_account_image_requests
            FOR EACH ROW EXECUTE FUNCTION reject_picture();
        """)
    with pytest.raises(Exception, match="fixture refuses insert"):
        await picture()
    provider_calls[0].assert_not_awaited()
    provider_calls[1].assert_not_awaited()
    async with account_db.connection() as conn:
        assert await (
            await conn.execute("SELECT count(*) FROM companion_account_image_requests")
        ).fetchone() == (0,)


@pytest.mark.parametrize("status", ["failed", "refused", "unavailable"])
async def test_a_failed_picture_can_be_asked_for_again(
    account_db, provider_calls, status
):
    """She says "ask me again". Replaying the failure made that a lie."""
    submit, recover = provider_calls
    submit.return_value = {"status": status, "image": None}
    assert (await picture())["phase"] == status

    await picture()

    assert submit.await_count == 2
    assert recover.await_count == 0


@pytest.mark.parametrize("status", ["pending", "unknown"])
async def test_a_picture_that_may_still_exist_is_not_drawn_twice(
    account_db, provider_calls, status
):
    """Paid work may be running. Only observing it is safe."""
    submit, recover = provider_calls
    submit.return_value = {"status": status, "image": None}
    await picture()

    await picture()

    assert submit.await_count == 1
    assert recover.await_count == 1
