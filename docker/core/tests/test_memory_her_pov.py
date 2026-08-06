"""Tests for Her POV memory portraits."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import memory_her_pov as hp


def test_is_trivial():
    assert hp._is_trivial("ok")
    assert hp._is_trivial("hi")
    assert not hp._is_trivial("Tell me about the last mission with Mechty.")


@pytest.mark.asyncio
async def test_pick_exchange_pairs_user_assistant():
    rows_chrono = [
        ("1", "user", "I brought coffee for once today commander.", "composed", "", None),
        ("2", "assistant", "...I noticed. Don't expect gratitude out loud.", "quietly_pleased", "m", None),
        ("3", "user", "ok", "composed", "", None),
        ("4", "assistant", "Mm.", "composed", "m", None),
        ("5", "user", "Remember when we outran the storm on the bike?", "composed", "", None),
        ("6", "assistant", "I remember holding the throttle. You held on without being told.", "tender", "m", None),
    ]
    desc = list(reversed(rows_chrono))

    class FakeResult:
        async def fetchall(self):
            return desc

    class FakeConn:
        async def execute(self, *a, **k):
            return FakeResult()

    @asynccontextmanager
    async def fake_get_conn():
        yield FakeConn()

    with patch("app.db.get_conn", fake_get_conn):
        result = await hp.pick_exchange("u1")

    assert result is not None
    blob = result["user_content"] + result["assistant_content"]
    assert ("coffee" in blob) or ("storm" in blob)


@pytest.mark.asyncio
async def test_compose_pov_fallback_on_bad_llm():
    exchange = {
        "user_content": "Hello there Commander test long enough",
        "assistant_content": "State your business.",
        "mood": "composed",
    }

    class _Gate:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        with patch("app.llm_json.call_llm", AsyncMock(return_value={})):
            pov = await hp.compose_pov(exchange, affection_level=5)
    assert len(pov["annotation"]) >= 8
    assert len(pov["scene_tags"]) >= 8


@pytest.mark.asyncio
async def test_compose_pov_uses_llm_fields():
    exchange = {
        "user_content": "I waited for you at the hangar.",
        "assistant_content": "You did not have to.",
        "mood": "tender",
    }

    class _Gate:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    llm = {
        "annotation": "He waited. I pretended I arrived first.",
        "scene_tags": "hangar, night, silver hair, looking away",
        "couple": True,
        "mood": "tender",
        "title": "Hangar Wait",
    }
    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        with patch("app.llm_json.call_llm", AsyncMock(return_value=llm)):
            pov = await hp.compose_pov(exchange, affection_level=7)
    assert pov["annotation"].startswith("He waited")
    assert pov["couple"] is True
    assert pov["title"] == "Hangar Wait"


@pytest.mark.asyncio
async def test_start_her_pov_returns_job_id():
    with patch.object(hp, "run_her_pov", new=AsyncMock()):
        with patch("app.context.ws") as mock_ws:
            mock_ws.track_task = MagicMock()
            out = await hp.start_her_pov("claude")
    assert "job_id" in out
    job = await hp.get_job(out["job_id"])
    assert job is not None
    assert job["user_id"] == "claude"


# ─────────────────────────────────────────────────────────────────────────
# Shared plumbing for the run_her_pov pipeline
# ─────────────────────────────────────────────────────────────────────────


def _rows(pairs):
    """Build DESC-ordered companion_messages rows from (user, assistant) pairs."""
    chrono = []
    for i, (u, a) in enumerate(pairs):
        chrono.append((f"u{i}", "user", u, "composed", "", None))
        chrono.append((f"a{i}", "assistant", a, "tender", "venice", None))
    return list(reversed(chrono))


def _patch_conn(rows):
    class FakeResult:
        async def fetchall(self):
            return rows

    class FakeConn:
        async def execute(self, *a, **k):
            return FakeResult()

    @asynccontextmanager
    async def fake_get_conn():
        yield FakeConn()

    return patch("app.db.get_conn", fake_get_conn)


class _Gate:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def clean_jobs():
    """run_her_pov mutates module-level job state — isolate each test."""
    hp._JOBS.clear()
    hp._ACTIVE_JOB_BY_USER.clear()
    yield
    hp._JOBS.clear()
    hp._ACTIVE_JOB_BY_USER.clear()


@pytest.fixture
def pipeline():
    """Patch every collaborator run_her_pov reaches for, with sane defaults.

    Returns the mock bundle so tests can assert on what she actually did.
    """
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_proactive = AsyncMock()
    ws.track_task = MagicMock()

    aff_state = MagicMock()
    aff_state.level = 7
    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=aff_state)

    memory = MagicMock()
    memory.recall_fact = AsyncMock(return_value=None)

    save_image = AsyncMock(return_value="mem-123")
    generate_image = AsyncMock(return_value=b"\x89PNG-bytes")
    build_prompt = MagicMock(return_value="a prompt")
    is_outfit_unlocked = MagicMock(return_value=True)

    pov = {
        "annotation": "I kept this one. Don't ask me why.",
        "scene_tags": "hangar, night, silver hair",
        "couple": True,
        "mood": "tender",
        "title": "Hangar Wait",
    }

    exchange = {
        "user_id_msg": "u0",
        "user_content": "I waited for you at the hangar last night.",
        "assistant_content": "You did not have to wait. I would have found you.",
        "mood": "tender",
        "created_at": "2026-08-06T00:00:00+00:00",
        "score": 400,
    }

    with (
        patch("app.context.ws", ws),
        patch("app.context.affection", affection),
        patch("app.context.memory", memory),
        patch("app.memory_archive.save_image", save_image),
        patch("app.image_gen.generate_image", generate_image),
        patch("app.image_gen.build_prompt", build_prompt),
        patch("app.image_gen.is_outfit_unlocked", is_outfit_unlocked),
        patch.object(hp, "pick_exchange", AsyncMock(return_value=exchange)),
        patch.object(hp, "compose_pov", AsyncMock(return_value=dict(pov))),
        # the pipeline paces its WS delivery; don't pay for it in the suite
        patch.object(hp.asyncio, "sleep", AsyncMock()),
    ):
        yield SimpleNamespace(
            ws=ws,
            affection=affection,
            aff_state=aff_state,
            memory=memory,
            save_image=save_image,
            generate_image=generate_image,
            build_prompt=build_prompt,
            is_outfit_unlocked=is_outfit_unlocked,
            pov=pov,
            exchange=exchange,
        )


def _ws_frames(ws, frame_type="her_pov"):
    return [
        c.args[1]
        for c in ws.send.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], dict)
        and c.args[1].get("type") == frame_type
    ]


# ─────────────────────────────────────────────────────────────────────────
# run_her_pov — happy path
# ─────────────────────────────────────────────────────────────────────────


class TestRunHerPovHappyPath:
    @pytest.mark.asyncio
    async def test_completes_and_saves_memory(self, clean_jobs, pipeline):
        await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "done"
        assert job["phase"] == "done"
        assert job["memory_id"] == "mem-123"
        assert job["title"] == "Hangar Wait"
        assert job["has_image"] is True
        pipeline.save_image.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_walks_every_phase_in_order(self, clean_jobs, pipeline):
        await hp.run_her_pov("claude", "job-1")

        phases = [f.get("phase") for f in _ws_frames(pipeline.ws)]
        assert phases == ["searching", "thinking", "drawing", "done"]

    @pytest.mark.asyncio
    async def test_image_is_delivered_over_ws(self, clean_jobs, pipeline):
        await hp.run_her_pov("claude", "job-1")

        images = _ws_frames(pipeline.ws, "image")
        assert len(images) == 1
        assert images[0]["memory_id"] == "mem-123"
        # base64 of the generated bytes, not the raw bytes
        assert isinstance(images[0]["data"], str)
        assert base64.b64decode(images[0]["data"]) == b"\x89PNG-bytes"

    @pytest.mark.asyncio
    async def test_she_speaks_her_journal_line(self, clean_jobs, pipeline):
        await hp.run_her_pov("claude", "job-1")

        said = [c.args[1] for c in pipeline.ws.send_proactive.call_args_list]
        assert pipeline.pov["annotation"] in said

    @pytest.mark.asyncio
    async def test_saves_with_her_pov_tags(self, clean_jobs, pipeline):
        await hp.run_her_pov("claude", "job-1")

        curation = pipeline.save_image.await_args.kwargs["curation"]
        assert curation["keep"] is True
        assert curation["image_tags"] == ["her_pov", "from_her_side", "commander_request"]
        assert curation["annotation"] == pipeline.pov["annotation"]

    @pytest.mark.asyncio
    async def test_saves_under_the_requesting_user(self, clean_jobs, pipeline):
        await hp.run_her_pov("claude", "job-1")

        assert pipeline.save_image.await_args.kwargs["user_id"] == "claude"
        assert pipeline.save_image.await_args.kwargs["conversation_id"] == "her_pov:job-1"

    @pytest.mark.asyncio
    async def test_releases_the_active_user_slot(self, clean_jobs, pipeline):
        hp._ACTIVE_JOB_BY_USER["claude"] = "job-1"
        await hp.run_her_pov("claude", "job-1")
        assert "claude" not in hp._ACTIVE_JOB_BY_USER


# ─────────────────────────────────────────────────────────────────────────
# run_her_pov — affection-driven behaviour
# ─────────────────────────────────────────────────────────────────────────


class TestRunHerPovAffection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "level,category",
        [
            (9, "Precious Memories"),
            (6, "Precious Memories"),
            (5, "The Commander"),
            (3, "The Commander"),
            (2, "Quiet Hours"),
            (0, "Quiet Hours"),
        ],
    )
    async def test_category_follows_affection(self, clean_jobs, pipeline, level, category):
        pipeline.aff_state.level = level
        await hp.run_her_pov("claude", "job-1")

        assert pipeline.save_image.await_args.kwargs["curation"]["category"] == category

    @pytest.mark.asyncio
    async def test_affection_level_threads_into_the_render(self, clean_jobs, pipeline):
        """Regression: this used `app.affection` (module) instead of the
        manager singleton and raised AttributeError in production."""
        pipeline.aff_state.level = 4
        await hp.run_her_pov("claude", "job-1")

        pipeline.affection.get_state.assert_awaited_once_with("claude")
        assert pipeline.build_prompt.call_args.kwargs["affection_level"] == 4
        assert pipeline.save_image.await_args.kwargs["affection_level"] == 4


# ─────────────────────────────────────────────────────────────────────────
# run_her_pov — costume gating
# ─────────────────────────────────────────────────────────────────────────


class TestRunHerPovCostume:
    @pytest.mark.asyncio
    async def test_unlocked_costume_is_used(self, clean_jobs, pipeline):
        pipeline.memory.recall_fact = AsyncMock(return_value="winter coat")
        pipeline.is_outfit_unlocked.return_value = True

        await hp.run_her_pov("claude", "job-1")

        assert pipeline.build_prompt.call_args.kwargs["costume"] == "winter coat"

    @pytest.mark.asyncio
    async def test_locked_costume_is_dropped(self, clean_jobs, pipeline):
        pipeline.memory.recall_fact = AsyncMock(return_value="locked outfit")
        pipeline.is_outfit_unlocked.return_value = False

        await hp.run_her_pov("claude", "job-1")

        assert pipeline.build_prompt.call_args.kwargs["costume"] is None

    @pytest.mark.asyncio
    async def test_no_remembered_costume(self, clean_jobs, pipeline):
        pipeline.memory.recall_fact = AsyncMock(return_value=None)

        await hp.run_her_pov("claude", "job-1")

        assert pipeline.build_prompt.call_args.kwargs["costume"] is None


# ─────────────────────────────────────────────────────────────────────────
# run_her_pov — time of day
# ─────────────────────────────────────────────────────────────────────────


class TestRunHerPovTimeOfDay:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hour,tod",
        [
            (5, "morning"), (9, "morning"), (11, "morning"),
            (12, "afternoon"), (16, "afternoon"),
            (17, "evening"), (20, "evening"),
            (21, "night"), (3, "night"), (0, "night"),
        ],
    )
    async def test_time_of_day_buckets(self, clean_jobs, pipeline, hour, tod):
        """Buckets follow the Commander's wall clock, not the container's UTC."""
        with patch("app.proactive.state.now_local",
                   return_value=datetime(2026, 8, 6, hour)):
            await hp.run_her_pov("claude", "job-1")

        assert pipeline.build_prompt.call_args.kwargs["time_of_day"] == tod

    @pytest.mark.asyncio
    async def test_uses_local_clock_not_utc(self, clean_jobs, pipeline):
        """Regression: `datetime.now()` in a UTC container baked 1am lighting
        into a portrait the Commander asked for at 19:00 local."""
        with patch("app.proactive.state.now_local",
                   return_value=datetime(2026, 8, 6, 19, 0)):
            with patch.object(hp, "datetime") as naive_utc:
                naive_utc.now.return_value = datetime(2026, 8, 7, 0, 0)
                await hp.run_her_pov("claude", "job-1")

        assert pipeline.build_prompt.call_args.kwargs["time_of_day"] == "evening"


# ─────────────────────────────────────────────────────────────────────────
# run_her_pov — failure paths
# ─────────────────────────────────────────────────────────────────────────


class TestRunHerPovFailures:
    @pytest.mark.asyncio
    async def test_thin_history_fails_softly(self, clean_jobs, pipeline):
        with patch.object(hp, "pick_exchange", AsyncMock(return_value=None)):
            await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "failed"
        assert job["error"] == "no_history"
        pipeline.generate_image.assert_not_awaited()
        pipeline.save_image.assert_not_awaited()
        # she explains herself rather than going silent
        assert pipeline.ws.send_proactive.await_count == 1

    @pytest.mark.asyncio
    async def test_image_failure_keeps_the_journal_line(self, clean_jobs, pipeline):
        pipeline.generate_image.return_value = None

        await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "failed"
        assert job["error"] == "image_failed"
        # the words she wrote survive even though the picture did not
        assert job["annotation"] == pipeline.pov["annotation"]
        assert job["title"] == pipeline.pov["title"]
        pipeline.save_image.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unexpected_error_marks_job_failed(self, clean_jobs, pipeline):
        pipeline.save_image.side_effect = RuntimeError("archive volume full")

        await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "failed"
        assert job["error"] == "internal"
        assert "archive volume full" in job["message"]

    @pytest.mark.asyncio
    async def test_active_slot_released_even_on_crash(self, clean_jobs, pipeline):
        pipeline.save_image.side_effect = RuntimeError("boom")
        hp._ACTIVE_JOB_BY_USER["claude"] = "job-1"

        await hp.run_her_pov("claude", "job-1")

        assert "claude" not in hp._ACTIVE_JOB_BY_USER

    @pytest.mark.asyncio
    async def test_dead_socket_does_not_break_the_job(self, clean_jobs, pipeline):
        """A disconnected client must not cost her the memory."""
        pipeline.ws.send.side_effect = RuntimeError("socket closed")
        pipeline.ws.send_thinking.side_effect = RuntimeError("socket closed")
        pipeline.ws.send_proactive.side_effect = RuntimeError("socket closed")

        await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "done"
        assert job["memory_id"] == "mem-123"

    @pytest.mark.asyncio
    async def test_dead_socket_on_no_history_still_records_failure(
        self, clean_jobs, pipeline
    ):
        pipeline.ws.send.side_effect = RuntimeError("socket closed")
        pipeline.ws.send_proactive.side_effect = RuntimeError("socket closed")
        with patch.object(hp, "pick_exchange", AsyncMock(return_value=None)):
            await hp.run_her_pov("claude", "job-1")

        assert (await hp.get_job("job-1"))["error"] == "no_history"

    @pytest.mark.asyncio
    async def test_archive_rejection_is_not_reported_as_kept(
        self, clean_jobs, pipeline
    ):
        """save_image returns None when the archive dedupes the annotation.

        Regression: the job used to say "Kept." with memory_id=None, so the
        client showed a success panel over an empty stage.
        """
        pipeline.save_image.return_value = None

        await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "failed"
        assert job["error"] == "not_saved"
        assert job.get("memory_id") is None
        assert job.get("has_image") is not True
        assert _ws_frames(pipeline.ws, "image") == []

    @pytest.mark.asyncio
    async def test_dead_socket_on_archive_rejection_still_records_failure(
        self, clean_jobs, pipeline
    ):
        pipeline.save_image.return_value = None
        pipeline.ws.send.side_effect = RuntimeError("socket closed")

        await hp.run_her_pov("claude", "job-1")

        assert (await hp.get_job("job-1"))["error"] == "not_saved"

    @pytest.mark.asyncio
    async def test_dead_socket_on_internal_error_still_records_failure(
        self, clean_jobs, pipeline
    ):
        pipeline.save_image.side_effect = RuntimeError("archive volume full")
        pipeline.ws.send.side_effect = RuntimeError("socket closed")

        await hp.run_her_pov("claude", "job-1")

        assert (await hp.get_job("job-1"))["error"] == "internal"

    @pytest.mark.asyncio
    async def test_cancellation_lands_the_job_in_a_terminal_state(
        self, clean_jobs, pipeline
    ):
        """The socket dropping cancels the task; the job must not wedge.

        CancelledError is a BaseException, so the generic handler never saw it
        and the job sat at 'drawing' forever while the client polled it.
        """
        pipeline.generate_image.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await hp.run_her_pov("claude", "job-1")

        job = await hp.get_job("job-1")
        assert job["status"] == "failed"
        assert job["error"] == "cancelled"
        assert "claude" not in hp._ACTIVE_JOB_BY_USER

    @pytest.mark.asyncio
    async def test_dead_socket_on_image_failure_still_records_failure(
        self, clean_jobs, pipeline
    ):
        pipeline.generate_image.return_value = None
        pipeline.ws.send.side_effect = RuntimeError("socket closed")
        pipeline.ws.send_proactive.side_effect = RuntimeError("socket closed")

        await hp.run_her_pov("claude", "job-1")

        assert (await hp.get_job("job-1"))["error"] == "image_failed"


# ─────────────────────────────────────────────────────────────────────────
# start_her_pov concurrency
# ─────────────────────────────────────────────────────────────────────────


class TestStartHerPov:
    @pytest.mark.asyncio
    async def test_second_request_reuses_the_running_job(self, clean_jobs):
        with patch.object(hp, "run_her_pov", new=AsyncMock()):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock()
                first = await hp.start_her_pov("claude")
                second = await hp.start_her_pov("claude")

        assert first["reused"] is False
        assert second["reused"] is True
        assert second["job_id"] == first["job_id"]

    @pytest.mark.asyncio
    async def test_finished_job_does_not_block_a_new_one(self, clean_jobs):
        with patch.object(hp, "run_her_pov", new=AsyncMock()):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock()
                first = await hp.start_her_pov("claude")
                await hp._set_job(first["job_id"], status="done")
                hp._ACTIVE_JOB_BY_USER.pop("claude", None)
                second = await hp.start_her_pov("claude")

        assert second["job_id"] != first["job_id"]
        assert second["reused"] is False

    @pytest.mark.asyncio
    async def test_separate_users_get_separate_jobs(self, clean_jobs):
        with patch.object(hp, "run_her_pov", new=AsyncMock()):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock()
                a = await hp.start_her_pov("claude")
                b = await hp.start_her_pov("ricky")

        assert a["job_id"] != b["job_id"]
        assert (await hp.get_job(a["job_id"]))["user_id"] == "claude"
        assert (await hp.get_job(b["job_id"]))["user_id"] == "ricky"

    @pytest.mark.asyncio
    async def test_double_tap_never_starts_a_second_render(self, clean_jobs):
        """Regression: the claim used to be validated by scanning _JOBS for a
        non-terminal job, which raced with the WS delivery tail and let a
        second full pipeline (LLM + GPU render) start."""
        started = []

        async def _slow(user_id, job_id):
            started.append(job_id)
            await asyncio.sleep(0)

        with patch.object(hp, "run_her_pov", _slow):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock()
                outs = await asyncio.gather(
                    *(hp.start_her_pov("claude") for _ in range(8))
                )
        await asyncio.sleep(0)

        assert len({o["job_id"] for o in outs}) == 1
        assert sum(1 for o in outs if not o["reused"]) == 1
        assert len(started) == 1

    @pytest.mark.asyncio
    async def test_reuse_survives_a_pruned_job_record(self, clean_jobs):
        """The claim, not the job row, is authoritative for dedupe."""
        with patch.object(hp, "run_her_pov", new=AsyncMock()):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock()
                first = await hp.start_her_pov("claude")
                hp._JOBS.clear()  # simulate eviction
                second = await hp.start_her_pov("claude")

        assert second["reused"] is True
        assert second["job_id"] == first["job_id"]

    @pytest.mark.asyncio
    async def test_failed_scheduling_releases_the_claim(self, clean_jobs):
        """A stranded claim would lock the user out of the feature until
        the worker restarted."""
        with patch.object(hp.asyncio, "create_task",
                          side_effect=RuntimeError("no running loop")):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock()
                with pytest.raises(RuntimeError):
                    await hp.start_her_pov("claude")

        assert "claude" not in hp._ACTIVE_JOB_BY_USER

    @pytest.mark.asyncio
    async def test_untrackable_socket_does_not_prevent_start(self, clean_jobs):
        with patch.object(hp, "run_her_pov", new=AsyncMock()):
            with patch("app.context.ws") as mock_ws:
                mock_ws.track_task = MagicMock(side_effect=RuntimeError("no socket"))
                out = await hp.start_her_pov("claude")

        assert out["status"] == "queued"


# ─────────────────────────────────────────────────────────────────────────
# get_job / _set_job
# ─────────────────────────────────────────────────────────────────────────


class TestJobBoard:
    @pytest.mark.asyncio
    async def test_unknown_job_is_none(self, clean_jobs):
        assert await hp.get_job("nope") is None

    @pytest.mark.asyncio
    async def test_get_job_returns_a_copy(self, clean_jobs):
        await hp._set_job("j", status="queued")
        snapshot = await hp.get_job("j")
        snapshot["status"] = "tampered"
        assert (await hp.get_job("j"))["status"] == "queued"

    @pytest.mark.asyncio
    async def test_set_job_stamps_updated_at(self, clean_jobs):
        await hp._set_job("j", status="queued")
        assert "updated_at" in await hp.get_job("j")

    @pytest.mark.asyncio
    async def test_internal_bookkeeping_is_not_exposed(self, clean_jobs):
        await hp._set_job("j", status="queued")
        assert not [k for k in await hp.get_job("j") if k.startswith("_")]

    @pytest.mark.asyncio
    async def test_stale_jobs_are_evicted(self, clean_jobs):
        """The board is in-process and never persisted — it must not grow
        for the lifetime of the worker."""
        await hp._set_job("old", status="done")
        hp._JOBS["old"]["_touched"] -= hp._JOB_TTL_SECONDS + 1

        await hp._set_job("new", status="queued")

        assert await hp.get_job("old") is None
        assert await hp.get_job("new") is not None

    @pytest.mark.asyncio
    async def test_fresh_jobs_survive_pruning(self, clean_jobs):
        await hp._set_job("a", status="done")
        await hp._set_job("b", status="queued")
        assert await hp.get_job("a") is not None
        assert await hp.get_job("b") is not None

    @pytest.mark.asyncio
    async def test_board_respects_a_hard_ceiling(self, clean_jobs):
        for i in range(hp._JOB_MAX + 25):
            await hp._set_job(f"j{i}", status="done")
        assert len(hp._JOBS) <= hp._JOB_MAX
        # newest survive
        assert await hp.get_job(f"j{hp._JOB_MAX + 24}") is not None


# ─────────────────────────────────────────────────────────────────────────
# pick_exchange edge cases
# ─────────────────────────────────────────────────────────────────────────


class TestPickExchange:
    @pytest.mark.asyncio
    async def test_empty_history_returns_none(self):
        with _patch_conn([]):
            assert await hp.pick_exchange("u1") is None

    @pytest.mark.asyncio
    async def test_all_trivial_returns_none(self):
        with _patch_conn(_rows([("ok", "Mm."), ("hi", "Hey.")])):
            assert await hp.pick_exchange("u1") is None

    @pytest.mark.asyncio
    async def test_unanswered_user_message_is_not_a_candidate(self):
        rows = list(reversed([("u0", "user", "Tell me about the storm run.",
                               "composed", "", None)]))
        with _patch_conn(rows):
            assert await hp.pick_exchange("u1") is None

    @pytest.mark.asyncio
    async def test_assistant_without_preceding_user_is_skipped(self):
        rows = list(reversed([
            ("a0", "assistant", "I was thinking about the hangar again.",
             "tender", "venice", None),
        ]))
        with _patch_conn(rows):
            assert await hp.pick_exchange("u1") is None

    @pytest.mark.asyncio
    async def test_empty_assistant_reply_is_skipped(self):
        rows = list(reversed([
            ("u0", "user", "Do you remember the storm run?", "composed", "", None),
            ("a0", "assistant", "   ", "tender", "venice", None),
        ]))
        with _patch_conn(rows):
            assert await hp.pick_exchange("u1") is None

    @pytest.mark.asyncio
    async def test_trivial_user_with_substantive_reply_is_kept(self):
        """Only *both* sides being trivial disqualifies a moment."""
        rows = list(reversed([
            ("u0", "user", "ok", "composed", "", None),
            ("a0", "assistant",
             "You say that, but you stayed on comms the whole night anyway.",
             "tender", "venice", None),
        ]))
        with _patch_conn(rows):
            picked = await hp.pick_exchange("u1")
        assert picked is not None
        assert picked["assistant_content"].startswith("You say that")

    @pytest.mark.asyncio
    async def test_long_content_is_truncated(self):
        rows = list(reversed([
            ("u0", "user", "C" * 2000, "composed", "", None),
            ("a0", "assistant", "K" * 3000, "tender", "venice", None),
        ]))
        with _patch_conn(rows):
            picked = await hp.pick_exchange("u1")
        assert len(picked["user_content"]) == 800
        assert len(picked["assistant_content"]) == 1200

    @pytest.mark.asyncio
    async def test_created_at_is_serialised(self):
        when = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        rows = list(reversed([
            ("u0", "user", "Do you remember the storm run?", "composed", "", when),
            ("a0", "assistant", "I remember holding the throttle.",
             "tender", "venice", when),
        ]))
        with _patch_conn(rows):
            picked = await hp.pick_exchange("u1")
        assert picked["created_at"] == when.isoformat()

    @pytest.mark.asyncio
    async def test_missing_mood_defaults_to_composed(self):
        rows = list(reversed([
            ("u0", "user", "Do you remember the storm run?", None, "", None),
            ("a0", "assistant", "I remember holding the throttle.", None, "venice", None),
        ]))
        with _patch_conn(rows):
            picked = await hp.pick_exchange("u1")
        assert picked["mood"] == "composed"

    @pytest.mark.asyncio
    async def test_longer_commander_lines_score_higher(self):
        short_u, long_u = "Did you sleep?" + "." * 10, "C" * 200
        rows = _rows([(short_u, "A" * 100), (long_u, "A" * 100)])
        with _patch_conn(rows):
            # deterministic: take the top-scored candidate
            with patch("app.memory_her_pov.random.choices",
                       side_effect=lambda pool, weights, k: [pool[0]]):
                picked = await hp.pick_exchange("u1")
        assert picked["user_content"] == long_u


# ─────────────────────────────────────────────────────────────────────────
# compose_pov edge cases
# ─────────────────────────────────────────────────────────────────────────


class TestComposePov:
    @staticmethod
    def _exchange():
        return {
            "user_content": "I waited for you at the hangar.",
            "assistant_content": "You did not have to.",
            "mood": "wistful",
        }

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back(self):
        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm",
                       AsyncMock(side_effect=RuntimeError("gateway down"))):
                pov = await hp.compose_pov(self._exchange(), affection_level=5)
        assert len(pov["annotation"]) >= 8
        assert pov["title"] == "From my side"
        assert pov["couple"] is False

    @pytest.mark.asyncio
    async def test_non_dict_llm_response_falls_back(self):
        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm",
                       AsyncMock(return_value=["not", "a", "dict"])):
                pov = await hp.compose_pov(self._exchange(), affection_level=5)
        assert pov["title"] == "From my side"

    @pytest.mark.asyncio
    async def test_too_short_fields_fall_back(self):
        bad = {"annotation": "hm", "scene_tags": "x", "couple": "yes", "title": "  "}
        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm", AsyncMock(return_value=bad)):
                pov = await hp.compose_pov(self._exchange(), affection_level=5)
        assert pov["annotation"] != "hm"
        assert pov["scene_tags"] != "x"
        # a non-bool `couple` must not be trusted as truthy
        assert pov["couple"] is False
        assert pov["title"] == "From my side"

    @pytest.mark.asyncio
    async def test_mood_falls_back_to_the_exchange_mood(self):
        raw = {
            "annotation": "He waited. I pretended I arrived first.",
            "scene_tags": "hangar, night, silver hair",
            "couple": False,
            "title": "Hangar Wait",
        }
        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm", AsyncMock(return_value=raw)):
                pov = await hp.compose_pov(self._exchange(), affection_level=5)
        assert pov["mood"] == "wistful"

    @pytest.mark.asyncio
    async def test_title_is_collapsed_and_capped(self):
        raw = {
            "annotation": "He waited. I pretended I arrived first.",
            "scene_tags": "hangar, night, silver hair",
            "couple": False,
            "title": "  A   ridiculously\n\tlong " + "title " * 20,
        }
        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm", AsyncMock(return_value=raw)):
                pov = await hp.compose_pov(self._exchange(), affection_level=5)
        assert len(pov["title"]) <= 48
        assert "\n" not in pov["title"]
        assert "  " not in pov["title"]

    @pytest.mark.asyncio
    async def test_overlong_annotation_and_tags_are_capped(self):
        raw = {
            "annotation": "A" * 900,
            "scene_tags": "tag, " * 400,
            "couple": True,
            "mood": "M" * 90,
            "title": "Long Night",
        }
        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm", AsyncMock(return_value=raw)):
                pov = await hp.compose_pov(self._exchange(), affection_level=9)
        assert len(pov["annotation"]) <= 500
        assert len(pov["scene_tags"]) <= 600
        assert len(pov["mood"]) <= 40

    @pytest.mark.asyncio
    async def test_affection_level_reaches_the_prompt(self):
        captured = {}

        async def _capture(url, model, prompt, **kw):
            captured["prompt"] = prompt
            return {}

        with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
            with patch("app.llm_json.call_llm", _capture):
                await hp.compose_pov(self._exchange(), affection_level=8)
        assert "8/9" in captured["prompt"]
        assert "I waited for you at the hangar." in captured["prompt"]
