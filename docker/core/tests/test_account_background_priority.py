"""Background account pictures (portraits, album seeding) yield to his own.

The account worker has one slot. A background job holds a marker on the
images volume while it renders, and every /generate call this process has in
flight is counted. An explicit picture or Her POV that finds the worker busy
because of either waits for it to clear and is then submitted once more,
instead of failing. With neither, a busy answer is returned exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import account_images as journal
from app import agy_image_provider as provider
from tests.test_account_image_retry_protocol import fulfill, journal_rows  # noqa: F401


@pytest.fixture(autouse=True)
def _images(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "BACKGROUND_WAIT_POLL_S", 0.01)
    return tmp_path


def _marker(tmp_path):
    return tmp_path / ".creative" / "background-render.json"


class TestMarker:
    async def test_marker_is_fresh_only_inside_the_block(self, tmp_path):
        assert journal.background_render_active() is False
        async with journal.background_render("portrait"):
            assert journal.background_render_active() is True
            data = json.loads(_marker(tmp_path).read_text())
            assert data["kind"] == "portrait"
            assert data["until"] > time.time()
        assert journal.background_render_active() is False
        assert not _marker(tmp_path).exists()

    async def test_marker_is_private(self, tmp_path):
        async with journal.background_render("album_seed"):
            mode = _marker(tmp_path).stat().st_mode & 0o777
        assert mode == 0o600

    def test_an_expired_or_garbled_marker_is_not_active(self, tmp_path):
        path = _marker(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"kind": "portrait", "until": time.time() - 1}))
        assert journal.background_render_active() is False
        path.write_text("{not json")
        assert journal.background_render_active() is False
        path.write_text(json.dumps(["until"]))
        assert journal.background_render_active() is False
        path.write_text(json.dumps({"until": "soon"}))
        assert journal.background_render_active() is False
        path.write_text(json.dumps({"until": True}))
        assert journal.background_render_active() is False

    async def test_another_jobs_marker_is_left_in_place(self, tmp_path):
        async with journal.background_render("portrait"):
            # The album seeder (its own process) took over the marker.
            _marker(tmp_path).write_text(
                json.dumps(
                    {"id": "other", "kind": "album_seed", "until": time.time() + 60}
                )
            )
        assert journal.background_render_active() is True

    async def test_an_unwritable_volume_never_blocks_the_job(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "file"))
        (tmp_path / "file").write_text("not a directory")
        async with journal.background_render("portrait"):
            assert journal.background_render_active() is False

    async def test_the_block_marks_its_own_task_as_background(self):
        assert journal.in_background_render() is False
        async with journal.background_render("portrait"):
            assert journal.in_background_render() is True
        assert journal.in_background_render() is False


class TestInFlightPicture:
    """A picture already rendering in this process holds the one slot too."""

    async def test_submit_counts_itself_in_flight_and_always_clears(self, monkeypatch):
        seen = []

        async def post(path, payload):
            seen.append(provider.generation_in_flight())
            raise OSError("worker gone")

        monkeypatch.setattr(provider, "_post_worker", post)
        monkeypatch.setattr(provider, "uses_agy_images", lambda user_id: True)
        result = await provider.submit_agy_image("a picture", user_id="u")
        assert result["status"] == "unknown"
        assert seen == [True]
        assert provider.generation_in_flight() is False

    async def test_a_refused_payload_is_never_counted(self, monkeypatch):
        monkeypatch.setattr(provider, "uses_agy_images", lambda user_id: False)
        post = AsyncMock()
        monkeypatch.setattr(provider, "_post_worker", post)
        result = await provider.submit_agy_image("a picture", user_id="u")
        assert result["status"] == "refused"
        post.assert_not_awaited()
        assert provider.generation_in_flight() is False

    async def test_a_picture_waits_for_one_in_flight_then_goes(self, monkeypatch):
        monkeypatch.setattr(provider, "_generations_in_flight", 1)

        async def finish():
            await asyncio.sleep(0.05)
            monkeypatch.setattr(provider, "_generations_in_flight", 0)

        clear = asyncio.create_task(finish())
        assert await journal.wait_for_background_render() is True
        await clear
        assert provider.generation_in_flight() is False

    async def test_a_stuck_in_flight_picture_bounds_the_wait(self, monkeypatch):
        monkeypatch.setattr(provider, "_generations_in_flight", 1)
        monkeypatch.setattr(journal, "background_wait_limit", lambda: 0.05)
        assert await journal.wait_for_background_render() is True

    async def test_nothing_in_flight_does_not_wait(self):
        assert await journal.wait_for_background_render() is False


class TestForegroundYield:
    async def test_busy_without_a_background_job_returns_at_once(
        self, journal_rows, monkeypatch
    ):
        submit = AsyncMock(return_value={"status": "busy", "image": None})
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        result = await fulfill(request_id=str(uuid.uuid4()))
        assert result["phase"] == "busy"
        submit.assert_awaited_once()

    async def test_his_picture_waits_for_a_background_render_then_goes(
        self, journal_rows, monkeypatch, tmp_path
    ):
        submit = AsyncMock(
            side_effect=[
                {"status": "busy", "image": None},
                {"status": "completed", "image": b"his picture"},
            ]
        )
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        path = _marker(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"id": "bg", "kind": "portrait", "until": time.time() + 60})
        )

        async def finish_background():
            await asyncio.sleep(0.05)
            path.unlink()

        clear = asyncio.create_task(finish_background())
        result = await fulfill("his words", request_id=str(uuid.uuid4()))
        await clear
        assert result["image"] == b"his picture"
        assert [c.args[0] for c in submit.await_args_list] == ["his words", "his words"]

    async def test_his_picture_waits_for_one_in_flight_then_goes(
        self, journal_rows, monkeypatch
    ):
        # Her POV (or another of his pictures) already holds the worker's slot.
        submit = AsyncMock(
            side_effect=[
                {"status": "busy", "image": None},
                {"status": "completed", "image": b"his picture"},
            ]
        )
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        monkeypatch.setattr(provider, "_generations_in_flight", 1)

        async def finish():
            await asyncio.sleep(0.05)
            monkeypatch.setattr(provider, "_generations_in_flight", 0)

        clear = asyncio.create_task(finish())
        result = await fulfill("his words", request_id=str(uuid.uuid4()))
        await clear
        assert result["image"] == b"his picture"
        assert [c.args[0] for c in submit.await_args_list] == ["his words", "his words"]

    async def test_a_stale_marker_bounds_the_wait(
        self, journal_rows, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(journal, "background_wait_limit", lambda: 0.05)
        submit = AsyncMock(
            side_effect=[
                {"status": "busy", "image": None},
                {"status": "busy", "image": None},
            ]
        )
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        path = _marker(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"id": "bg", "kind": "portrait", "until": time.time() + 600})
        )
        result = await fulfill(request_id=str(uuid.uuid4()))
        assert result["phase"] == "busy"
        assert submit.await_count == 2  # one bounded retry, never a loop

    async def test_a_background_job_never_waits_for_itself(
        self, journal_rows, monkeypatch
    ):
        submit = AsyncMock(return_value={"status": "busy", "image": None})
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        async with journal.background_render("portrait"):
            result = await fulfill(request_id=str(uuid.uuid4()))
        assert result["phase"] == "busy"
        submit.assert_awaited_once()

    async def test_a_vanished_row_keeps_the_busy_answer(
        self, journal_rows, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(journal, "background_wait_limit", lambda: 0.02)
        monkeypatch.setattr(
            provider,
            "submit_agy_image",
            AsyncMock(return_value={"status": "busy", "image": None}),
        )
        path = _marker(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"id": "bg", "kind": "portrait", "until": time.time() + 60})
        )
        request_id = str(uuid.uuid4())
        original = journal.fetch_request
        calls = {"n": 0}

        async def fetch_then_vanish(rid):
            calls["n"] += 1
            return None if calls["n"] > 1 else await original(rid)

        monkeypatch.setattr(journal, "fetch_request", fetch_then_vanish)
        result = await fulfill(request_id=request_id)
        assert result["phase"] == "busy"

    def test_wait_limit_covers_one_worker_request(self):
        assert journal.background_wait_limit() >= provider.REQUEST_DEADLINE_SECONDS


class TestAllowanceCount:
    def test_day_turns_over_at_local_midnight(self):
        # 03:30 UTC on 2026-10-20 is still 22:30 on the 19th in Chicago.
        now = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)
        start = journal.local_day_start(now)
        assert start.isoformat() == "2026-10-19T00:00:00-05:00"

    async def test_counts_journalled_background_requests_by_kind(self, monkeypatch):
        cursor = MagicMock()
        cursor.fetchone = AsyncMock(return_value=(2,))
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=cursor)

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("app.db.get_conn_autocommit", lambda: _Ctx())
        since = datetime(2026, 10, 19, tzinfo=timezone.utc)
        assert await journal.count_requests_since("portrait", since) == 2
        sql, params = conn.execute.await_args.args
        assert "archive_context->>'kind' = %s" in sql
        assert "created_at >= %s" in sql
        assert params == ("portrait", since)

    async def test_no_row_counts_as_zero(self, monkeypatch):
        cursor = MagicMock()
        cursor.fetchone = AsyncMock(return_value=None)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=cursor)

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("app.db.get_conn_autocommit", lambda: _Ctx())
        assert (
            await journal.count_requests_since("album_seed", datetime.now(timezone.utc))
            == 0
        )


class TestDirectAccountPictures:
    """Her POV and other direct generate_image calls on the worker also wait
    out a background render instead of failing with "busy"."""

    @pytest.fixture
    def agy(self, monkeypatch):
        from app import image_gen

        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
        monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf")
        return image_gen

    async def test_waits_for_the_background_render_then_goes(
        self, agy, monkeypatch, tmp_path
    ):
        submit = AsyncMock(
            side_effect=[
                {"status": "busy", "image": None},
                {"status": "completed", "image": b"her pov"},
            ]
        )
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        path = _marker(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"id": "bg", "kind": "portrait", "until": time.time() + 60})
        )

        async def finish_background():
            await asyncio.sleep(0.05)
            path.unlink()

        clear = asyncio.create_task(finish_background())
        result = await agy.generate_image("silver hair", user_id="jalsarraf", sfw=True)
        await clear
        assert result == b"her pov"
        assert submit.await_count == 2
        first, second = submit.await_args_list
        assert first.args == second.args and first.kwargs == second.kwargs

    async def test_her_pov_waits_for_a_chat_picture_in_flight_then_goes(
        self, agy, monkeypatch
    ):
        # 2026-10-24: a chat picture held the slot and Her POV failed at once with
        # "Visualization failed". It now waits its turn and goes once more.
        submit = AsyncMock(
            side_effect=[
                {"status": "busy", "image": None},
                {"status": "completed", "image": b"her pov"},
            ]
        )
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        monkeypatch.setattr(provider, "_generations_in_flight", 1)

        async def finish_chat_picture():
            await asyncio.sleep(0.05)
            monkeypatch.setattr(provider, "_generations_in_flight", 0)

        clear = asyncio.create_task(finish_chat_picture())
        result = await agy.generate_image("silver hair", user_id="jalsarraf", sfw=True)
        await clear
        assert result == b"her pov"
        assert submit.await_count == 2
        first, second = submit.await_args_list
        assert first.args == second.args and first.kwargs == second.kwargs

    async def test_a_stuck_picture_bounds_her_pov_without_a_third_submit(
        self, agy, monkeypatch
    ):
        monkeypatch.setattr(journal, "background_wait_limit", lambda: 0.05)
        monkeypatch.setattr(provider, "_generations_in_flight", 1)
        submit = AsyncMock(return_value={"status": "busy", "image": None})
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        assert await agy.generate_image("silver hair", user_id="jalsarraf") is None
        assert submit.await_count == 2  # one bounded retry, never a loop

    async def test_a_failed_render_is_not_retried_while_one_is_in_flight(
        self, agy, monkeypatch
    ):
        monkeypatch.setattr(provider, "_generations_in_flight", 1)
        for status in ("failed", "refused", "unknown", "pending"):
            submit = AsyncMock(return_value={"status": status, "image": None})
            monkeypatch.setattr(provider, "submit_agy_image", submit)
            assert await agy.generate_image("silver hair", user_id="jalsarraf") is None
            submit.assert_awaited_once()

    async def test_plain_busy_is_still_a_miss(self, agy, monkeypatch):
        submit = AsyncMock(return_value={"status": "busy", "image": None})
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        assert await agy.generate_image("silver hair", user_id="jalsarraf") is None
        submit.assert_awaited_once()

    async def test_a_background_caller_never_waits(self, agy, monkeypatch, tmp_path):
        submit = AsyncMock(return_value={"status": "busy", "image": None})
        monkeypatch.setattr(provider, "submit_agy_image", submit)
        async with journal.background_render("album_seed"):
            assert await agy.generate_image("silver hair", user_id="jalsarraf") is None
        submit.assert_awaited_once()


async def test_a_marker_that_cannot_be_removed_never_breaks_the_job(tmp_path):
    folder = tmp_path / ".creative"
    async with journal.background_render("portrait"):
        folder.chmod(0o500)  # read-only directory: the unlink fails
    try:
        assert _marker(tmp_path).exists()  # left to expire on its own
    finally:
        folder.chmod(0o700)
