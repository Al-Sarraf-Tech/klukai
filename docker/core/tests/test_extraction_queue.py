"""The turn-extraction queue and the background-call cancel contract.

One extraction runs at a time per user, a preempted job is retried a bounded
number of times (each try reserves a ledger attempt), the backlog is bounded,
and only the turn's own extraction sees ExtractionCancelled.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import background_llm, extraction_llm
from app.chat_followups import ChatFollowups
from app.extraction_llm import ExtractionCancelled


class _WS:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []

    def track_task(self, user_id, task, survive_disconnect=False):
        self.tasks.append(task)


async def _noop(*args, **kwargs):
    return None


def _followups(**changes) -> ChatFollowups:
    values = dict(
        ws=_WS(),
        last_memory_id=lambda user_id: None,
        wants_recall=lambda content: False,
        needs_image=lambda content: False,
        keep_memory=_noop,
        recall=_noop,
        image=_noop,
        extract=_noop,
        compact=_noop,
        save_keywords=(),
        discard_keywords=(),
        trivial_patterns=(),
        compact_threshold=99,
        extraction_idle_s=0.01,
    )
    values.update(changes)
    return ChatFollowups(**values)


async def _drain(followups: ChatFollowups) -> None:
    await asyncio.wait_for(
        asyncio.gather(*followups.ws.tasks, return_exceptions=True), 2
    )


async def test_one_extraction_at_a_time_per_user():
    running = 0
    peak = 0
    order = []

    def job(name):
        async def run():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            order.append(name)
            running -= 1

        return run

    followups = _followups()
    followups._queue_extract("u", job("first"))
    await asyncio.sleep(0.02)  # first is running now
    followups._queue_extract("u", job("second"))
    followups._queue_extract("u", job("third"))
    await _drain(followups)
    assert order == ["first", "second", "third"]
    assert peak == 1
    assert len(followups.ws.tasks) == 1  # one runner, not one per turn
    assert followups._extract_runner == {}
    assert followups._extract_queue == {}


async def test_users_do_not_wait_for_each_other():
    started = []

    def job(name):
        async def run():
            started.append(name)
            await asyncio.sleep(0.05)

        return run

    followups = _followups()
    followups._queue_extract("a", job("a"))
    followups._queue_extract("b", job("b"))
    await asyncio.sleep(0.03)
    assert sorted(started) == ["a", "b"]
    await _drain(followups)


async def test_a_job_preempted_every_time_stops_after_the_cap(caplog):
    tries = 0

    async def always_preempted():
        nonlocal tries
        tries += 1
        raise ExtractionCancelled("cancelled")

    followups = _followups(extraction_max_tries=3)
    with caplog.at_level(logging.WARNING, logger="app.chat_followups"):
        followups._queue_extract("u", always_preempted)
        await _drain(followups)
    assert tries == 3
    assert any("preempted 3 times" in r.getMessage() for r in caplog.records)


async def test_a_preempted_job_runs_before_newer_turns():
    order = []
    first_tries = 0

    async def first():
        nonlocal first_tries
        first_tries += 1
        if first_tries == 1:
            raise ExtractionCancelled("cancelled")
        order.append("first")

    async def second():
        order.append("second")

    followups = _followups()
    followups._queue_extract("u", first)
    followups._queue_extract("u", second)
    await _drain(followups)
    assert order == ["first", "second"]


async def test_the_backlog_drops_the_oldest_waiting_turn(caplog):
    seen = []

    def job(name):
        async def run():
            seen.append(name)

        return run

    followups = _followups(extraction_max_pending=2, extraction_idle_s=0.05)
    with caplog.at_level(logging.WARNING, logger="app.chat_followups"):
        for name in ("one", "two", "three"):
            followups._queue_extract("u", job(name))
        await _drain(followups)
    assert seen == ["two", "three"]
    assert any("backlog full" in r.getMessage() for r in caplog.records)


async def test_a_new_turn_restarts_the_quiet_gap():
    ran = asyncio.Event()

    async def job():
        ran.set()

    followups = _followups(extraction_idle_s=0.08)
    followups._queue_extract("u", job)
    await asyncio.sleep(0.05)
    followups._queue_extract("u", _noop)  # he spoke again before the gap ended
    await asyncio.sleep(0.05)
    assert not ran.is_set()
    await _drain(followups)
    assert ran.is_set()


@pytest.fixture
def preempted(monkeypatch):
    async def complete(prompt, **kwargs):
        raise extraction_llm.ExtractionCancelled("cancelled")

    monkeypatch.setattr(extraction_llm, "complete", complete)


async def test_other_background_callers_get_their_default_when_preempted(preempted):
    report = extraction_llm.CallReport()
    assert await background_llm.text("dream", "p", max_tokens=10, report=report) == ""
    assert report.outcome == "cancelled->defaults"
    assert await background_llm.json_object("her_pov", "p", max_tokens=10) is None


async def test_the_turn_extraction_opts_in_to_the_requeue(preempted):
    with pytest.raises(ExtractionCancelled):
        await background_llm.json_object("extraction", "p", max_tokens=10, requeue=True)
    from app.fact_extractor import extract_facts

    with pytest.raises(ExtractionCancelled):
        await extract_facts("I will fix the door tomorrow", "I heard you.")
