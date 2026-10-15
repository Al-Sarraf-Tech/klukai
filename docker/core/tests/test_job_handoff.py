"""Durable acceptance and lifecycle tests, with no external providers."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import memory_her_pov as hp


@pytest.fixture(autouse=True)
def queue_mode(monkeypatch):
    monkeypatch.setenv('HER_POV_EXECUTION', 'queue')
    hp._JOBS.clear()
    hp._ACTIVE_JOB_BY_USER.clear()
    yield
    hp._JOBS.clear()
    hp._ACTIVE_JOB_BY_USER.clear()


@pytest.mark.parametrize('cloud', [False, True])
async def test_queue_acceptance_requires_durable_insert_even_without_cloud(cloud):
    with patch.object(hp, '_cloud_generation_enabled', return_value=cloud), \
         patch('app.db.get_pool', side_effect=RuntimeError('DB unavailable')), \
         patch.object(hp, '_start_inline', AsyncMock()) as inline:
        with pytest.raises(RuntimeError, match='durable'):
            await hp.start_her_pov('test-user')
    assert not hp._ACTIVE_JOB_BY_USER
    assert not hp._JOBS
    inline.assert_not_awaited()


@pytest.mark.parametrize('cloud', [False, True])
async def test_missing_subscriber_keeps_durable_job_queued_without_inline(cloud):
    with patch.object(hp, '_cloud_generation_enabled', return_value=cloud), \
         patch.object(hp, '_claim_active_from_db', AsyncMock(return_value=None)), \
         patch.object(hp, '_persist_job', AsyncMock()), \
         patch('app.events.publish', AsyncMock(return_value=0)), \
         patch.object(hp, '_start_inline', AsyncMock()) as inline:
        out = await hp.start_her_pov('test-user')
    assert out['status'] == 'queued'
    assert hp._JOBS[out['job_id']]['status'] == 'queued'
    inline.assert_not_awaited()


async def test_reconciler_runs_immediately_retries_errors_and_cancels_on_exit(monkeypatch):
    from app import job_handoff
    calls = []
    retried = asyncio.Event()

    async def reconcile():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('temporary DB outage')
        retried.set()

    monkeypatch.setattr(job_handoff, 'reconcile_once', reconcile)
    monkeypatch.setattr(job_handoff, 'POLL_SECONDS', .001)
    async with job_handoff.running():
        assert calls == [1]
        await asyncio.wait_for(retried.wait(), 1)
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == 'her-pov-handoff']
        assert len(tasks) == 1
    assert tasks[0].cancelled()


async def test_inline_mode_does_not_start_reconciler(monkeypatch):
    from app import job_handoff
    monkeypatch.setenv('HER_POV_EXECUTION', 'inline')
    with patch.object(job_handoff, 'reconcile_once', AsyncMock()) as reconcile:
        async with job_handoff.running():
            pass
    reconcile.assert_not_awaited()


async def test_concurrent_request_cannot_accept_an_uncommitted_memory_claim():
    entered, release = asyncio.Event(), asyncio.Event()
    async def unavailable(*args):
        entered.set()
        await release.wait()
        raise RuntimeError('durable insert failed')
    with patch.object(hp, '_claim_active_from_db', AsyncMock(return_value=None)), \
         patch.object(hp, '_persist_job', unavailable):
        first = asyncio.create_task(hp.start_her_pov('same-user'))
        await entered.wait()
        second = asyncio.create_task(hp.start_her_pov('same-user'))
        try:
            await asyncio.sleep(0)
            assert not second.done(), 'queued acceptance escaped before durable insertion'
        finally:
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert not hp._JOBS and not hp._ACTIVE_JOB_BY_USER


async def test_cancelled_insert_does_not_leave_an_accepted_memory_job():
    entered = asyncio.Event()
    async def pending(*args):
        entered.set()
        await asyncio.Event().wait()
    with patch.object(hp, '_claim_active_from_db', AsyncMock(return_value=None)), \
         patch.object(hp, '_persist_job', pending):
        task = asyncio.create_task(hp.start_her_pov('cancelled-user'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not hp._JOBS and not hp._ACTIVE_JOB_BY_USER
